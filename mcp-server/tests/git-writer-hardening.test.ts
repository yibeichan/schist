/**
 * git-writer hardening regressions:
 *
 * #331 — write_branch from schist.yaml flowed unvalidated into git argv.
 * `write_branch: "-f"` passed the old ensureBranch (`git branch -f` with no
 * name just lists branches, exit 0) and then `git checkout -f`
 * force-checked-out the CURRENT branch, silently discarding uncommitted
 * edits. Now: loud VALIDATION_ERROR naming the bad value, working tree
 * untouched.
 *
 * #335 — withWriteLock's `fs.mkdir(dirname, { recursive: true })` ran BEFORE
 * the symlink containment guard (which realpaths the parent and needs it to
 * exist). mkdir follows symlinked ancestors, so `notes → /outside` plus a
 * write to `notes/sub/n.md` created `/outside/sub` before the guard rejected
 * the write. Now: the deepest EXISTING ancestor is realpath-validated first,
 * so the write fails with PATH_TRAVERSAL and nothing is created outside.
 */
import {
  writeNote,
  updateNote,
  appendToNote,
  deleteNote,
  endOfOptionsArgs,
  parseGitMajorMinor,
  __setGitVersionCacheForTesting,
} from "../src/git-writer.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);

const createdDirs = new Set<string>();

afterAll(async () => {
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

async function makeTempVault(writeBranch = "drafts"): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-test-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), `name: test\nwrite_branch: "${writeBranch}"\n`);
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

describe("write_branch validation (#331)", () => {
  test.each(["-f", "--detach"])(
    "write_branch %p fails loudly and leaves uncommitted work untouched",
    async (badBranch) => {
      const vault = await makeTempVault(badBranch);

      // Tracked file with an UNCOMMITTED modification — exactly what the old
      // `git checkout -f` path silently discarded.
      const seedPath = path.join(vault, "seed.md");
      await fs.writeFile(seedPath, "committed content\n");
      await execFile("git", ["add", "seed.md"], { cwd: vault });
      await execFile("git", ["commit", "-m", "seed"], { cwd: vault });
      await fs.writeFile(seedPath, "UNCOMMITTED local edit\n");

      await expect(
        writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
      ).rejects.toMatchObject({
        error: "VALIDATION_ERROR",
        message: expect.stringContaining(badBranch),
      });

      // The force-checkout never ran: the dirty edit survives and no branch
      // named after the bad value was created.
      expect(await fs.readFile(seedPath, "utf-8")).toBe("UNCOMMITTED local edit\n");
      const { stdout } = await execFile("git", ["branch", "--list"], { cwd: vault });
      expect(stdout).not.toContain(badBranch);
    },
  );

  test("other git-invalid names (embedded ..) are rejected by check-ref-format", async () => {
    const vault = await makeTempVault("a..b");
    await expect(
      writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
    ).rejects.toMatchObject({
      error: "VALIDATION_ERROR",
      message: expect.stringContaining("a..b"),
    });
  });

  test.each(["HEAD", "@"])(
    "write_branch %p is rejected — it rev-parses to the CURRENT branch, so accepting it silently writes wherever HEAD points (#370 review)",
    async (alias) => {
      const vault = await makeTempVault(alias);
      await expect(
        writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
      ).rejects.toMatchObject({
        error: "VALIDATION_ERROR",
        message: expect.stringContaining(alias),
      });
      // Nothing landed on the checked-out branch.
      await expect(
        execFile("git", ["cat-file", "-e", "HEAD:notes/n.md"], { cwd: vault }),
      ).rejects.toThrow();
    },
  );

  test("write_branch @{-1} (dynamic ref) is rejected even when the reflog can resolve it (#370)", async () => {
    const vault = await makeTempVault("@{-1}");
    // Give the reflog a previous branch so @{-1} RESOLVES. The old
    // `check-ref-format --branch` NORMALIZED the value against exactly this
    // state — it validated the resolved branch's name and exited 0, letting
    // a dynamic ref through as a literal write_branch. The full-ref form
    // rejects `@{` unconditionally.
    await execFile("git", ["checkout", "-b", "previous"], { cwd: vault });
    await execFile("git", ["checkout", "-"], { cwd: vault });

    await expect(
      writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
    ).rejects.toMatchObject({
      error: "VALIDATION_ERROR",
      message: expect.stringContaining("@{-1}"),
    });
    // The resolved branch was never written to either.
    const { stdout } = await execFile(
      "git", ["ls-tree", "--name-only", "previous"], { cwd: vault },
    );
    expect(stdout).not.toContain("notes");
  });

  test("a valid non-default write_branch still works", async () => {
    const vault = await makeTempVault("scratch/drafts");
    const result = await writeNote(vault, "notes/ok.md", "---\ntitle: OK\n---\nBody\n");
    expect(result.committed).toBe(true);
    await execFile("git", ["cat-file", "-e", "scratch/drafts:notes/ok.md"], { cwd: vault });
  });
});

