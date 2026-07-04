import { writeNote, writeMutex } from "../src/git-writer.js";
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
});
