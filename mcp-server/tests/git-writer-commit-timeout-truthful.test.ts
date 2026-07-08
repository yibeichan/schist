/**
 * #336 — `git commit` updates the ref BEFORE running the post-commit hook
 * (schist-ingest). When GIT_COMMIT_TIMEOUT_MS expired during the hook, the
 * old code (a) told the caller the write/delete FAILED (GIT_TIMEOUT) when it
 * had committed, (b) ran deleteNote's rollback against the NEW HEAD (which no
 * longer has the path — the restore failed and was swallowed), and (c) killed
 * only the git pid, leaving the hook's sh/ingest children orphaned.
 *
 * Now: the commit timeout re-checks HEAD — if it advanced, the operation
 * reports success with a `commitWarning`; and git() kills the whole detached
 * process group so the hook chain dies with it.
 *
 * Ceilings are read at git-writer module load, so they're pinned here BEFORE
 * the dynamic import (no static import of git-writer in this file). The hook
 * sleeps PAST the commit ceiling — the inverse of git-writer-timeouts.test.ts,
 * where the hook finishes inside it.
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);

const COMMIT_TIMEOUT_MS = 1500;
const HOOK_SLEEP_S = 3;

const createdDirs = new Set<string>();
const savedOpTimeout = process.env.SCHIST_GIT_OP_TIMEOUT_MS;
const savedCommitTimeout = process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS;
// Fast ops keep a roomy ceiling; only the commit ceiling is squeezed under
// the hook's sleep so the timeout fires while the hook is still running.
process.env.SCHIST_GIT_OP_TIMEOUT_MS = "20000";
process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS = String(COMMIT_TIMEOUT_MS);

afterAll(async () => {
  if (savedOpTimeout === undefined) delete process.env.SCHIST_GIT_OP_TIMEOUT_MS;
  else process.env.SCHIST_GIT_OP_TIMEOUT_MS = savedOpTimeout;
  if (savedCommitTimeout === undefined) delete process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS;
  else process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS = savedCommitTimeout;
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-test-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  // Pin hooksPath per-repo so a machine-global core.hooksPath (husky-style)
  // can't silently skip the slow hook and pass the test vacuously.
  await execFile("git", ["config", "core.hooksPath", ".git/hooks"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

/** Install a post-commit hook that outlives the commit ceiling; returns probes. */
async function installSlowHook(vault: string): Promise<{ pidFile: string; marker: string }> {
  const pidFile = path.join(vault, ".git", "hook-pid");
  const marker = path.join(vault, ".git", "hook-finished");
  const hookPath = path.join(vault, ".git", "hooks", "post-commit");
  await fs.writeFile(
    hookPath,
    `#!/bin/sh\necho $$ > "${pidFile}"\nsleep ${HOOK_SLEEP_S}\ntouch "${marker}"\n`,
  );
  await fs.chmod(hookPath, 0o755);
  return { pidFile, marker };
}

async function assertHookChainKilled(pidFile: string, marker: string): Promise<void> {
  // Past the 500ms SIGTERM→SIGKILL escalation window.
  await sleep(900);
  const pid = Number((await fs.readFile(pidFile, "utf-8")).trim());
  expect(Number.isInteger(pid) && pid > 0).toBe(true);
  // Signal 0 probes liveness; the detached-group kill must have taken the
  // hook's shell down with git.
  expect(() => process.kill(pid, 0)).toThrow();
  // And once the hook's full sleep window has elapsed, the completion marker
  // must still be absent — the sleep never finished, it was killed.
  await sleep(HOOK_SLEEP_S * 1000 + 500 - COMMIT_TIMEOUT_MS - 900);
  await expect(fs.access(marker)).rejects.toThrow();
}

describe("truthful commit-timeout reporting (#336)", () => {
  test("writeNote: hook outlives the commit ceiling → success with commitWarning, hook chain killed", async () => {
    const { writeNote } = await import("../src/git-writer.js");
    const vault = await makeTempVault();
    const { pidFile, marker } = await installSlowHook(vault);

    const result = await writeNote(vault, "notes/slow.md", "---\ntitle: Slow\n---\nBody\n", "Slow");

    // The commit LANDED — the caller must be told the truth, plus a warning
    // that ingest didn't finish.
    expect(result.committed).toBe(true);
    expect(result.commitWarning).toMatch(/committed; post-commit ingest/);
    await execFile("git", ["cat-file", "-e", "drafts:notes/slow.md"], { cwd: vault });
    const { stdout } = await execFile("git", ["rev-parse", "drafts"], { cwd: vault });
    expect(result.commitSha).toBe(stdout.trim());

    await assertHookChainKilled(pidFile, marker);
  }, 30000);

  test("deleteNote: hook outlives the commit ceiling → success with commitWarning, no bogus rollback", async () => {
    const { deleteNote } = await import("../src/git-writer.js");
    const vault = await makeTempVault();

    // Tracked note, committed BEFORE the slow hook is installed.
    await fs.mkdir(path.join(vault, "notes"), { recursive: true });
    await fs.writeFile(path.join(vault, "notes", "doomed.md"), "---\ntitle: Doomed\n---\nBody\n");
    await execFile("git", ["add", "-A"], { cwd: vault });
    await execFile("git", ["commit", "-m", "add doomed note"], { cwd: vault });

    const { pidFile, marker } = await installSlowHook(vault);

    const result = await deleteNote(vault, "notes/doomed.md", "Doomed");

    expect(result.committed).toBe(true);
    expect(result.commitWarning).toMatch(/committed; post-commit ingest/);

    // The delete landed on the write branch...
    await expect(
      execFile("git", ["cat-file", "-e", "drafts:notes/doomed.md"], { cwd: vault }),
    ).rejects.toThrow();
    // ...and the old GIT_TIMEOUT path's rollback did NOT resurrect the file
    // in the working tree.
    await expect(fs.access(path.join(vault, "notes", "doomed.md"))).rejects.toThrow();

    await assertHookChainKilled(pidFile, marker);
  }, 30000);
});