describe("mkdir ancestry containment (#335)", () => {
  test("symlinked ancestor: write rejected AND no directory created outside the vault", async () => {
    const vault = await makeTempVault();
    const outside = await fs.mkdtemp(path.join(os.tmpdir(), "schist-outside-"));
    createdDirs.add(outside);
    await fs.symlink(outside, path.join(vault, "notes"));

    await expect(
      writeNote(vault, "notes/sub/evil.md", "---\ntitle: Evil\n---\nBody\n"),
    ).rejects.toMatchObject({ error: "PATH_TRAVERSAL" });

    // The whole point of #335: the guard must fire BEFORE the recursive
    // mkdir, so the escape target must not exist.
    await expect(fs.access(path.join(outside, "sub"))).rejects.toThrow();
    expect(await fs.readdir(outside)).toEqual([]);
  });

  test("dangling symlinked ancestor: write rejected without creating the target", async () => {
    const vault = await makeTempVault();
    const target = path.join(os.tmpdir(), `schist-dangling-${Date.now()}`);
    await fs.symlink(target, path.join(vault, "notes"));

    await expect(
      writeNote(vault, "notes/sub/evil.md", "---\ntitle: Evil\n---\nBody\n"),
    ).rejects.toBeTruthy();
    // Nothing materialized at the dangling target.
    await expect(fs.access(target)).rejects.toThrow();
  });

  test("legitimate brand-new nested directory still works", async () => {
    const vault = await makeTempVault();
    const result = await writeNote(vault, "notes/a/b/deep.md", "---\ntitle: Deep\n---\nBody\n");
    expect(result.committed).toBe(true);
    await execFile("git", ["cat-file", "-e", "drafts:notes/a/b/deep.md"], { cwd: vault });
  });
});

describe("--end-of-options gated on runtime git version (#355 deploy fix)", () => {
  // `--end-of-options` requires git ≥2.24; older git (common on HPC login
  // nodes) rejects it and breaks every write. The flag is belt-and-suspenders
  // behind validation + the universal trailing `--`, so it must be OMITTED on
  // old / unknown git.
  afterEach(() => {
    __setGitVersionCacheForTesting(undefined); // restore real detection
  });

  test("parseGitMajorMinor extracts major.minor from git --version output", () => {
    expect(parseGitMajorMinor("git version 2.50.1 (Apple Git-155)")).toEqual([2, 50]);
    expect(parseGitMajorMinor("git version 2.24.0")).toEqual([2, 24]);
    expect(parseGitMajorMinor("git version 1.8.3.1")).toEqual([1, 8]);
    expect(parseGitMajorMinor("git version 3.0.0")).toEqual([3, 0]);
    expect(parseGitMajorMinor("not a version string")).toBeNull();
  });

  test("flag IS present when detected git ≥2.24", async () => {
    __setGitVersionCacheForTesting([2, 24]);
    expect(await endOfOptionsArgs()).toEqual(["--end-of-options"]);
    __setGitVersionCacheForTesting([2, 50]);
    expect(await endOfOptionsArgs()).toEqual(["--end-of-options"]);
    __setGitVersionCacheForTesting([3, 0]);
    expect(await endOfOptionsArgs()).toEqual(["--end-of-options"]);
  });

  test("flag is ABSENT when detected git <2.24 or unknown", async () => {
    __setGitVersionCacheForTesting([2, 23]);
    expect(await endOfOptionsArgs()).toEqual([]);
    __setGitVersionCacheForTesting([1, 8]);
    expect(await endOfOptionsArgs()).toEqual([]);
    // Detection failure / unparseable → treated as old, the safe default.
    __setGitVersionCacheForTesting(null);
    expect(await endOfOptionsArgs()).toEqual([]);
  });

  test("write still succeeds on the <2.24 path (validation + trailing -- only)", async () => {
    // Force the old-git code path even though the test host runs modern git:
    // the write must land using only the universal guards.
    __setGitVersionCacheForTesting([2, 20]);
    const vault = await makeTempVault();
    const result = await writeNote(vault, "notes/old-git.md", "---\ntitle: Old Git\n---\nBody\n");
    expect(result.committed).toBe(true);
    await execFile("git", ["cat-file", "-e", "drafts:notes/old-git.md"], { cwd: vault });
  });

  test("write_branch validation still rejects option-like names on the <2.24 path", async () => {
    // The real injection guard is version-independent: even without the flag,
    // a "-f" write_branch is rejected before it reaches git argv.
    __setGitVersionCacheForTesting([2, 20]);
    const vault = await makeTempVault("-f");
    await expect(
      writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
    ).rejects.toMatchObject({ error: "VALIDATION_ERROR" });
  });
});

