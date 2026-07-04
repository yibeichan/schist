/**
 * deleteNote must give `git commit` the GIT_COMMIT_TIMEOUT_MS ceiling, not the
 * 30s GIT_OP_TIMEOUT_MS default — commit runs the synchronous post-commit
 * ingest hook, which routinely outlives the fast-op ceiling on large vaults
 * (#324; the same defect #257 fixed for withWriteLock).
 *
 * Strategy: shrink SCHIST_GIT_OP_TIMEOUT_MS to 1.5s BEFORE git-writer is
 * loaded (its ceilings are read at module load, hence the dynamic import and
 * no static import of git-writer in this file), install a post-commit hook
 * that sleeps past that ceiling, and assert the delete still lands. Before
 * the fix the commit is SIGTERM'd and deleteNote rejects with GIT_TIMEOUT.
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";

const execFile = promisify(execFileCb);

const OP_TIMEOUT_MS = "1500";
const HOOK_SLEEP_S = 3;

const createdDirs = new Set<string>();
const savedOpTimeout = process.env.SCHIST_GIT_OP_TIMEOUT_MS;
const savedCommitTimeout = process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS;
process.env.SCHIST_GIT_OP_TIMEOUT_MS = OP_TIMEOUT_MS;
// Pin the commit ceiling too: an ambient shell export below the hook's sleep
// would fail the test spuriously even with the fix in place.
process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS = "20000";

afterAll(async () => {
  if (savedOpTimeout === undefined) delete process.env.SCHIST_GIT_OP_TIMEOUT_MS;
  else process.env.SCHIST_GIT_OP_TIMEOUT_MS = savedOpTimeout;
  if (savedCommitTimeout === undefined) delete process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS;
  else process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS = savedCommitTimeout;
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-test-"));
  createdDirs.add(dir);
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  // A machine-global core.hooksPath (husky-style setups) would make git
  // ignore .git/hooks entirely and the slow hook below would never run,
  // letting the test pass with or without the fix. Pin it per-repo.
  await execFile("git", ["config", "core.hooksPath", ".git/hooks"], { cwd: dir });
  await fs.writeFile(path.join(dir, "schist.yaml"), "name: test\nwrite_branch: drafts\n");
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

describe("deleteNote commit timeout ceiling (#324)", () => {
  test("delete survives a post-commit hook slower than the fast-op ceiling", async () => {
    const { deleteNote } = await import("../src/git-writer.js");
    const vault = await makeTempVault();

    // Tracked note (committed before the slow hook is installed).
    await fs.mkdir(path.join(vault, "notes"), { recursive: true });
    await fs.writeFile(path.join(vault, "notes", "doomed.md"), "---\ntitle: Doomed\n---\nBody\n");
    await execFile("git", ["add", "-A"], { cwd: vault });
    await execFile("git", ["commit", "-m", "add doomed note"], { cwd: vault });

    // Post-commit hook that outlives GIT_OP_TIMEOUT_MS but not the commit
    // ceiling. It drops a marker file so we can assert it actually ran —
    // without that, a hook silently skipped by git config would vacuously
    // pass this test.
    const marker = path.join(vault, ".git", "hook-ran");
    const hookPath = path.join(vault, ".git", "hooks", "post-commit");
    await fs.writeFile(hookPath, `#!/bin/sh\nsleep ${HOOK_SLEEP_S}\ntouch "${marker}"\n`);
    await fs.chmod(hookPath, 0o755);

    const result = await deleteNote(vault, "notes/doomed.md", "Doomed");
    expect(result.committed).toBe(true);
    await expect(fs.access(marker)).resolves.toBeUndefined(); // hook really ran, past its sleep

    // The delete really landed on the write branch.
    await execFile("git", ["rev-parse", "--verify", "drafts"], { cwd: vault });
    await expect(
      execFile("git", ["cat-file", "-e", "drafts:notes/doomed.md"], { cwd: vault }),
    ).rejects.toThrow();
  }, 30000);
});
