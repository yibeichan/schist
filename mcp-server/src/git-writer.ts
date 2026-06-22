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
  /**
   * The HEAD sha after this call. When `committed: true`, this is the sha of
   * the new commit. When `committed: false` (the dedup path), this is the
   * current HEAD sha — which may match a sha returned by an earlier call that
   * wrote identical content. Callers must not assume the sha is unique per
   * call.
   */
  commitSha: string;
  committed: boolean;
};

/**
 * Normalize a caller-supplied title for use inside a git commit message.
 *
 * Strips CR/LF (newlines would produce multi-paragraph commits and a leading
 * `#` line could be stripped by `commit.cleanup`), collapses whitespace, and
 * truncates to keep `git log --oneline` readable. The title is interpolated
 * into the message, never passed through a shell — `execFile` keeps us safe
 * from meta-character injection independently of this normalization.
 */
function sanitizeCommitTitle(title: string): string {
  const collapsed = title.replace(/[\r\n]+/g, " ").replace(/\s+/g, " ").trim();
  return collapsed.length > 200 ? collapsed.slice(0, 197) + "..." : collapsed;
}

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

/**
 * Commit-message attribution: `— by {owner}` when `owner` is provided (the
 * normal MCP path, since tools.ts requires identity per #63), otherwise
 * `— via MCP` for callers that haven't been wired through the identity
 * gate yet. Newlines and excessive whitespace in the owner are stripped
 * for the same reason as commit titles — the value flows into a single
 * git `-m` line via execFile, so meta-character injection isn't possible,
 * but linebreaks would still produce multi-paragraph commits.
 */
function attribution(owner: string | undefined): string {
  if (!owner) return "via MCP";
  const cleaned = owner.replace(/[\r\n]+/g, " ").replace(/\s+/g, " ").trim();
  return cleaned.length > 0 ? `by ${cleaned}` : "via MCP";
}

export async function writeNote(
  vaultRoot: string,
  relPath: string,
  content: string,
  commitTitle?: string,
  owner?: string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  // Prefer the caller-supplied title — gray-matter folds long or special-char
  // titles to `>-` / `|-`, so regex-parsing the rendered YAML produced commit
  // messages like "write >- — via MCP" (issue #104). Fall back to relPath
  // when no title is provided.
  const rawTitle = commitTitle && commitTitle.trim().length > 0 ? commitTitle : relPath;
  const title = sanitizeCommitTitle(rawTitle);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): write ${title} — ${attribution(owner)}`,
    async (absPath) => {
      await fs.writeFile(absPath, content, "utf-8");
    }
  );
}

export async function appendToNote(
  vaultRoot: string,
  relPath: string,
  addition: string,
  owner?: string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): append to ${sanitizeCommitTitle(relPath)} — ${attribution(owner)}`,
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

/**
 * Replace an existing note's content. Identical to writeNote except for the
 * commit verb ("update" vs "write") so `git log --oneline` distinguishes a
 * curation edit from an initial create. The dedup path in withWriteLock makes
 * a no-op update (content unchanged) return `committed: false` rather than
 * churning the history. Issue #119.
 */
export async function updateNote(
  vaultRoot: string,
  relPath: string,
  content: string,
  commitTitle?: string,
  owner?: string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  const rawTitle = commitTitle && commitTitle.trim().length > 0 ? commitTitle : relPath;
  const title = sanitizeCommitTitle(rawTitle);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): update ${title} — ${attribution(owner)}`,
    async (absPath) => {
      await fs.writeFile(absPath, content, "utf-8");
    }
  );
}

/**
 * Delete a note via `git rm`, optionally repairing notes that linked to it in
 * the SAME commit (the cascade path — see delete_note in tools.ts). Issue #119.
 *
 * `repairs` are pre-computed (caller stripped the dangling `## Connections`
 * lines). Order matters: `git rm` runs FIRST (it's the operation most likely
 * to fail — e.g. target untracked on the write branch), then the repairs are
 * written + staged, then a single commit lands the deletion and edge cleanup
 * atomically. On ANY failure we `git reset --hard HEAD` before rethrowing,
 * because a later unrelated write commits ALL staged paths (withWriteLock's
 * `git commit` has no pathspec) — leftover staged repairs would otherwise leak
 * into the next commit. We do NOT reuse withWriteLock: it stages exactly one
 * path and short-circuits on an empty diff, neither of which fits a multi-file
 * delete. `git rm` of a tracked file always produces a staged change.
 */
export async function deleteNote(
  vaultRoot: string,
  relPath: string,
  noteTitle: string,
  owner?: string,
  repairs: Array<{ relPath: string; content: string }> = []
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  for (const r of repairs) assertPathSafe(vaultRoot, r.relPath);
  const title = sanitizeCommitTitle(noteTitle && noteTitle.trim().length > 0 ? noteTitle : relPath);
  const release = await writeMutex.acquire();
  try {
    const branch = await getWriteBranch(vaultRoot);
    await ensureBranch(vaultRoot, branch);
    await git(vaultRoot, ["checkout", branch]);

    try {
      await git(vaultRoot, ["rm", "--quiet", "--", relPath]);

      for (const r of repairs) {
        const absPath = path.resolve(vaultRoot, r.relPath);
        await fs.writeFile(absPath, r.content, "utf-8");
        await git(vaultRoot, ["add", "--", r.relPath]);
      }

      // NEVER --no-verify — hard coded out (mirrors withWriteLock).
      await git(vaultRoot, ["commit", "-m", `feat(schist): delete ${title} — ${attribution(owner)}`]);
      const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
      return { path: relPath, commitSha: sha, committed: true };
    } catch (e) {
      // Roll back partial staging/working-tree edits so a failed delete can't
      // leak into the next write's commit. Best-effort — the original error is
      // what the caller needs to see.
      try {
        await git(vaultRoot, ["reset", "--hard", "HEAD"]);
      } catch {
        /* nothing more we can do */
      }
      throw e;
    }
  } finally {
    release();
  }
}