describe("checkout omits --end-of-options (#444)", () => {
  // #355 spread endOfOptionsArgs() into the branch-checkout argv, producing
  // `git checkout --end-of-options <branch> --`. But `git checkout` only learned
  // to honor --end-of-options AFTER 2.43 (fails on ≤2.43, works on ≥2.49) —
  // unlike rev-parse/branch, which honor it since 2.24. endOfOptionsArgs() gates
  // on ≥2.24, so on git in [2.24, ~2.44) the flag reached checkout, which read
  // it as an operand and rejected the command with "only one reference expected,
  // 2 given" — breaking every write. The common HPC git 2.43.x is in that
  // window; the CI runner (2.54) is past it, so CI never caught it. The #355
  // hardening tests forced the OLD-git path ([2,20]), so endOfOptionsArgs()
  // returned [] and the flagged checkout argv was never exercised.
  //
  // These tests force git version [2,43] — the exact broken window on a common
  // HPC host — so endOfOptionsArgs() would return the flag, then run the real
  // write/delete paths. They go red if the flag is re-added to a checkout site,
  // regardless of the host git version (verified: red against pre-fix source).
  afterEach(() => {
    __setGitVersionCacheForTesting(undefined); // restore real detection
  });

  test("writeNote succeeds on the ≥2.24 path (checkout must not carry the flag)", async () => {
    __setGitVersionCacheForTesting([2, 43]);
    // Sanity-check the premise: the flag IS returned for this version, so the
    // checkout call site is the only reason the write can still succeed.
    expect(await endOfOptionsArgs()).toEqual(["--end-of-options"]);
    const vault = await makeTempVault();
    const result = await writeNote(vault, "notes/modern.md", "---\ntitle: Modern\n---\nBody\n");
    expect(result.committed).toBe(true);
    await execFile("git", ["cat-file", "-e", "drafts:notes/modern.md"], { cwd: vault });
  });

  test("deleteNote succeeds on the ≥2.24 path (checkout must not carry the flag)", async () => {
    __setGitVersionCacheForTesting([2, 43]);
    const vault = await makeTempVault();
    await writeNote(vault, "notes/doomed.md", "---\ntitle: Doomed\n---\nBody\n");
    const result = await deleteNote(vault, "notes/doomed.md", "Doomed");
    expect(result.committed).toBe(true);
    await expect(
      execFile("git", ["cat-file", "-e", "drafts:notes/doomed.md"], { cwd: vault }),
    ).rejects.toBeTruthy();
  });
});

