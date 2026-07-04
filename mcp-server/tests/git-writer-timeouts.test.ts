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
process.env.SCHIST_GIT_OP_TIMEOUT_MS = OP_TIMEOUT_MS;

afterAll(async () => {
  if (savedOpTimeout === undefined) delete process.env.SCHIST_GIT_OP_TIMEOUT_MS;
  else process.env.SCHIST_GIT_OP_TIMEOUT_MS = savedOpTimeout;
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

    // Post-commit hook that outlives GIT_OP_TIMEOUT_MS but not the commit ceiling.
    const hookPath = path.join(vault, ".git", "hooks", "post-commit");
    await fs.writeFile(hookPath, `#!/bin/sh\nsleep ${HOOK_SLEEP_S}\n`);
    await fs.chmod(hookPath, 0o755);

    const result = await deleteNote(vault, "notes/doomed.md", "Doomed");
    expect(result.committed).toBe(true);

    // The delete really landed on the write branch.
    await expect(
      execFile("git", ["cat-file", "-e", "drafts:notes/doomed.md"], { cwd: vault }),
    ).rejects.toThrow();
  }, 30000);
});
