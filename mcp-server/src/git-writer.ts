import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import { Mutex, withTimeout } from "async-mutex";
import * as fs from "fs/promises";
import * as path from "path";
import { load as yamlLoad } from "js-yaml";

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

// Fast local git ops (checkout/add/diff/rev-parse) should never take long;
// `git commit` fires the synchronous post-commit hook (schist-ingest), so it
// needs a much larger ceiling. Without a timeout a stalled ingest wedges the
// held write mutex forever, and every subsequent write returns WRITE_TIMEOUT
// while the root call never resolves (#257). Both are env-overridable.
const GIT_OP_TIMEOUT_MS = Number(process.env.SCHIST_GIT_OP_TIMEOUT_MS) || 30000;
const GIT_COMMIT_TIMEOUT_MS = Number(process.env.SCHIST_GIT_COMMIT_TIMEOUT_MS) || 120000;

async function git(vaultRoot: string, args: string[], timeoutMs: number = GIT_OP_TIMEOUT_MS): Promise<string> {
  try {
    const { stdout } = await execFile("git", args, { cwd: vaultRoot, timeout: timeoutMs });
    return stdout.trim();
  } catch (e: unknown) {
    // execFile kills the child with SIGTERM on timeout and rejects with
    // killed=true. Surface a typed, actionable error instead of a raw
    // "Command failed" so the caller can distinguish a hang from a git error.
    if (e !== null && typeof e === "object" && (e as { killed?: boolean }).killed) {
      throw Object.assign(
        new Error(
          `git ${args[0]} timed out after ${timeoutMs}ms — the post-commit hook / schist-ingest may be stalled`,
        ),
        { error: "GIT_TIMEOUT" },
      );
    }
    throw e;
  }
}