describe("atomic-write temp lives under .schist/tmp (#433)", () => {
  // atomicWriteFile writes to a temp then renames over the target. A hard kill
  // (OOM/SIGKILL/force-quit/power-loss) in the write→rename window runs no
  // cleanup, orphaning the temp. If that temp sat in the note's own directory —
  // a synced scope like notes/ — the next `schist sync push` would stage and
  // commit the junk-named orphan (possibly a truncated partial note) to the hub
  // and every spoke (#433). The temp must instead be created under
  // <vault>/.schist/tmp/, which is gitignored (`.schist/`) and never a sync
  // scope target, so a leaked orphan is inert. These go red if atomicWriteFile
  // reverts to a same-dir temp: .schist/tmp/ would never be created, and a temp
  // shape could reappear under the scope dir.
  //
  // The EXDEV fallback (`.schist` on a different filesystem than the note ⇒
  // retry the temp in the note's own dir) is not unit-tested here: forcing
  // fs.rename to raise EXDEV needs a mock of the frozen `fs/promises` ESM
  // namespace, which would break makeTempVault and every other test in this
  // file. The branch is small and symmetric with markdown_io._atomic_write,
  // whose EXDEV fallback IS directly covered — see
  // cli/tests/test_markdown_io.py::test_atomic_write_falls_back_to_target_dir_on_cross_fs_exdev.
  async function listTmpShaped(dir: string): Promise<string[]> {
    let entries: string[] = [];
    try {
      entries = await fs.readdir(dir);
    } catch {
      return [];
    }
    return entries.filter((n) => n.endsWith(".tmp"));
  }

  test("writeNote routes its atomic temp through .schist/tmp, not the scope dir", async () => {
    const vault = await makeTempVault();
    const result = await writeNote(vault, "notes/created.md", "---\ntitle: Created\n---\nBody\n");
    expect(result.committed).toBe(true);

    // The temp staging dir was created under the gitignored .schist/ runtime dir.
    const tmpDir = path.join(vault, ".schist", "tmp");
    await expect(fs.stat(tmpDir)).resolves.toBeTruthy();
    // No orphaned temp under the synced scope dir.
    expect(await listTmpShaped(path.join(vault, "notes"))).toEqual([]);
  });

  test("updateNote (read-modify-write) keeps its temp out of the scope dir", async () => {
    // The RMW path is where a mid-write kill is most damaging — the target
    // already holds content that a same-dir orphan could shadow/partial-commit.
    const vault = await makeTempVault();
    await writeNote(vault, "notes/edited.md", "---\ntitle: Edited\n---\nOne\n");
    const result = await updateNote(
      vault,
      "notes/edited.md",
      undefined,
      undefined,
      () => "---\ntitle: Edited\n---\nTwo\n",
    );
    expect(result.committed).toBe(true);

    await expect(fs.stat(path.join(vault, ".schist", "tmp"))).resolves.toBeTruthy();
    expect(await listTmpShaped(path.join(vault, "notes"))).toEqual([]);
    // Content actually updated across the cross-directory rename.
    const onDisk = await execFile("git", ["show", "drafts:notes/edited.md"], { cwd: vault });
    expect(onDisk.stdout).toContain("Two");
  });

  test("appendToNote keeps its temp out of the scope dir (#450 coverage gap)", async () => {
    // #447 covered writeNote/updateNote but not appendToNote — a same-dir revert
    // there would have slipped through. appendToNote reads-then-atomic-writes,
    // so its temp must also route through .schist/tmp.
    const vault = await makeTempVault();
    await writeNote(vault, "notes/log.md", "---\ntitle: Log\n---\nline1\n");
    const result = await appendToNote(vault, "notes/log.md", "line2");
    expect(result.committed).toBe(true);

    await expect(fs.stat(path.join(vault, ".schist", "tmp"))).resolves.toBeTruthy();
    expect(await listTmpShaped(path.join(vault, "notes"))).toEqual([]);
    const onDisk = await execFile("git", ["show", "drafts:notes/log.md"], { cwd: vault });
    expect(onDisk.stdout).toContain("line1"); // prior content preserved
    expect(onDisk.stdout).toContain("line2"); // addition applied
  });
});

describe("appendToNote read error is not swallowed (#449)", () => {
  // appendToNote read the existing note under a bare `catch {}` labeled "file
  // doesn't exist yet". ENOENT IS that case — the note is created. But ANY other
  // read error (EACCES/EIO/EISDIR/stale NFS handle) was also swallowed, leaving
  // existing="" so atomicWriteFile overwrote the note with ONLY the addition,
  // erasing all prior content. The fix re-raises every non-ENOENT read error.
  test("ENOENT (note absent) still creates the note from just the addition", async () => {
    const vault = await makeTempVault();
    const result = await appendToNote(vault, "notes/fresh.md", "first line");
    expect(result.committed).toBe(true);
    const onDisk = await execFile("git", ["show", "drafts:notes/fresh.md"], { cwd: vault });
    expect(onDisk.stdout).toContain("first line");
  });

  test("a non-ENOENT read error aborts the append instead of clobbering", async () => {
    // Deterministic non-ENOENT read error, no root-bypass concerns: put a
    // DIRECTORY where the note path is — fs.readFile then rejects with EISDIR.
    // Pre-fix that was swallowed and the append proceeded; post-fix it must
    // propagate so nothing overwrites the path.
    const vault = await makeTempVault();
    await fs.mkdir(path.join(vault, "notes", "asdir.md"), { recursive: true });
    // Pin the rejection to the readFile EISDIR (code ≠ ENOENT), so this can't
    // pass for the wrong reason if a future containment guard rejects earlier.
    await expect(
      appendToNote(vault, "notes/asdir.md", "should not be written"),
    ).rejects.toMatchObject({ code: "EISDIR" });
    // The directory is untouched — the append never wrote through it.
    const stat = await fs.stat(path.join(vault, "notes", "asdir.md"));
    expect(stat.isDirectory()).toBe(true);
  });
});

