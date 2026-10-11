import { writeNote, deleteNote } from "../src/git-writer.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);
const createdDirs = new Set<string>();

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-index-lock-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

async function gitDir(vault: string): Promise<string> {
  const d = (await execFile("git", ["rev-parse", "--absolute-git-dir"], { cwd: vault })).stdout.trim();
  return d;
}

// The pre-commit hook runs once per `git commit` attempt. `$n` is the attempt
// number (1-based), so a body can behave differently on the first attempt
// and on a retry.
async function preCommitHook(vault: string, body: string): Promise<void> {
  const dir = path.join(await gitDir(vault), "hooks");
  await fs.mkdir(dir, { recursive: true });
  const counter = path.join(dir, "attempts");
  await fs.writeFile(
    path.join(dir, "pre-commit"),
    `#!/bin/sh\nn=$(( $(cat "${counter}" 2>/dev/null || echo 0) + 1 ))\necho "$n" > "${counter}"\n${body}\n`,
    { mode: 0o755 },
  );
}

async function commitAttempts(vault: string): Promise<number> {
  try {
    return Number((await fs.readFile(path.join(await gitDir(vault), "hooks", "attempts"), "utf-8")).trim());
  } catch {
    return 0;
  }
}

// What git prints when another process holds the index lock — the message a
// background `schist sync push` produces in a write tool's `git commit`.
const LOCK_HELD = `echo "fatal: Unable to create '$(git rev-parse --absolute-git-dir)/index.lock': File exists." >&2; exit 1`;
// The lock holder's own commit: everything staged, ours included, under its message.
const SWEEP = 'c=$(git commit-tree "$(git write-tree)" -p HEAD -m "sync(other): 1 file") && git update-ref HEAD "$c"';

async function blobInHead(vault: string, rel: string): Promise<string | null> {
  try {
    return (await execFile("git", ["show", `HEAD:${rel}`], { cwd: vault })).stdout;
  } catch {
    return null;
  }
}

async function headSubject(vault: string): Promise<string> {
  return (await execFile("git", ["log", "-1", "--format=%s"], { cwd: vault })).stdout.trim();
}

describe("git-writer: another process holds .git/index.lock (#719)", () => {
  afterAll(async () => {
    for (const dir of createdDirs) await fs.rm(dir, { recursive: true, force: true });
  });

  test("writeNote waits out a real index.lock and commits under its own message", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    const lock = path.join(await gitDir(vault), "index.lock");
    await fs.writeFile(lock, "");
    const released = new Promise<void>((resolve) =>
      setTimeout(() => void fs.rm(lock, { force: true }).then(() => resolve()), 400),
    );

    const content = "---\ntitle: Queued\n---\nbody";
    const result = await writeNote(vault, "notes/queued.md", content, "Queued", "orcd");
    await released;

    expect(result.committed).toBe(true);
    expect(result.commitWarning).toBeUndefined();
    expect(await blobInHead(vault, "notes/queued.md")).toBe(content);
    expect(await headSubject(vault)).toContain("Queued — by orcd");
  }, 30000);

  test("writeNote retries a commit that lost the lock", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, `if [ "$n" -eq 1 ]; then ${LOCK_HELD}; fi; exit 0`);

    const content = "---\ntitle: Second\n---\nbody";
    const result = await writeNote(vault, "notes/second.md", content, "Second", "orcd");

    expect(await commitAttempts(vault)).toBe(2);
    expect(result.committed).toBe(true);
    expect(result.commitWarning).toBeUndefined();
    expect(await headSubject(vault)).toContain("Second — by orcd");
  }, 30000);

  test("the lock holder sweeps our staged note while we wait: reported as committed", async () => {
    // The sequence in #719: our commit loses the lock, then the holder's
    // commit takes our staged file with it before our retry.
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, `if [ "$n" -eq 2 ]; then ${SWEEP}; fi; ${LOCK_HELD}`);

    const content = "---\ntitle: Swept\n---\nbody";
    const result = await writeNote(vault, "notes/swept.md", content, "Swept", "orcd");

    expect(await commitAttempts(vault)).toBe(2);
    expect(result.committed).toBe(true);
    expect(result.commitWarning).toContain("concurrent");
    expect(await blobInHead(vault, "notes/swept.md")).toBe(content);
  }, 30000);

  test("lock still held after the retries: a distinct error that says the file was written", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, LOCK_HELD);

    const err = await writeNote(vault, "notes/stuck.md", "---\ntitle: Stuck\n---\nbody").then(
      () => null,
      (e: unknown) => e as { error?: string; message?: string; details?: { path?: string } },
    );

    expect(err).not.toBeNull();
    expect(err!.error).toBe("GIT_COMMIT_FAILED_FILE_WRITTEN");
    expect(err!.details?.path).toBe("notes/stuck.md");
    expect(err!.message).toContain("index.lock");
    expect(await commitAttempts(vault)).toBeGreaterThan(1);
    // Truthful: the file is on disk, staged, and not in HEAD.
    await expect(fs.access(path.join(vault, "notes/stuck.md"))).resolves.toBeUndefined();
    expect(await blobInHead(vault, "notes/stuck.md")).toBeNull();
  }, 30000);

  test("a commit refused for another reason is not retried, and still says the file was written", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/seed.md", "---\ntitle: Seed\n---\nseed");
    await preCommitHook(vault, 'echo "ERROR: Potential secret detected" >&2; exit 1');

    const err = await writeNote(vault, "notes/refused.md", "---\ntitle: R\n---\nr").then(
      () => null,
      (e: unknown) => e as { error?: string; details?: { path?: string } },
    );

    expect(await commitAttempts(vault)).toBe(1);
    expect(err!.error).toBe("GIT_COMMIT_FAILED_FILE_WRITTEN");
    expect(err!.details?.path).toBe("notes/refused.md");
  }, 30000);

  test("deleteNote retries a commit that lost the lock", async () => {
    const vault = await makeTempVault();
    await writeNote(vault, "notes/doomed.md", "---\ntitle: Doomed\n---\nbye");
    await preCommitHook(vault, `if [ "$n" -eq 1 ]; then ${LOCK_HELD}; fi; exit 0`);

    const result = await deleteNote(vault, "notes/doomed.md", "Doomed", "orcd");

    expect(await commitAttempts(vault)).toBe(2);
    expect(result.committed).toBe(true);
    expect(await blobInHead(vault, "notes/doomed.md")).toBeNull();
    expect(await headSubject(vault)).toContain("delete Doomed — by orcd");
  }, 30000);
});
