import { execFile as execFileCb, spawn } from "child_process";
import { promisify } from "util";
import { Mutex, withTimeout } from "async-mutex";
import * as fs from "fs/promises";
import * as path from "path";
import { randomBytes } from "crypto";
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

function isGitTimeout(e: unknown): boolean {
  return e !== null && typeof e === "object" && (e as { error?: unknown }).error === "GIT_TIMEOUT";
}

/**
 * `--end-of-options` (which stops git parsing later args as flags) was only
 * added in git 2.24. On an HPC login node the user lands on a different host
 * each session with whatever git module happens to be loaded — possibly an
 * ancient system git — and `git checkout` is STRICT about unknown options, so
 * an unconditional `--end-of-options` would break EVERY write with a cryptic
 * "unknown option 'end-of-options'". We therefore gate the flag on the
 * runtime git version and treat any parse failure / unknown version as OLD
 * (omit the flag) — the safe default.
 *
 * The flag is only ever belt-and-suspenders: the guards that actually prevent
 * option injection work on every git version and stay UNCONDITIONAL —
 * `assertValidWriteBranch` (full-ref check-ref-format + leading-dash reject)
 * rejects option-like branch names, and the trailing `--` pathspec separator
 * (universal, ancient) is kept on the checkout/add/rm calls. So on old git we
 * degrade to "validation + trailing `--`"; on git ≥2.24 we keep the full
 * hardening.
 */
export function parseGitMajorMinor(versionOutput: string): [number, number] | null {
  // e.g. "git version 2.50.1 (Apple Git-155)" → [2, 50]
  const m = /(\d+)\.(\d+)/.exec(versionOutput);
  if (!m) return null;
  return [Number(m[1]), Number(m[2])];
}

// undefined = not yet detected; null = detected-but-unknown (treated as old).
let gitVersionCache: [number, number] | null | undefined = undefined;

/** Detect the runtime git major.minor once and cache it for the process. */
async function getGitMajorMinor(): Promise<[number, number] | null> {
  if (gitVersionCache !== undefined) return gitVersionCache;
  try {
    const { stdout } = await execFile("git", ["--version"], { timeout: GIT_OP_TIMEOUT_MS });
    gitVersionCache = parseGitMajorMinor(stdout);
  } catch {
    gitVersionCache = null; // git --version failed → assume old, omit the flag
  }
  return gitVersionCache;
}

/**
 * `["--end-of-options"]` when the runtime git is ≥2.24, otherwise `[]`.
 * Spread into git argv immediately before the branch-name operand.
 */
export async function endOfOptionsArgs(): Promise<string[]> {
  const v = await getGitMajorMinor();
  if (!v) return [];
  const [maj, min] = v;
  return maj > 2 || (maj === 2 && min >= 24) ? ["--end-of-options"] : [];
}

/** @internal — test-only: force or reset the cached git version. */
export function __setGitVersionCacheForTesting(v: [number, number] | null | undefined): void {
  gitVersionCache = v;
}

async function git(vaultRoot: string, args: string[], timeoutMs: number = GIT_OP_TIMEOUT_MS): Promise<string> {
  return await new Promise<string>((resolve, reject) => {
    // detached:true puts git in its own process group so a timeout can kill
    // the WHOLE group. `git commit` runs the post-commit hook (sh →
    // schist-ingest) as children; execFile's built-in timeout only SIGTERMs
    // the git pid, leaving the hook chain orphaned and still churning after
    // the caller was told the operation timed out (#336). Mirrors tools.ts
    // runCommand's kill strategy.
    const child = spawn("git", args, {
      cwd: vaultRoot,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const finish = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn();
    };
    const killGroup = (sig: NodeJS.Signals): void => {
      if (child.pid === undefined) return;
      try {
        process.kill(-child.pid, sig);
      } catch {
        /* group already gone */
      }
    };
    const timer = setTimeout(() => {
      killGroup("SIGTERM");
      // Escalate in case the hook traps/ignores SIGTERM. unref'd so a dead
      // group doesn't hold the event loop open.
      setTimeout(() => killGroup("SIGKILL"), 500).unref();
      // Typed, actionable error instead of a raw "Command failed" so the
      // caller can distinguish a hang from a git error.
      finish(() =>
        reject(
          Object.assign(
            new Error(
              `git ${args[0]} timed out after ${timeoutMs}ms — the post-commit hook / schist-ingest may be stalled`,
            ),
            { error: "GIT_TIMEOUT" },
          ),
        ),
      );
    }, timeoutMs);
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf-8");
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      stderr += chunk.toString("utf-8");
    });
    child.on("error", (err) => finish(() => reject(err)));
    child.on("close", (code, signal) => {
      if (code === 0) {
        finish(() => resolve(stdout.trim()));
      } else {
        finish(() =>
          reject(
            Object.assign(
              new Error(`Command failed: git ${args.join(" ")}\n${stderr}`),
              { code, signal, stderr },
            ),
          ),
        );
      }
    });
  });
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

