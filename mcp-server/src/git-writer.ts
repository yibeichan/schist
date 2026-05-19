import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { Mutex, withTimeout } from "async-mutex";
import * as fs from "fs/promises";
import * as path from "path";

const execFile = promisify(execFileCb);
const WRITE_TIMEOUT_ERROR = Object.assign(
  new Error("Git write timed out after 10s — another write is in progress"),
  { error: "WRITE_TIMEOUT" }
);

/**
 * @internal — exported for test instrumentation only.
 *
 * Timer / graceful-shutdown note:
 * `withTimeout` from async-mutex creates a `setTimeout` internally only
 * while an `acquire()` call is waiting (i.e. the mutex is contended). The
 * timer is created inside the Promise executor and is always cleared via
 * `clearTimeout` before resolve/reject completes — confirmed by reading
 * async-mutex@0.5.x src (lib/withTimeout.js case 2/3). There is no
 * persistent background timer attached to the wrapper itself, so no
 * `.unref()` is needed and graceful shutdown is not affected.
 */
export const writeMutex = withTimeout(new Mutex(), 10000, WRITE_TIMEOUT_ERROR);

async function git(vaultRoot: string, args: string[]): Promise<string> {
  const { stdout } = await execFile("git", args, { cwd: vaultRoot });
  return stdout.trim();
}

async function getWriteBranch(vaultRoot: string): Promise<string> {
  try {
    const configPath = path.join(vaultRoot, "schist.yaml");
    const content = await fs.readFile(configPath, "utf-8");
    const match = content.match(/write_branch:\s*["']?(\S+?)["']?\s*$/m);
    return match ? match[1] : "drafts";
  } catch {
    return "drafts";
  }
}

async function ensureBranch(vaultRoot: string, branch: string): Promise<void> {
  try {
    await git(vaultRoot, ["rev-parse", "--verify", branch]);
  } catch {
    await git(vaultRoot, ["branch", branch]);
  }
}

function assertPathSafe(vaultRoot: string, relPath: string): void {
  const absVaultRoot = path.resolve(vaultRoot);
  const absPath = path.resolve(vaultRoot, relPath);
  if (!absPath.startsWith(absVaultRoot + path.sep) && absPath !== absVaultRoot) {
    throw { error: "PATH_TRAVERSAL", message: "Write path is outside vault root" };
  }
}

export type WriteResult = {
  path: string;
  commitSha: string;
  committed: boolean;
};

/**
 * Acquires the write mutex, checks out the write branch, runs fn(absPath),
 * then stages + commits relPath. Shared by writeNote and appendToNote to
 * eliminate duplicated mutex/git boilerplate.
 *
 * Dedup: after staging, if the working tree shows no change against HEAD for
 * relPath, the commit is skipped and `committed: false` is returned with the
 * current HEAD sha. This prevents the "MCP write tool over-commits" pattern
 * (issue #104) where re-emitting identical content produced empty-message
 * churn in the vault history.
 */
async function withWriteLock(
  vaultRoot: string,
  relPath: string,
  commitMessage: string,
  fn: (absPath: string) => Promise<void>
): Promise<WriteResult> {
  const release = await writeMutex.acquire();
  try {
    const branch = await getWriteBranch(vaultRoot);
    await ensureBranch(vaultRoot, branch);
    await git(vaultRoot, ["checkout", branch]);

    const absPath = path.resolve(vaultRoot, relPath);
    await fs.mkdir(path.dirname(absPath), { recursive: true });
    await fn(absPath);

    await git(vaultRoot, ["add", relPath]);

    // Dedup: skip commit if staged content matches HEAD. `git diff --cached
    // --quiet` exits 0 when there is no staged diff, 1 when there is. execFile
    // throws on non-zero exit, so we catch the "has diff" branch.
    let hasChanges = false;
    try {
      await execFile("git", ["diff", "--cached", "--quiet", "--", relPath], { cwd: vaultRoot });
    } catch {
      hasChanges = true;
    }
    if (!hasChanges) {
      const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
      return { path: relPath, commitSha: sha, committed: false };
    }

    // NEVER --no-verify — hard coded out
    await git(vaultRoot, ["commit", "-m", commitMessage]);
    const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
    return { path: relPath, commitSha: sha, committed: true };
  } finally {
    release();
  }
}

export async function writeNote(
  vaultRoot: string,
  relPath: string,
  content: string,
  commitTitle?: string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  // Prefer the caller-supplied title — gray-matter folds long or special-char
  // titles to `>-` / `|-`, so regex-parsing the rendered YAML produced commit
  // messages like "write >- — via MCP" (issue #104). Fall back to relPath
  // when no title is provided.
  const title = commitTitle && commitTitle.trim().length > 0 ? commitTitle : relPath;
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): write ${title} — via MCP`,
    async (absPath) => {
      await fs.writeFile(absPath, content, "utf-8");
    }
  );
}

export async function appendToNote(
  vaultRoot: string,
  relPath: string,
  addition: string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): append to ${relPath} — via MCP`,
    async (absPath) => {
      let existing = "";
      try {
        existing = await fs.readFile(absPath, "utf-8");
      } catch {
        // file doesn't exist yet
      }
      const newContent = existing + (existing.endsWith("\n") ? "" : "\n") + addition;
      await fs.writeFile(absPath, newContent, "utf-8");
    }
  );
}
