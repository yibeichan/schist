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