/**
 * write_branch flows from schist.yaml straight into git argv (#331). An
 * option-like value such as "-f" passed the old ensureBranch (`git branch -f`
 * with no name just lists branches, exit 0) and then `git checkout -f`
 * force-checkouts the CURRENT branch, silently discarding uncommitted vault
 * edits. Validate with git's own rules before the value is ever used, and
 * fail loudly naming the bad value — a misconfigured write_branch must never
 * degrade into "write somewhere else".
 */
async function assertValidWriteBranch(vaultRoot: string, branch: string): Promise<void> {
  const invalid = {
    error: "VALIDATION_ERROR",
    message: `write_branch ${JSON.stringify(branch)} in schist.yaml is not a valid git branch name — fix write_branch before retrying this write`,
  };
  // Fast-reject option-like values ourselves; belt for the case where a git
  // version parses the value below as a flag instead of a branch name.
  if (branch.startsWith("-")) throw invalid;
  // Ref-namespace prefixes are lexically valid branch names but resolve
  // through git's ref DWIM to OTHER refs when those exist: "refs/heads/foo",
  // "heads/foo", "tags/v1", "remotes/origin/main" all make the later
  // checkout detach HEAD onto the DWIM target while the write still reports
  // committed:true — the next checkout then strands that commit reflog-only
  // (#381). Nobody means a literal branch named "refs/heads/foo"; reject the
  // whole namespace-shaped class. assertAttachedToWriteBranch is the
  // behavioral backstop for DWIM forms no blocklist anticipates.
  const first = branch.split("/", 1)[0];
  if (first === "refs" || first === "heads" || first === "tags" || first === "remotes") {
    throw invalid;
  }
  // "HEAD" and "@" are LEXICALLY valid as refs/heads/<x> below but are not
  // branch names: both rev-parse to the current branch, so ensureBranch's
  // checkout becomes an attached no-op and the note silently commits to
  // whatever branch is checked out — the exact "write somewhere else"
  // degradation this function exists to prevent. git's own --branch mode
  // (strbuf_check_branch_ref) special-cases HEAD beyond the full-ref rules
  // for the same reason; mirror it, and close the "@" alias hole the old
  // form left open. Verified end-to-end in git-writer-hardening.test.ts.
  if (branch === "HEAD" || branch === "@") throw invalid;
  // Validate the FULL ref form, not `--branch <name>` (#370):
  //  - `--branch` needs git ≥1.8.2; on the older gits common on HPC login
  //    nodes it exits non-zero ("unknown option") and the catch below turned
  //    EVERY write into a VALIDATION_ERROR blaming a perfectly valid
  //    write_branch. The full-ref form is original git syntax. The same
  //    old-git concern is why --end-of-options is version-gated
  //    (endOfOptionsArgs), and this path must not quietly require a NEWER
  //    git than that gated one.
  //  - `--branch` also NORMALIZES: `@{-1}` resolves to the previously
  //    checked-out branch and validates THAT, so a dynamic ref sneaks
  //    through as a write_branch value and rev-parses to whatever branch
  //    happens to be in reflog history. The full-ref form rejects `@{`
  //    outright (check-ref-format rule 8) — write_branch must be a literal
  //    branch name.
  try {
    await git(vaultRoot, ["check-ref-format", `refs/heads/${branch}`]);
  } catch (e) {
    if (isGitTimeout(e)) throw e;
    throw invalid;
  }
}