describe("write_branch ref-DWIM detach (#381)", () => {
  // Each of these is lexically a valid branch name, but git's ref search
  // order resolves it to ANOTHER ref when one exists — the checkout then
  // detached HEAD and the "committed" note became reflog-only after the
  // next write. The namespace-shaped class is rejected outright.
  test.each([
    ["refs/heads/foo", async (vault: string) => {
      await execFile("git", ["branch", "foo"], { cwd: vault });
    }],
    ["heads/foo", async (vault: string) => {
      await execFile("git", ["branch", "foo"], { cwd: vault });
    }],
    ["tags/v1", async (vault: string) => {
      await execFile("git", ["tag", "v1"], { cwd: vault });
    }],
    ["remotes/origin/main", async (vault: string) => {
      await execFile(
        "git", ["update-ref", "refs/remotes/origin/main", "HEAD"], { cwd: vault },
      );
    }],
  ] as Array<[string, (vault: string) => Promise<void>]>)(
    "write_branch %p is rejected instead of detaching HEAD onto the DWIM target",
    async (badBranch, seedRef) => {
      const vault = await makeTempVault(badBranch);
      await seedRef(vault);
      const before = (
        await execFile("git", ["rev-list", "--all"], { cwd: vault })
      ).stdout;

      await expect(
        writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n"),
      ).rejects.toMatchObject({
        error: "VALIDATION_ERROR",
        message: expect.stringContaining(badBranch),
      });

      // HEAD is still an attached symbolic ref and no commit was stranded.
      const { stdout: head } = await execFile(
        "git", ["symbolic-ref", "HEAD"], { cwd: vault },
      );
      expect(head.trim()).toMatch(/^refs\/heads\//);
      const after = (
        await execFile("git", ["rev-list", "--all"], { cwd: vault })
      ).stdout;
      expect(after).toBe(before);
    },
  );

  test("write_branch colliding with an existing tag creates the real branch and stays attached", async () => {
    // Pre-#381 the bare `rev-parse --verify v1` DWIMed to the TAG, skipped
    // branch creation, and the checkout detached onto the tag — every write
    // reported committed:true onto no branch at all.
    const vault = await makeTempVault("v1");
    await execFile("git", ["tag", "v1"], { cwd: vault });

    const result = await writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n");
    expect(result.committed).toBe(true);

    const { stdout: head } = await execFile(
      "git", ["symbolic-ref", "HEAD"], { cwd: vault },
    );
    expect(head.trim()).toBe("refs/heads/v1");
    // The note landed on the BRANCH (full ref — no DWIM in the assertion
    // either), and the returned sha is reachable from it.
    await execFile(
      "git", ["cat-file", "-e", "refs/heads/v1:notes/n.md"], { cwd: vault },
    );
    const { stdout: reachable } = await execFile(
      "git", ["branch", "--contains", result.commitSha as string], { cwd: vault },
    );
    expect(reachable).toContain("v1");
  });

  test("write_branch ambiguous between existing branch and tag lands on the branch", async () => {
    const vault = await makeTempVault("v2");
    await execFile("git", ["branch", "v2"], { cwd: vault });
    await execFile("git", ["tag", "v2"], { cwd: vault });

    const result = await writeNote(vault, "notes/n.md", "---\ntitle: N\n---\nBody\n");
    expect(result.committed).toBe(true);
    const { stdout: head } = await execFile(
      "git", ["symbolic-ref", "HEAD"], { cwd: vault },
    );
    expect(head.trim()).toBe("refs/heads/v2");
    await execFile(
      "git", ["cat-file", "-e", "refs/heads/v2:notes/n.md"], { cwd: vault },
    );
  });
});