async function getWriteBranch(vaultRoot: string): Promise<string> {
  const configPath = path.join(vaultRoot, "schist.yaml");
  let content: string;
  try {
    content = await fs.readFile(configPath, "utf-8");
  } catch {
    return "drafts"; // no schist.yaml — expected for bare test vaults
  }
  try {
    // Real YAML parse, not a regex: a trailing inline comment on the
    // write_branch line made the old regex miss and silently fall back to
    // "drafts", diverging from loadVaultConfig's js-yaml result (#277).
    const raw = yamlLoad(content) as Record<string, unknown> | null;
    const v = raw?.write_branch;
    if (typeof v === "string" && v.trim()) return v.trim();
    if (v !== undefined && v !== null) {
      // Same fallback loadVaultConfig's getString applies, but writes landing
      // on an unexpected branch must not be silent — that's the #277 failure
      // mode. stderr is safe for a stdio MCP server.
      console.error(
        `schist: write_branch in schist.yaml is not a string (${JSON.stringify(v)}) — writing to "drafts"`,
      );
    }
    return "drafts";
  } catch (e) {
    console.error(
      `schist: cannot parse schist.yaml — writing to "drafts": ${e instanceof Error ? e.message : String(e)}`,
    );
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

/**
 * Symlink-aware containment guard, run INSIDE the write lock AFTER the write
 * branch is checked out — the only point where the on-disk bytes at `relPath`
 * match what we're about to write to. A lexical / pre-checkout realpath check
 * (e.g. tools.ts:resolvesInsideVault) can be defeated by branch skew: a regular
 * file on the current branch, a symlink at the same path on the write branch.
 *
 * Guards two escape shapes that a plain `realpath(file)` would miss:
 *  - a symlinked ANCESTOR dir pointing outside the vault (caught via the parent
 *    realpath), and
 *  - the note path itself being a symlink — including one whose target does not
 *    yet exist, which `fs.writeFile` would happily CREATE outside the vault
 *    (caught via lstat, before any read/write follows the link).
 * The vault root may itself be a symlink (env vars often point at one), so both
 * sides are realpath-resolved. New-file writes (lstat ENOENT) are allowed. #119.
 */
async function assertResolvesInside(vaultRoot: string, relPath: string): Promise<void> {
  const realRoot = await fs.realpath(vaultRoot);
  const absPath = path.resolve(vaultRoot, relPath);
  const realParent = await fs.realpath(path.dirname(absPath));
  if (realParent !== realRoot && !realParent.startsWith(realRoot + path.sep)) {
    throw { error: "PATH_TRAVERSAL", message: "Write path resolves outside vault root (symlinked dir?)" };
  }
  try {
    const st = await fs.lstat(absPath);
    if (st.isSymbolicLink()) {
      throw { error: "PATH_TRAVERSAL", message: "Note path is a symlink; refusing to write through it" };
    }
  } catch (e) {
    // Rethrow our own ToolError; ENOENT just means a brand-new file — allowed.
    if (e !== null && typeof e === "object" && "error" in e) throw e;
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
      await execFile("git", ["diff", "--cached", "--quiet", "--", relPath], { cwd: vaultRoot, timeout: GIT_OP_TIMEOUT_MS });
    } catch (e: unknown) {
      // `git diff --cached --quiet` exits 1 when there IS a staged diff, which
      // execFile reports as a throw — that's the normal "has changes" path. A
      // timeout also throws (killed=true); don't silently treat a hung diff as
      // "has changes" — surface it so the write fails loudly rather than
      // committing on a bad signal.
      if (e !== null && typeof e === "object" && (e as { killed?: boolean }).killed) {
        throw Object.assign(
          new Error(`git diff timed out after ${GIT_OP_TIMEOUT_MS}ms`),
          { error: "GIT_TIMEOUT" },
        );
      }
      hasChanges = true;
    }
    if (!hasChanges) {
      const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
      return { path: relPath, commitSha: sha, committed: false };
    }

    // NEVER --no-verify — hard coded out. Larger timeout: commit runs the
    // synchronous post-commit ingest hook.
    await git(vaultRoot, ["commit", "-m", commitMessage], GIT_COMMIT_TIMEOUT_MS);
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
  commitTitle: string | undefined,
  owner: string | undefined,
  transform: (current: string) => string
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  const rawTitle = commitTitle && commitTitle.trim().length > 0 ? commitTitle : relPath;
  const title = sanitizeCommitTitle(rawTitle);
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): update ${title} — ${attribution(owner)}`,
    async (absPath) => {
      // Authoritative symlink/containment guard, post-checkout (see
      // assertResolvesInside) — the handler's pre-checkout check can be skipped
      // by branch skew. Runs before any read/write follows the path.
      await assertResolvesInside(vaultRoot, relPath);
      // Re-read the note INSIDE the write lock (after checkout) and build the
      // new content from the fresh on-disk state. If the note vanished since the
      // handler's pre-lock read — e.g. a concurrent delete_note committed in the
      // window — abort with NOT_FOUND rather than resurrecting it from stale
      // content. #119.
      let current: string;
      try {
        current = await fs.readFile(absPath, "utf-8");
      } catch {
        throw { error: "NOT_FOUND", message: `Note not found: ${relPath}` };
      }
      await fs.writeFile(absPath, transform(current), "utf-8");
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
 * atomically. On ANY failure we roll back ONLY the paths this call touched
 * (via `git restore`) before rethrowing,
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
        // Post-checkout symlink/containment guard before writing through the
        // repair path (a write-branch symlink could redirect outside the vault;
        // the `git rm` of the delete target above is safe — it removes the link
        // rather than writing through it). #119.
        await assertResolvesInside(vaultRoot, r.relPath);
        const absPath = path.resolve(vaultRoot, r.relPath);
        await fs.writeFile(absPath, r.content, "utf-8");
        await git(vaultRoot, ["add", "--", r.relPath]);
      }

      // NEVER --no-verify — hard coded out (mirrors withWriteLock).
      await git(vaultRoot, ["commit", "-m", `feat(schist): delete ${title} — ${attribution(owner)}`]);
      const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
      return { path: relPath, commitSha: sha, committed: true };
    } catch (e) {
      // Roll back ONLY the paths this delete touched (the removed note + any
      // repaired linkers), NOT the whole tree. A `git reset --hard HEAD` here
      // would also discard unrelated uncommitted edits elsewhere in the vault
      // (human edits, pre-existing staged work) on any delete failure. `git
      // restore --source=HEAD --staged --worktree` un-deletes / un-modifies
      // exactly these paths. Best-effort — the original error is what the
      // caller needs to see.
      try {
        const touched = [relPath, ...repairs.map((r) => r.relPath)];
        await git(vaultRoot, ["restore", "--staged", "--worktree", "--source=HEAD", "--", ...touched]);
      } catch {
        /* nothing more we can do */
      }
      throw e;
    }
  } finally {
    release();
  }
}