async function ensureBranch(vaultRoot: string, branch: string): Promise<void> {
  await assertValidWriteBranch(vaultRoot, branch);
  // --end-of-options (git ≥2.24 only, see endOfOptionsArgs): even if validation
  // were ever bypassed, an option-like name can only be read as a branch name,
  // never as a flag (#331).
  const eoo = await endOfOptionsArgs();
  try {
    // Full-ref form, no DWIM (#381): bare `rev-parse --verify <branch>`
    // resolves through the ref search order, so an existing TAG of the same
    // name (write_branch "v1", tag v1) satisfied the check, no local branch
    // was created, and the later checkout detached onto the tag. Only an
    // actual local branch may skip creation.
    await git(vaultRoot, ["rev-parse", "--verify", ...eoo, `refs/heads/${branch}`]);
  } catch (e) {
    // A hung rev-parse is not "branch missing" — don't try to create on it.
    if (isGitTimeout(e)) throw e;
    await git(vaultRoot, ["branch", ...eoo, branch]);
  }
}

/**
 * Post-checkout backstop for #381: whatever form slipped past validation and
 * however `git checkout <branch>` DWIM-resolved it, refuse to write unless
 * HEAD is attached to exactly refs/heads/<branch>. A detached HEAD here means
 * the commit would land on no branch and become reflog-only garbage after the
 * next checkout — silent data loss reported as committed:true.
 */
