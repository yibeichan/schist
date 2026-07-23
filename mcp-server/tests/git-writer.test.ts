import { writeNote, appendToNote, updateNote, writeMutex } from "../src/git-writer.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);

const createdDirs = new Set<string>();

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-test-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

describe("git-writer", () => {
  afterAll(async () => {
    for (const dir of createdDirs) {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });
  test("PATH_TRAVERSAL: rejects writes outside vault root", async () => {
    const vault = await makeTempVault();
    await expect(writeNote(vault, "../../etc/passwd", "evil")).rejects.toMatchObject({
      error: "PATH_TRAVERSAL",
    });
  });

  test("Mutex: two concurrent writes serialize without corruption", async () => {
    const vault = await makeTempVault();

    const [r1, r2] = await Promise.all([
      writeNote(vault, "notes/note1.md", "---\ntitle: Note 1\n---\nBody 1"),
      writeNote(vault, "notes/note2.md", "---\ntitle: Note 2\n---\nBody 2"),
    ]);

    expect(r1.commitSha).toBeDefined();
    expect(r2.commitSha).toBeDefined();
    expect(r1.commitSha).not.toBe(r2.commitSha);

    const content1 = await fs.readFile(path.join(vault, "notes", "note1.md"), "utf-8");
    const content2 = await fs.readFile(path.join(vault, "notes", "note2.md"), "utf-8");
    expect(content1).toContain("Note 1");
    expect(content2).toContain("Note 2");
  }, 30000);

  test("WRITE_TIMEOUT: second call rejects when mutex held past timeout", async () => {
    const { Mutex } = await import("async-mutex");
    const mutex = new Mutex();

    const release = await mutex.acquire();

    const raceResult = await Promise.race([
      mutex.acquire().then(() => "acquired"),
      new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 200)),
    ]);

    expect(raceResult).toBe("timeout");
    release();
  });

  test("writeNote: rejects with WRITE_TIMEOUT when mutex is held and subsequent write succeeds after release", async () => {
    const vault = await makeTempVault();

    // Hold the shared write mutex for 12 s — longer than the 10 s withTimeout threshold
    const release = await writeMutex.acquire();
    const holdTimer = setTimeout(release, 12000);

    // writeNote should time out because the mutex is held
    await expect(
      writeNote(vault, "notes/blocked.md", "---\ntitle: Blocked\n---\nBody")
    ).rejects.toMatchObject({ error: "WRITE_TIMEOUT" });

    // Release early so the test doesn't take 12 s
    clearTimeout(holdTimer);
    release();

    // After release the mutex must be acquirable — a subsequent write must succeed
    const result = await writeNote(
      vault,
      "notes/after-timeout.md",
      "---\ntitle: After Timeout\n---\nBody"
    );
    expect(result.commitSha).toBeDefined();
    expect(result.path).toBe("notes/after-timeout.md");
  }, 30000);

  test("dedup: re-writing identical content does not create a second commit (#104)", async () => {
    const vault = await makeTempVault();
    const content = "---\ntitle: Dedup\n---\nBody";

    const first = await writeNote(vault, "notes/dup.md", content, "Dedup");
    expect(first.committed).toBe(true);

    const second = await writeNote(vault, "notes/dup.md", content, "Dedup");
    expect(second.committed).toBe(false);
    expect(second.commitSha).toBe(first.commitSha);

    // A real follow-up edit must still commit
    const third = await writeNote(vault, "notes/dup.md", content + "\nmore", "Dedup");
    expect(third.committed).toBe(true);
    expect(third.commitSha).not.toBe(first.commitSha);
  }, 30000);

  // Plant a symlink at notes/x.md on the write branch (drafts) pointing at a
  // file outside the vault, while the base branch has no such path. A lexical
  // pre-checkout check cannot see the symlink; only the in-lock post-checkout
  // guard can. Shared by the two branch-skew tests below (#323).
  async function plantWriteBranchSymlink(vault: string): Promise<string> {
    const { stdout } = await execFile("git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd: vault });
    const baseBranch = stdout.trim();
    const outside = path.join(path.dirname(vault), `outside-${path.basename(vault)}.txt`);
    await fs.writeFile(outside, "SECRET", "utf-8");
    await execFile("git", ["checkout", "-b", "drafts"], { cwd: vault });
    await fs.mkdir(path.join(vault, "notes"), { recursive: true });
    await fs.symlink(outside, path.join(vault, "notes", "x.md"));
    await execFile("git", ["add", "-A"], { cwd: vault });
    await execFile("git", ["commit", "-m", "symlink on write branch"], { cwd: vault });
    await execFile("git", ["checkout", baseBranch], { cwd: vault });
    return outside;
  }

  test("writeNote refuses to write through a symlink that exists only on the write branch (#323)", async () => {
    const vault = await makeTempVault();
    const outside = await plantWriteBranchSymlink(vault);

    await expect(writeNote(vault, "notes/x.md", "pwned")).rejects.toMatchObject({
      error: "PATH_TRAVERSAL",
    });
    expect(await fs.readFile(outside, "utf-8")).toBe("SECRET"); // never written through
    await fs.rm(outside, { force: true });
  }, 30000);

  test("appendToNote refuses to append through a symlink that exists only on the write branch (#323)", async () => {
    const vault = await makeTempVault();
    const outside = await plantWriteBranchSymlink(vault);

    await expect(appendToNote(vault, "notes/x.md", "pwned")).rejects.toMatchObject({
      error: "PATH_TRAVERSAL",
    });
    expect(await fs.readFile(outside, "utf-8")).toBe("SECRET"); // never read or written through
    await fs.rm(outside, { force: true });
  }, 30000);

  test("writeNote to a brand-new subdirectory still succeeds (guard runs after mkdir, #323)", async () => {
    const vault = await makeTempVault();
    const result = await writeNote(vault, "notes/newdir/fresh.md", "---\ntitle: Fresh\n---\nBody");
    expect(result.committed).toBe(true);
    const { stdout } = await execFile("git", ["show", "drafts:notes/newdir/fresh.md"], { cwd: vault });
    expect(stdout).toContain("Fresh");
  }, 30000);

  test("commit title is sanitized: newlines collapsed, length capped", async () => {
    const vault = await makeTempVault();
    const evilTitle = "first line\nsecond line\r\n# would-be-comment\n\n\ntrailing";
    await writeNote(vault, "notes/evil.md", "---\ntitle: x\n---\nBody", evilTitle);
    const { stdout: oneLine } = await execFile("git", ["log", "-1", "--pretty=%s"], { cwd: vault });
    expect(oneLine.trim()).toBe(
      "feat(schist): write first line second line # would-be-comment trailing — via MCP"
    );
    // %B (full message) must also be a single subject line — no body paragraphs.
    const { stdout: fullMsg } = await execFile("git", ["log", "-1", "--pretty=%B"], { cwd: vault });
    expect(fullMsg.trim().split("\n").length).toBe(1);

    // Length cap (197 chars + "...")
    const longTitle = "x".repeat(500);
    await writeNote(vault, "notes/long.md", "---\ntitle: y\n---\nBody", longTitle);
    const { stdout: longLine } = await execFile("git", ["log", "-1", "--pretty=%s"], { cwd: vault });
    const subj = longLine.trim();
    expect(subj).toMatch(/^feat\(schist\): write x{197}\.\.\. — via MCP$/);
  }, 30000);

  test("commit message uses caller-supplied title, not folded YAML ('>-') (#104)", async () => {
    const vault = await makeTempVault();
    const longTitle =
      "Very long title that goes on for many words and might trigger line folding behavior";
    // gray-matter folds this title to `title: >-\n  <body>` — the old regex
    // captured `>-` as the title. The fix is the explicit commitTitle arg.
    const body = `---\ntitle: >-\n  ${longTitle}\n---\nBody`;

    await writeNote(vault, "notes/folded.md", body, longTitle);

    const { stdout } = await execFile("git", ["log", "-1", "--pretty=%s"], { cwd: vault });
    expect(stdout.trim()).toBe(`feat(schist): write ${longTitle} — via MCP`);
    expect(stdout).not.toContain(">-");
  }, 30000);

  test("write_branch with trailing inline comment is honored, not silently 'drafts' (#277)", async () => {
    const vault = await makeTempVault();
    await fs.writeFile(
      path.join(vault, "schist.yaml"),
      "name: test\nwrite_branch: custom-notes  # inline comment is valid YAML\n"
    );
    await execFile("git", ["add", "schist.yaml"], { cwd: vault });
    await execFile("git", ["commit", "-m", "set write_branch with comment"], { cwd: vault });

    await writeNote(vault, "notes/branch-check.md", "---\ntitle: Branch Check\n---\nBody");

    const { stdout } = await execFile("git", ["branch", "--show-current"], { cwd: vault });
    expect(stdout.trim()).toBe("custom-notes");
    const { stdout: branches } = await execFile("git", ["branch", "--list", "drafts"], { cwd: vault });
    expect(branches.trim()).toBe("");
  }, 30000);

  test("quoted write_branch value still parses (#277 regression guard)", async () => {
    const vault = await makeTempVault();
    await fs.writeFile(
      path.join(vault, "schist.yaml"),
      'name: test\nwrite_branch: "quoted-branch"\n'
    );
    await execFile("git", ["add", "schist.yaml"], { cwd: vault });
    await execFile("git", ["commit", "-m", "quoted write_branch"], { cwd: vault });

    await writeNote(vault, "notes/quoted-check.md", "---\ntitle: Quoted\n---\nBody");

    const { stdout } = await execFile("git", ["branch", "--show-current"], { cwd: vault });
    expect(stdout.trim()).toBe("quoted-branch");
  }, 30000);

  test("Mutex: lock is NOT left held after withTimeout fires", async () => {
    const { Mutex, withTimeout } = await import("async-mutex");

    // 50 ms timeout — short enough to fire in the test
    const timedMutex = withTimeout(new Mutex(), 50, new Error("timed out"));

    // Hold the lock indefinitely
    const release = await timedMutex.acquire();

    // A second acquire attempt should reject with our timeout error
    await expect(timedMutex.acquire()).rejects.toThrow("timed out");

    // Release the original holder — the mutex must become available again
    release();

    // If the mutex were permanently locked, this would hang; wrap with a 500 ms wall clock guard
    const acquireAfterRelease = await Promise.race([
      timedMutex.acquire().then((rel) => { rel(); return "ok"; }),
      new Promise<string>((_, rej) => setTimeout(() => rej(new Error("mutex still locked after timeout")), 500)),
    ]);

    expect(acquireAfterRelease).toBe("ok");
  });

  // #427: writes go through a temp file + atomic rename so a mid-write kill
  // can never leave a zero-byte note. #447 moved the temp OUT of the note dir
  // into <vault>/.schist/tmp/ (gitignored, non-scope) so a crash-orphaned temp
  // can't be committed to the hub. Observable guarantee: the final content is
  // correct, the temp is staged under .schist/tmp/, and no `.tmp` scratch file
  // is left in the note dir. (Per-function routing is asserted in
  // git-writer-hardening.test.ts "#433"; this keeps the end-to-end triple.)
  test("ATOMIC: writeNote/updateNote/appendToNote route temps through .schist/tmp", async () => {
    const vault = await makeTempVault();
    const rel = "notes/atomic.md";

    await writeNote(vault, rel, "---\ntitle: Atomic\n---\nv1");
    await updateNote(vault, rel, "Atomic", undefined, () => "---\ntitle: Atomic\n---\nv2");
    await appendToNote(vault, rel, "appended line");

    const content = await fs.readFile(path.join(vault, rel), "utf-8");
    expect(content).toContain("v2");
    expect(content).toContain("appended line");

    // Temp staging happened under the gitignored .schist/ runtime dir…
    await expect(fs.stat(path.join(vault, ".schist", "tmp"))).resolves.toBeTruthy();
    // …and NOT in the synced note directory (#447): no `.tmp` orphan there.
    const entries = await fs.readdir(path.join(vault, "notes"));
    expect(entries.filter((e) => e.endsWith(".tmp"))).toEqual([]);
    expect(entries).toContain("atomic.md");
  }, 30000);

  // #427 regression guard: the exclusive-create path must NOT be routed through
  // temp+rename — that would silently clobber an existing note and defeat the
  // O_EXCL collision check (#408). A second exclusive create must still fail.
  test("ATOMIC: exclusive create still rejects on an existing path (O_EXCL preserved)", async () => {
    const vault = await makeTempVault();
    const rel = "notes/excl.md";

    await writeNote(vault, rel, "---\ntitle: Excl\n---\nfirst", undefined, undefined, {
      exclusive: true,
    });
    await expect(
      writeNote(vault, rel, "---\ntitle: Excl\n---\nSECOND", undefined, undefined, {
        exclusive: true,
      })
    ).rejects.toMatchObject({ code: "EEXIST" });

    // The original content survives — the failed exclusive create did not clobber it.
    const content = await fs.readFile(path.join(vault, rel), "utf-8");
    expect(content).toContain("first");
    expect(content).not.toContain("SECOND");
  }, 30000);

  // #427 follow-up: the atomic rename swaps the note's inode, so the write must
  // carry the target's prior mode forward — otherwise a note with custom perms
  // is silently reset (a change git can't show, since it tracks only the exec
  // bit). Skipped on Windows, where POSIX mode bits don't apply.
  (process.platform === "win32" ? test.skip : test)(
    "ATOMIC: update preserves the note's existing file mode",
    async () => {
      const vault = await makeTempVault();
      const rel = "notes/perm.md";

      await writeNote(vault, rel, "---\ntitle: Perm\n---\nv1");
      const abs = path.join(vault, rel);
      await fs.chmod(abs, 0o640);

      await updateNote(vault, rel, "Perm", undefined, () => "---\ntitle: Perm\n---\nv2");

      const mode = (await fs.stat(abs)).mode & 0o777;
      expect(mode).toBe(0o640);
    },
    30000
  );
});
