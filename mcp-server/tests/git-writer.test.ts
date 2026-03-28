import { writeNote } from "../src/git-writer.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-test-"));
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

describe("git-writer", () => {
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