async function assertAttachedToWriteBranch(vaultRoot: string, branch: string): Promise<void> {
  let ref: string;
  try {
    // --quiet: detached HEAD exits 1 without noise; we map it below.
    ref = await git(vaultRoot, ["symbolic-ref", "--quiet", "HEAD"]);
  } catch (e) {
    if (isGitTimeout(e)) throw e;
    ref = "";
  }
  if (ref !== `refs/heads/${branch}`) {
    throw {
      error: "VALIDATION_ERROR",
      message:
        `write_branch ${JSON.stringify(branch)} in schist.yaml resolved to ` +
        `${ref === "" ? "a detached HEAD" : JSON.stringify(ref)} instead of ` +
        `"refs/heads/${branch}" — refusing to write (the commit would not land ` +
        `on the write branch). Fix write_branch before retrying this write`,
    };
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
/**
 * Pre-mkdir containment guard (#335). withWriteLock must mkdir the note's
 * parent chain BEFORE assertResolvesInside can run (that guard realpaths the
 * parent, which has to exist) — but `fs.mkdir(..., { recursive: true })`
 * FOLLOWS symlinks, so with a symlinked ancestor (`notes` → /outside) and a
 * write to `notes/sub/n.md` the mkdir created `/outside/sub` before the
 * post-mkdir guard ever rejected the write. Walk up from the target dir to
 * the deepest EXISTING ancestor, realpath it, and require it to resolve
 * inside the vault root; only then is the recursive mkdir safe. (A dangling
 * symlink ancestor needs no extra handling: realpath skips it as
 * non-existing, and the recursive mkdir itself then fails with ENOTDIR
 * without creating anything — verified on node 20.)
 */
async function assertExistingAncestryInside(vaultRoot: string, absDir: string): Promise<void> {
  const realRoot = await fs.realpath(vaultRoot);
  let probe = absDir;
  for (;;) {
    let real: string;
    try {
      real = await fs.realpath(probe);
    } catch {
      const parent = path.dirname(probe);
      if (parent === probe) {
        // Ran out of ancestors without resolving one — containment can't be
        // proven, so refuse. Unreachable in practice: assertPathSafe already
        // pinned absDir lexically inside vaultRoot, which exists.
        throw { error: "PATH_TRAVERSAL", message: "Cannot resolve any existing ancestor of the write path" };
      }
      probe = parent;
      continue;
    }
    if (real !== realRoot && !real.startsWith(realRoot + path.sep)) {
      throw { error: "PATH_TRAVERSAL", message: "Write path ancestor resolves outside vault root (symlinked dir?)" };
    }
    return;
  }
}

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
  /**
   * Present when the commit LANDED but `git commit` was killed by
   * GIT_COMMIT_TIMEOUT_MS while the synchronous post-commit hook
   * (schist-ingest) was still running (#336). The write itself succeeded —
   * only the index refresh may lag or need a re-ingest.
   */
  commitWarning?: string;
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
    // Trailing "--": force the branch reading even if a file shares the name
    // (universal, ancient). NO --end-of-options here (#444): `git checkout` only
    // learned to honor that flag after 2.43 (fails on ≤2.43, works on ≥2.49) —
    // unlike rev-parse/branch, which honor it since 2.24. endOfOptionsArgs()
    // gates on ≥2.24, so on git in [2.24, ~2.44) the flag reached checkout,
    // which read it as an operand and rejected the command with "only one
    // reference expected, 2 given" — breaking EVERY write (the common HPC git
    // 2.43.x is in that window). The real option-injection defense is
    // assertValidWriteBranch (full check-ref-format + leading-dash reject),
    // unconditional and version-independent, so the flag is safe to drop here.
    await git(vaultRoot, ["checkout", branch, "--"]);
    await assertAttachedToWriteBranch(vaultRoot, branch);

    const absPath = path.resolve(vaultRoot, relPath);
    // Containment check on the deepest EXISTING ancestor BEFORE mkdir — the
    // recursive mkdir follows symlinked ancestors and would otherwise create
    // real directories outside the vault for a write that the post-mkdir
    // guard below is about to reject (#335).
    await assertExistingAncestryInside(vaultRoot, path.dirname(absPath));
    await fs.mkdir(path.dirname(absPath), { recursive: true });
    // Authoritative symlink/containment guard, post-checkout — the only point
    // where on-disk symlink state matches the write branch (see
    // assertResolvesInside). Lives HERE, not in each caller's fn, so every
    // writer routed through withWriteLock is covered; writeNote and
    // appendToNote skipped it when it lived only in updateNote's fn (#323).
    // Must run AFTER the mkdir above: the guard realpaths the parent dir,
    // which may not exist yet for a brand-new subdirectory.
    await assertResolvesInside(vaultRoot, relPath);
    await fn(absPath);

    await git(vaultRoot, ["add", "--", relPath]);

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
    //
    // git updates the ref BEFORE running post-commit, so a timeout that fires
    // during a slow hook kills a commit that already LANDED; reporting that
    // as GIT_TIMEOUT tells the caller the write failed when it's on the
    // branch (#336). Capture HEAD first and re-check it on timeout.
    let preCommitHead: string | null = null;
    try {
      preCommitHead = await git(vaultRoot, ["rev-parse", "HEAD"]);
    } catch {
      // Unborn branch — any resolvable post-timeout HEAD means the commit landed.
    }
    let commitWarning: string | undefined;
    try {
      await git(vaultRoot, ["commit", "-m", commitMessage], GIT_COMMIT_TIMEOUT_MS);
    } catch (e) {
      if (!isGitTimeout(e)) throw e;
      let headNow: string | null = null;
      try {
        headNow = await git(vaultRoot, ["rev-parse", "HEAD"]);
      } catch {
        // Still unborn / unreadable — treat as "commit did not land".
      }
      // A rev-parse FAILURE here (headNow === null) is deliberately treated as
      // "did not land" — the conservative direction: we rethrow GIT_TIMEOUT
      // rather than claim success we can't confirm. The false-negative (commit
      // actually landed but HEAD was momentarily unreadable) is harmless for
      // writeNote: a retry re-stages identical content and withWriteLock's
      // dedup path returns committed:false without churning history.
      if (headNow === null || headNow === preCommitHead) throw e;
      commitWarning =
        "committed; post-commit ingest still running or timed out — the index may lag this write until the next ingest";
    }
    const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
    return {
      path: relPath,
      commitSha: sha,
      committed: true,
      ...(commitWarning !== undefined ? { commitWarning } : {}),
    };
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

/**
 * Write `content` to `absPath` atomically (write-to-temp + rename).
 *
 * A plain `fs.writeFile(absPath, ...)` with flag "w" opens the target with
 * O_TRUNC, zeroing the file BEFORE any byte is written. A process kill (OOM,
 * SIGKILL, Claude Desktop force-quit, power loss) in that window leaves a
 * zero-byte note, and any uncommitted content is unrecoverable (#427). We
 * write to a unique temp file in the SAME directory (⇒ same filesystem ⇒
 * `fs.rename` is atomic on POSIX) and rename over the target, so a crash sees
 * either the old file intact or the new file complete — never an empty
 * intermediate. The temp name is per-pid + random so a concurrent CLI writer
 * (not covered by this process's writeMutex) can't collide on it; it is renamed
 * away before any git staging and is unlinked on error, so it never leaks into
 * a commit. Mirrors the CLI's markdown_io._atomic_write / sync._atomic_write_hook.
 */
async function atomicWriteFile(absPath: string, content: string): Promise<void> {
  const dir = path.dirname(absPath);
  const tmpPath = path.join(
    dir,
    `.${path.basename(absPath)}.${process.pid}.${randomBytes(6).toString("hex")}.tmp`
  );
  try {
    await fs.writeFile(tmpPath, content, "utf-8");
    // Preserve the target's existing mode across the rename. fs.writeFile created
    // the temp with the default (0666 & ~umask, ~0644), so an existing note with
    // tighter or looser perms would otherwise be silently reset — a change git
    // can't surface (it tracks only the exec bit). Best-effort: a brand-new note
    // (no prior file) keeps the default create mode, matching the old writeFile.
    try {
      const { mode } = await fs.stat(absPath);
      await fs.chmod(tmpPath, mode);
    } catch {
      /* target doesn't exist yet (new note) — keep the default create mode */
    }
    await fs.rename(tmpPath, absPath);
  } catch (e) {
    try {
      await fs.unlink(tmpPath);
    } catch {
      /* temp file may not exist if writeFile itself failed — ignore */
    }
    throw e;
  }
}

export async function writeNote(
  vaultRoot: string,
  relPath: string,
  content: string,
  commitTitle?: string,
  owner?: string,
  opts?: { exclusive?: boolean }
): Promise<WriteResult> {
  assertPathSafe(vaultRoot, relPath);
  // Prefer the caller-supplied title — gray-matter folds long or special-char
  // titles to `>-` / `|-`, so regex-parsing the rendered YAML produced commit
  // messages like "write >- — via MCP" (issue #104). Fall back to relPath
  // when no title is provided.
  const rawTitle = commitTitle && commitTitle.trim().length > 0 ? commitTitle : relPath;
  const title = sanitizeCommitTitle(rawTitle);
  // exclusive: write with O_EXCL ("wx") so the create FAILS with EEXIST if the
  // path already exists on the checked-out write branch. This is the ONLY
  // race-safe collision check for create_note (#408): a pre-lock existence
  // probe is a TOCTOU — two concurrent same-title creates both see the path
  // free, then serialize inside the mutex and the second truncates the first.
  // With O_EXCL the loser's write throws EEXIST inside the lock and create_note
  // retries the next candidate. Also correct across branch skew: the check now
  // runs post-checkout, against the write branch the note actually lands on,
  // not whatever branch happened to be checked out when the path was chosen.
  return withWriteLock(
    vaultRoot,
    relPath,
    `feat(schist): write ${title} — ${attribution(owner)}`,
    async (absPath) => {
      if (opts?.exclusive) {
        // O_EXCL create — the race-safe collision check (#408). This MUST stay a
        // direct write: a temp-file + rename would silently clobber an existing
        // note and defeat the EEXIST guard create_note relies on. A fresh create
        // has no prior content to lose to a mid-write kill, so the truncation
        // window that atomicWriteFile closes (#427) does not apply here.
        await fs.writeFile(absPath, content, { encoding: "utf-8", flag: "wx" });
      } else {
        await atomicWriteFile(absPath, content);
      }
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
      await atomicWriteFile(absPath, newContent);
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
      // Symlink/containment guard runs in withWriteLock, post-checkout,
      // before this callback (#323).
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
      await atomicWriteFile(absPath, transform(current));
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
    // Same option-injection hardening as withWriteLock: trailing "--" is
    // universal, and NO --end-of-options — `git checkout` only honors that flag
    // after 2.43 (fails on ≤2.43), so with endOfOptionsArgs()'s ≥2.24 gate it
    // broke every delete on the common HPC git 2.43.x (#444).
    // assertValidWriteBranch is the real, version-independent defense.
    await git(vaultRoot, ["checkout", branch, "--"]);
    await assertAttachedToWriteBranch(vaultRoot, branch);

    try {
      await git(vaultRoot, ["rm", "--quiet", "--", relPath]);

      for (const r of repairs) {
        // Post-checkout symlink/containment guard before writing through the
        // repair path (a write-branch symlink could redirect outside the vault;
        // the `git rm` of the delete target above is safe — it removes the link
        // rather than writing through it). #119.
        await assertResolvesInside(vaultRoot, r.relPath);
        const absPath = path.resolve(vaultRoot, r.relPath);
        await atomicWriteFile(absPath, r.content);
        await git(vaultRoot, ["add", "--", r.relPath]);
      }

      // NEVER --no-verify — hard coded out (mirrors withWriteLock). Commit
      // runs the synchronous post-commit ingest hook, so it needs the larger
      // ceiling — the 30s default killed deletes on large vaults (#324).
      //
      // As in withWriteLock: the ref moves BEFORE post-commit runs, so a
      // timeout during a slow hook can kill a commit that already landed.
      // Without the HEAD re-check, that case fell through to the rollback
      // below, whose `git restore --source=HEAD -- <path>` referenced the NEW
      // HEAD (which no longer has the path), failed, and was swallowed —
      // while the caller was told the delete failed (#336). Now the rollback
      // only runs when the commit genuinely did not land, which is exactly
      // when `--source=HEAD` is correct.
      const preCommitHead = await git(vaultRoot, ["rev-parse", "HEAD"]);
      let commitWarning: string | undefined;
      try {
        await git(
          vaultRoot,
          ["commit", "-m", `feat(schist): delete ${title} — ${attribution(owner)}`],
          GIT_COMMIT_TIMEOUT_MS,
        );
      } catch (e) {
        if (!isGitTimeout(e)) throw e;
        let headNow: string | null = null;
        try {
          headNow = await git(vaultRoot, ["rev-parse", "HEAD"]);
        } catch {
          // Unreadable HEAD — treat as "commit did not land"; outer rollback runs.
        }
        // A rev-parse FAILURE here (headNow === null) is deliberately treated
        // as "did not land" — the conservative direction: rethrow GIT_TIMEOUT
        // and run the rollback rather than claim a success we can't confirm.
        // The false-negative (delete actually committed but HEAD was
        // momentarily unreadable) self-corrects: a retried deleteNote hits
        // `git rm` on a now-untracked path and fails fast rather than
        // double-deleting.
        if (headNow === null || headNow === preCommitHead) throw e;
        commitWarning =
          "committed; post-commit ingest still running or timed out — the index may lag this delete until the next ingest";
      }
      const sha = await git(vaultRoot, ["rev-parse", "HEAD"]);
      return {
        path: relPath,
        commitSha: sha,
        committed: true,
        ...(commitWarning !== undefined ? { commitWarning } : {}),
      };
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
