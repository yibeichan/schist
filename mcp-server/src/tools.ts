import * as fs from "fs/promises";
import { readFileSync } from "fs";
import * as path from "path";
import { fileURLToPath } from "url";
import { spawn } from "child_process";
import { load as yamlLoad } from "js-yaml";
import * as sqliteReader from "./sqlite-reader.js";
import { writeNote } from "./git-writer.js";
import { buildNote, buildConnectionLine } from "./markdown-parser.js";
import { validateOwner, resolveActiveOwner } from "./agent-identity.js";
import type { VaultConfig, ToolError, SearchMemoryResponse, SearchNotesResponse, QueryGraphResponse, ListConceptsResponse, GetContextResponse, SyncRetryResponse, SyncStatusResponse, ComposeBriefResponse, Note, SearchResult } from "./types.js";
import { loadVaultAcl, canWrite, deriveScope, resolveAclIdentity } from "./vault-acl.js";
import {
  canonicalizeQueryHash,
  decodeCursor,
  issueCursor,
  recordIssued,
  checkRefusal,
  parseVerbose,
  logVerbose,
  noteHighFrequency,
  snippetContent,
} from "./protocol/index.js";

// ---------------------------------------------------------------------------
// Canonical directories — derived from cli/schist/default.yaml at runtime.
// Fail-open: if the file is unreadable, the baked-in mirror keeps the server
// running. A drift test in tests/tools.test.ts keeps the mirror in sync.
// ---------------------------------------------------------------------------

// Baked-in mirror of cli/schist/default.yaml's `directories:` values. Kept in
// sync by the `default.yaml drift detection` describe block in tests/tools.test.ts.
// Used when the canonical file is unreadable at startup (e.g. broken install)
// so the MCP server stays up — fail-open. Asymmetric with cli/schist/acl.py
// (rate_limit.py), which fails closed; see spec 2026-05-24-flatten-spoke-dirs.
export const DEFAULT_DIRECTORIES_FALLBACK = [
  "notes", "papers", "concepts",
  "research", "decisions", "ops", "projects", "logs",
] as const;

let _canonicalDirsCache: readonly string[] | null = null;

// Test-only: clear the canonical-directories cache so a test that simulates an
// unreadable default.yaml (fail-open → baked-in fallback) can't poison the
// cache for later tests in the same module. Mirrors resetSpokePushTrackerForTesting.
export function resetCanonicalDirsCacheForTesting(): void {
  _canonicalDirsCache = null;
}

function loadCanonicalDirectories(): readonly string[] {
  if (_canonicalDirsCache !== null) return _canonicalDirsCache;
  try {
    // tools.ts compiles to mcp-server/dist/tools.js; cli/schist/default.yaml
    // lives at <repo>/cli/schist/default.yaml. dist/ is one level under
    // mcp-server/, which is one level under the repo root, so:
    //   import.meta.url → file://<repo>/mcp-server/dist/tools.js
    //   __dirname        → <repo>/mcp-server/dist
    //   ../../           → <repo>
    const __dirname_here = path.dirname(fileURLToPath(import.meta.url));
    // NOTE: this relative path only resolves inside the monorepo tree
    // (mcp-server/dist → ../../cli/schist/default.yaml). If @schist/mcp-server
    // is ever published standalone to npm, cli/schist/ won't be present and
    // this read fails — at which point the fail-open path below uses the
    // baked-in DEFAULT_DIRECTORIES_FALLBACK. That degradation is correct, just
    // noisier (a stderr warning on every startup).
    const canonicalPath = path.resolve(__dirname_here, "..", "..", "cli", "schist", "default.yaml");
    const raw = yamlLoad(readFileSync(canonicalPath, "utf-8")) as Record<string, unknown>;
    const dirs = raw?.directories as Record<string, string> | undefined;
    if (dirs && typeof dirs === "object") {
      _canonicalDirsCache = Object.values(dirs).map((v) => v.replace(/\/$/, ""));
      return _canonicalDirsCache;
    }
    console.warn(
      `schist: cli/schist/default.yaml at ${canonicalPath} is missing the ` +
      `'directories:' mapping. Using baked-in fallback.`,
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.warn(
      `schist: cli/schist/default.yaml unreadable (${msg}); ` +
      `using baked-in fallback.`,
    );
  }
  _canonicalDirsCache = [...DEFAULT_DIRECTORIES_FALLBACK];
  return _canonicalDirsCache;
}

function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .replace(/\s+/g, "-")
      .replace(/-+/g, "-")
      .trim() || "untitled"
  );
}

/** Returns the raw slug without the "untitled" fallback — used to detect empty-slug titles */
function rawSlug(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .trim();
}

function today(): string {
  return new Date().toISOString().split("T")[0];
}

/**
 * Normalise any caught value into a plain ToolError object so that
 * JSON.stringify always produces { error, message } — Error.message is
 * non-enumerable and would otherwise be silently dropped.
 */
function normalizeError(e: unknown, fallbackCode = "GIT_ERROR"): ToolError {
  // Already a plain ToolError shape (thrown by git-writer / assertPathSafe)
  if (
    e !== null &&
    typeof e === "object" &&
    "error" in e &&
    "message" in e &&
    typeof (e as Record<string, unknown>).error === "string"
  ) {
    const te = e as Record<string, unknown>;
    return { error: te.error as string, message: te.message as string, details: te.details };
  }
  // Real Error instance — .message is non-enumerable, must be lifted explicitly
  if (e instanceof Error) {
    const extra = e as Error & { error?: string };
    return {
      error: extra.error ?? fallbackCode,
      message: e.message,
      details: { stack: e.stack },
    };
  }
  return { error: fallbackCode, message: String(e) };
}

export async function loadVaultConfig(vaultRoot: string): Promise<VaultConfig> {
  const configPath = path.join(vaultRoot, "schist.yaml");
  const content = await fs.readFile(configPath, "utf-8");

  // Use js-yaml instead of hand-rolled regexes: handles inline comments,
  // quoted strings with ":", multiline values, and all valid YAML.
  const raw = yamlLoad(content) as Record<string, unknown>;

  const getString = (key: string, def: string): string => {
    const v = raw[key];
    return typeof v === "string" ? v.trim() : def;
  };

  const getStringList = (key: string, def: string[]): string[] => {
    const v = raw[key];
    if (Array.isArray(v)) return v.map(String).filter(Boolean);
    return def;
  };

  return {
    name: getString("name", path.basename(vaultRoot)),
    path: vaultRoot,
    directories: getStringList("directories", [...loadCanonicalDirectories()]),
    connectionTypes: getStringList("connection_types", [
      "extends", "contradicts", "supports", "replicates",
      "applies-method-of", "reinterprets", "related",
    ]),
    statuses: getStringList("statuses", ["draft", "review", "final", "archived"]),
    writeBranch: getString("write_branch", "drafts"),
  };
}

export function triggerIngestion(vaultRoot: string): void {
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");

  // Spawn the `schist-ingest` console script registered by the schist
  // CLI package (cli/pyproject.toml). Works for both `pip install schist`
  // and `pip install -e ./cli` setups; ENOENTs cleanly if the CLI was
  // never installed (in which case the post-commit hook also can't run).
  // Honours SCHIST_INGEST_BIN env (#123) for operators pinning a version.
  const child = spawn(schistCliBin("schist-ingest"), ["--vault", vaultRoot, "--db", dbPath], {
    cwd: vaultRoot,
    stdio: "ignore",
  });
  child.unref();
  child.on("error", (err) => {
    console.error("[schist] ingestion failed:", err);
  });
}

const SYNC_ERROR_SENTINEL = ".schist/last-sync-error";
const SYNC_RETRY_TIMEOUT_MS = 30_000;
const SYNC_STATUS_TIMEOUT_MS = 10_000;

/**
 * Write a sync-failure sentinel so agents have a visible trace when a
 * background push silently fails. `readSyncWarning` surfaces it on reads and
 * blocks subsequent write tools until successful sync recovery clears it.
 */
async function writeSyncError(vaultRoot: string, message: string): Promise<void> {
  try {
    const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
    await fs.mkdir(path.dirname(sentinelPath), { recursive: true });
    const entry = `${new Date().toISOString()} ${message}\n`;
    // Atomic write (#124): tmp + rename so concurrent readers never observe
    // a zero-byte truncate-in-progress state. Uniquified by pid+timestamp
    // so two concurrent writers don't clobber each other's tmp file (only
    // the rename target races, and POSIX rename is atomic). After the
    // rename both writers' content is consistent — last write wins, which
    // matches the pre-#124 fs.writeFile semantics; the gain is just
    // crash-safety for readers.
    const tmpPath = `${sentinelPath}.tmp-${process.pid}-${Date.now()}`;
    await fs.writeFile(tmpPath, entry);
    await fs.rename(tmpPath, sentinelPath);
  } catch {
    // Can't write the sentinel either — truly nothing we can do.
  }
}

/**
 * Sanitize sentinel content before embedding it in an agent-facing warning.
 * Strips non-printable / control characters (which could include ANSI
 * escapes or fake newlines an attacker with vault write might use to steer
 * an agent's instruction-following), and caps length at 500 chars to bound
 * response size. The sentinel is normally written by writeSyncError with
 * a small ISO timestamp + message, so legitimate content is unaffected.
 */
function sanitizeSentinelContent(raw: string): string {
  const cleaned = raw.replace(/[^\x20-\x7e\t\n]/g, "?").trim();
  return cleaned.length > 500 ? cleaned.slice(0, 500) + "…" : cleaned;
}

function parseSyncErrorText(sanitized: string): { timestamp?: string; contents: string } {
  const match = sanitized.match(/^(\S+)\s+([\s\S]*)$/);
  const timestamp = match?.[1]?.match(/^\d{4}-\d{2}-\d{2}T/) ? match[1] : undefined;
  return { timestamp, contents: timestamp ? (match?.[2] ?? "") : sanitized };
}

function formatSyncErrorAge(timestamp?: string): string | undefined {
  if (!timestamp) return undefined;
  const then = Date.parse(timestamp);
  if (!Number.isFinite(then)) return undefined;
  const ageMs = Date.now() - then;
  if (ageMs < 0) return "less than 1 minute ago";
  const minutes = Math.floor(ageMs / 60_000);
  if (minutes < 1) return "less than 1 minute ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

async function readSyncWarning(vaultRoot: string): Promise<string | undefined> {
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  let rawText: string;
  try {
    rawText = await fs.readFile(sentinelPath, "utf-8");
  } catch (e: unknown) {
    // ENOENT is the healthy case — no sentinel means no recent failure.
    // Any other error (EACCES on permission flip, EISDIR if something
    // mkdir'd over the sentinel path) is itself a sync-detection problem
    // worth surfacing. Without the distinction we'd silently report
    // "healthy" while sync detection is actually broken.
    const code = (e as NodeJS.ErrnoException).code;
    if (code === "ENOENT") return undefined;
    return `Sync-failure sentinel exists but is unreadable (${code ?? "unknown error"}). Sync state may be diverging silently.`;
  }
  const errText = sanitizeSentinelContent(rawText);
  if (!errText) return undefined;
  const parsed = parseSyncErrorText(errText);
  const age = formatSyncErrorAge(parsed.timestamp);
  const ageClause = age ? ` Sync failed ${age}.` : "";
  // Descriptive phrasing (not imperative). A dirty sentinel is cleared only
  // by successful sync recovery, not by context reads.
  return `Recent background sync failure:${ageClause} ${parsed.contents}. Writes may not have reached the hub. \`sync_status\` reports divergence and \`sync_retry\` can retry and clear this state.`;
}

async function blockWriteIfSyncDirty(vaultRoot: string): Promise<ToolError | null> {
  // Only spokes can diverge from a hub, and only spokes have a recovery path:
  // both `sync_retry` and `triggerSpokePush` are spoke-gated. Blocking a
  // non-spoke (e.g. a demoted vault or env-drift to a folder carrying a stale
  // sentinel) would refuse writes with no way to clear them — a permanent
  // deadlock. A standalone vault has no hub, so the divergence rationale
  // doesn't apply; never block it.
  if (!(await isSpokeVault(vaultRoot))) return null;
  const syncWarning = await readSyncWarning(vaultRoot);
  if (syncWarning === undefined) return null;
  return {
    error: "SYNC_DIRTY",
    message:
      `${syncWarning} Refusing this write to avoid compounding spoke/hub divergence. ` +
      "Run `sync_retry` after checking `sync_status`; writes resume after a successful push clears the sync error. " +
      "If recovery keeps failing, remove `.schist/last-sync-error` manually only as a last resort.",
  };
}

async function readSyncErrorState(vaultRoot: string): Promise<SyncStatusResponse["last_sync_error"] & { mtimeMs: number } | null> {
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  let stat;
  try {
    stat = await fs.stat(sentinelPath);
  } catch {
    return null;
  }

  try {
    const rawText = await fs.readFile(sentinelPath, "utf-8");
    const sanitized = sanitizeSentinelContent(rawText);
    if (!sanitized) return null;
    const { timestamp, contents } = parseSyncErrorText(sanitized);
    return { timestamp, contents, mtimeMs: stat.mtimeMs };
  } catch (e: unknown) {
    const code = (e as NodeJS.ErrnoException).code;
    return {
      contents: `Sync-failure sentinel exists but is unreadable (${code ?? "unknown error"})`,
      mtimeMs: stat.mtimeMs,
    };
  }
}

async function clearSyncErrorIfUnchanged(vaultRoot: string, originalMtimeMs: number | null): Promise<boolean> {
  if (originalMtimeMs === null) return false;
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  try {
    const stat = await fs.stat(sentinelPath);
    if (stat.mtimeMs !== originalMtimeMs) return false;
    await fs.rm(sentinelPath, { recursive: true, force: true });
    return true;
  } catch {
    return false;
  }
}

/**
 * Validates and normalizes a `limit` argument from a cursor-adopting tool
 * (#108). Accepts a value the JSON-schema layer already rejected (defense
 * in depth — the registry now declares `{ type: "integer", minimum: 1 }`
 * so well-behaved clients never reach this fallback). Handles:
 *
 *   - non-numeric (string from sloppy client, boolean, null, etc) → default
 *   - NaN / ±Infinity → default
 *   - fractional → truncate via Math.trunc, then clamp
 *   - zero / negative → default (matches canonicalize's `limit: 0` collapse)
 *   - >cap → clamp to cap
 *
 * Pre-#108 the handlers only checked `requested <= 0`, which left
 * stringified numbers (`"50" <= 0 === false`) flowing into Math.min and
 * triggering implicit string concat downstream — corrupting offset math
 * and cursor offsets. See #108 for the full failure mode.
 */
function validateLimit(requested: unknown, defaultVal: number, cap: number): number {
  if (typeof requested !== "number" || !Number.isFinite(requested)) return defaultVal;
  const n = Math.trunc(requested);
  if (n <= 0) return defaultVal;
  return Math.min(n, cap);
}

/**
 * Resolves which schist CLI binary to spawn. Parameterized so operators can
 * pin EACH of the two CLI binaries (`schist` for sync, `schist-ingest` for
 * post-commit indexing) independently — useful on hosts that have multiple
 * installs via `uv tool install` / `pipx`. (#123)
 *
 * Defaults to the binary name on PATH (what every install method produces).
 *
 * Why not `python3 -m schist`: under `uv tool install` and `pipx`, the
 * CLI ships in an isolated venv so the console-script is on PATH but the
 * `schist` module is NOT importable from the default `python3`. `python3
 * -m schist` then fails with ModuleNotFoundError and the failure surfaces
 * silently via the .schist/last-sync-error sentinel — see #120 for the
 * specific incident that motivated using the console-scripts here.
 *
 * **Version-coherence warning:** if an operator pins one binary via env
 * but not the other, sync and ingest may run different schist versions —
 * commits go through one version's schema while SQLite gets rebuilt by
 * another. CHANGELOG documents the coherence requirement; we don't
 * enforce it here because the parallel-version testing case is legitimate.
 *
 * `?.trim() || ...` (not `??`) so an exported-but-empty env var (e.g.
 * `SCHIST_BIN=""`) falls back to the default rather than spawning "" and
 * ENOENT.
 */
function schistCliBin(binName: "schist" | "schist-ingest"): string {
  const envVar = binName === "schist-ingest" ? "SCHIST_INGEST_BIN" : "SCHIST_BIN";
  return process.env[envVar]?.trim() || binName;
}

/**
 * Tracks vaults with an in-flight `schist sync push` child so concurrent
 * write tools coalesce instead of spawning N competing pushes (#122). A
 * write-heavy session (e.g. distillation runs that produce 20+ rapid
 * `create_note` calls) used to spawn 20 detached pushes — first grabs
 * `.git/index.lock`, rest fail with lock contention, each writes a fresh
 * sentinel and the agent sees an oscillating warning loop.
 *
 * The in-flight push will naturally batch any commits that landed while
 * it was running (`git push` sends current `HEAD`), so coalescing here
 * doesn't lose data. After the push exits, the next write tool's call to
 * `triggerSpokePush` spawns a fresh push for whatever's still ahead of
 * the hub. This trades the "every write triggers its own push" property
 * for "no spawn storms"; the timing slip is bounded by a single push's
 * runtime.
 */
type SyncCommandOutcome = {
  ok: boolean;
  code?: number | null;
  signal?: NodeJS.Signals | null;
  error?: string;
  stdout?: string;
  stderr?: string;
  timedOut?: boolean;
};

function truncateOutput(s: string, cap = 12_000): string {
  return s.length > cap ? s.slice(0, cap) + "…" : s;
}

async function runCommand(
  command: string,
  args: string[],
  opts: { cwd: string; timeoutMs: number; env?: NodeJS.ProcessEnv; capture?: boolean }
): Promise<SyncCommandOutcome> {
  return await new Promise<SyncCommandOutcome>((resolve) => {
    const child = spawn(command, args, {
      cwd: opts.cwd,
      env: opts.env ?? process.env,
      detached: true,
      stdio: opts.capture ? ["ignore", "pipe", "pipe"] : "ignore",
    });
    let stdout = "";
    let stderr = "";
    let settled = false;

    const finish = (outcome: SyncCommandOutcome): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({
        ...outcome,
        stdout: opts.capture ? truncateOutput(stdout) : undefined,
        stderr: opts.capture ? truncateOutput(stderr) : undefined,
      });
    };

    child.stdout?.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf-8"); });
    child.stderr?.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf-8"); });

    const killGroup = (sig: NodeJS.Signals): void => {
      if (child.pid === undefined) return;
      try { process.kill(-child.pid, sig); } catch { /* already dead */ }
    };
    const timer = setTimeout(() => {
      killGroup("SIGTERM");
      setTimeout(() => killGroup("SIGKILL"), 500);
      finish({ ok: false, timedOut: true, error: `timed out after ${opts.timeoutMs}ms` });
    }, opts.timeoutMs);

    child.on("error", (err) => {
      finish({ ok: false, error: err.message });
    });
    child.on("exit", (code, signal) => {
      finish({ ok: code === 0, code, signal });
    });
    child.on("close", (code, signal) => {
      finish({ ok: code === 0, code, signal });
    });
  });
}

function runSchistSync(
  vaultRoot: string,
  action: "pull" | "push",
  timeoutMs = SYNC_RETRY_TIMEOUT_MS,
  force = false,
): Promise<SyncCommandOutcome> {
  const args = ["--vault", vaultRoot, "sync", action];
  if (force) args.push("--force");
  return runCommand(
    schistCliBin("schist"),
    args,
    { cwd: vaultRoot, timeoutMs, env: process.env, capture: true },
  );
}

const inFlightSpokePushes = new Map<string, Promise<SyncCommandOutcome>>();

async function hasStaleGitOperation(vaultRoot: string): Promise<boolean> {
  const gitDir = path.join(vaultRoot, ".git");
  const sentinels = [
    path.join(gitDir, "rebase-merge"),
    path.join(gitDir, "rebase-apply"),
    path.join(gitDir, "MERGE_HEAD"),
    path.join(gitDir, "index.lock"),
  ];
  for (const sentinel of sentinels) {
    try {
      await fs.access(sentinel);
      return true;
    } catch {
      // absent is healthy for this sentinel
    }
  }
  return false;
}

function pushFailureMessage(outcome: SyncCommandOutcome): string | null {
  if (outcome.error && outcome.code === undefined && outcome.signal === undefined && !outcome.timedOut) {
    return `push spawn failed: ${outcome.error}`;
  }
  if (outcome.timedOut) return "push timed out after 30000ms";
  if (outcome.code !== null && outcome.code !== undefined && outcome.code !== 0) {
    return `push exited with code ${outcome.code}`;
  }
  if (outcome.code === null && outcome.signal) {
    return `push killed by signal ${outcome.signal}`;
  }
  return null;
}

/** Fire-and-forget spoke push after a write. No-op for non-spoke vaults. */
export function triggerSpokePush(vaultRoot: string): void {
  // Coalesce: if a push for this vault is already running, the next
  // commit will be picked up by that in-flight child — skip spawn.
  if (inFlightSpokePushes.has(vaultRoot)) return;

  const spokeConfig = path.join(vaultRoot, ".schist", "spoke.yaml");
  fs.access(spokeConfig).then(() => {
    // Re-check inside the .then in case a concurrent caller raced through
    // the synchronous check above and we beat them to fs.access. Cheap.
    if (inFlightSpokePushes.has(vaultRoot)) return;

    const pushPromise = (async () => {
      const sentinelBeforePush = await readSyncErrorState(vaultRoot);
      let outcome = await runCommand(
        schistCliBin("schist"),
        ["--vault", vaultRoot, "sync", "push"],
        { cwd: vaultRoot, timeoutMs: SYNC_RETRY_TIMEOUT_MS, env: process.env, capture: false },
      );

      let failure = pushFailureMessage(outcome);
      if (failure !== null && await hasStaleGitOperation(vaultRoot)) {
        console.error("[schist] spoke push failed with stale git state; retrying with sync push --force");
        const retry = await runSchistSync(vaultRoot, "push", SYNC_RETRY_TIMEOUT_MS, true);
        if (retry.ok) {
          outcome = retry;
          failure = null;
        } else {
          outcome = retry;
          failure = `push failed after stale-state cleanup retry: ${outcomeMessage(retry)}`;
        }
      }

      if (failure === null) {
        await clearSyncErrorIfUnchanged(vaultRoot, sentinelBeforePush?.mtimeMs ?? null);
      } else {
        await writeSyncError(vaultRoot, failure);
      }
      return outcome;
    })().finally(() => {
      inFlightSpokePushes.delete(vaultRoot);
    });
    inFlightSpokePushes.set(vaultRoot, pushPromise);
  }).catch(() => {
    // Not a spoke vault — silent no-op
  });
}

/** Test-only: clear the in-flight push tracker. */
export function resetSpokePushTrackerForTesting(): void {
  inFlightSpokePushes.clear();
}

/**
 * Pull from hub before a read, with a hard timeout. Falls through silently on
 * failure so a flaky hub never blocks an agent read. Awaited but bounded.
 */
export async function maybeSpokePull(vaultRoot: string, timeoutMs = 5000): Promise<void> {
  const spokeConfig = path.join(vaultRoot, ".schist", "spoke.yaml");
  try {
    await fs.access(spokeConfig);
  } catch {
    return; // Not a spoke
  }
  await new Promise<void>((resolve) => {
    const child = spawn(
      schistCliBin("schist"),
      ["--vault", vaultRoot, "sync", "pull"],
      { cwd: vaultRoot, stdio: "ignore", env: process.env, detached: true }
    );
    // `detached: true` puts the child in its own process group. On timeout we
    // must signal the whole group (negative PID) — child.kill() only signals
    // the schist CLI process, leaving git-fetch/git-rebase grandchildren alive
    // with a live .git/index.lock. SIGTERM first, then SIGKILL after a short
    // grace in case git ignores SIGTERM mid-rebase.
    const killGroup = (sig: NodeJS.Signals): void => {
      if (child.pid === undefined) return;
      try { process.kill(-child.pid, sig); } catch { /* already dead */ }
    };
    const timer = setTimeout(() => {
      killGroup("SIGTERM");
      setTimeout(() => killGroup("SIGKILL"), 500);
      resolve();
    }, timeoutMs);
    child.on("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    child.on("error", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

async function isSpokeVault(vaultRoot: string): Promise<boolean> {
  try {
    await fs.access(path.join(vaultRoot, ".schist", "spoke.yaml"));
    return true;
  } catch {
    return false;
  }
}

async function runGit(vaultRoot: string, args: string[], timeoutMs = SYNC_STATUS_TIMEOUT_MS): Promise<SyncCommandOutcome> {
  return runCommand("git", args, { cwd: vaultRoot, timeoutMs, env: process.env, capture: true });
}

function outcomeMessage(outcome: SyncCommandOutcome): string {
  const output = [outcome.stderr, outcome.stdout, outcome.error].filter(Boolean).join("\n").trim();
  if (output) return output;
  if (outcome.timedOut) return "command timed out";
  if (outcome.signal) return `command killed by signal ${outcome.signal}`;
  return `command exited with code ${outcome.code ?? "unknown"}`;
}

function ensureStringArray(value: unknown, field: string): string[] | ToolError {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    return { error: "VALIDATION_ERROR", message: `${field} must be an array of strings` };
  }
  return value;
}

function oneLine(text: string, cap = 180): string {
  const cleaned = text
    .replace(/^---[\s\S]*?---\s*/m, "")
    .replace(/[#>*_`[\]()-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!cleaned) return "";
  return cleaned.length > cap ? cleaned.slice(0, cap - 1).trimEnd() + "…" : cleaned;
}

function matchesScopePath(id: string, scope: string[]): boolean {
  if (scope.length === 0) return true;
  return scope.some((prefix) => id === prefix || id.startsWith(`${prefix.replace(/\/$/, "")}/`));
}

function addTags(acc: Map<string, number>, tags: unknown): void {
  let parsed: unknown;
  try {
    parsed = typeof tags === "string" ? JSON.parse(tags) : tags;
  } catch {
    return;
  }
  if (!Array.isArray(parsed)) return;
  for (const tag of parsed) {
    if (typeof tag === "string" && tag.trim()) {
      acc.set(tag, (acc.get(tag) ?? 0) + 1);
    }
  }
}

type BriefNote = ComposeBriefResponse["related_notes"][number] & {
  tags: string[];
  annotation: string;
};

function briefNoteFromSearch(note: SearchResult, reason: string): BriefNote {
  return {
    id: note.id,
    title: note.title,
    reason,
    tags: note.tags,
    annotation: oneLine(note.snippet) || note.title,
  };
}

function briefNoteFromNote(note: Note, reason: string): BriefNote {
  return {
    id: note.id,
    title: note.title,
    reason,
    tags: note.tags,
    annotation: oneLine(note.body) || note.title,
  };
}

async function recentAddedPaths(vaultRoot: string, scope: string[]): Promise<Array<{ path: string; commit: string }>> {
  const outcome = await runGit(vaultRoot, [
    "log",
    "--since=24 hours ago",
    "--diff-filter=A",
    "--name-only",
    "--pretty=format:commit:%h",
  ], 2_000);
  if (!outcome.ok || !outcome.stdout) return [];

  const rows: Array<{ path: string; commit: string }> = [];
  let commit = "";
  for (const raw of outcome.stdout.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("commit:")) {
      commit = line.slice("commit:".length);
      continue;
    }
    if (matchesScopePath(line, scope)) rows.push({ path: line, commit });
    if (rows.length >= 8) break;
  }
  return rows;
}

function isAclRejection(outcome: SyncCommandOutcome): boolean {
  const text = outcomeMessage(outcome).toLowerCase();
  return (
    text.includes("acl") ||
    text.includes("pre-receive hook declined") ||
    text.includes("push rejected by hub") ||
    text.includes("cannot determine push identity") ||
    text.includes("identity") && text.includes("rejected")
  );
}

function isRebaseConflict(outcome: SyncCommandOutcome): boolean {
  const text = outcomeMessage(outcome).toLowerCase();
  return text.includes("conflict") || text.includes("could not apply") || text.includes("rebase --abort");
}

function syncFailureResponse(
  mode: SyncRetryResponse["mode"],
  phase: SyncRetryResponse["phase"],
  outcome: SyncCommandOutcome,
): SyncRetryResponse {
  const acl = isAclRejection(outcome);
  return {
    ok: false,
    mode,
    phase,
    retriable: !acl,
    reason: acl ? "ACL violation" : (outcome.timedOut ? "Timeout" : "Command failed"),
    message: outcomeMessage(outcome),
    code: outcome.code ?? undefined,
    signal: outcome.signal ?? undefined,
    timed_out: outcome.timedOut,
  };
}

export async function sync_status(vaultRoot: string): Promise<SyncStatusResponse | ToolError> {
  try {
    const isSpoke = await isSpokeVault(vaultRoot);
    const clean = await runGit(vaultRoot, ["status", "--porcelain"], SYNC_STATUS_TIMEOUT_MS);
    const head = await runGit(vaultRoot, ["rev-parse", "--short", "HEAD"], SYNC_STATUS_TIMEOUT_MS);
    const sentinel = await readSyncErrorState(vaultRoot);

    if (!head.ok) {
      return { error: "GIT_ERROR", message: outcomeMessage(head), details: head } satisfies ToolError;
    }

    let hubHead: string | null = null;
    let ahead: number | null = null;
    let behind: number | null = null;
    let hubError: string | undefined;

    const upstream = await runGit(vaultRoot, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], SYNC_STATUS_TIMEOUT_MS);
    if (upstream.ok) {
      const fetch = await runGit(vaultRoot, ["fetch", "--quiet"], SYNC_STATUS_TIMEOUT_MS);
      if (fetch.ok) {
        const remoteHead = await runGit(vaultRoot, ["rev-parse", "--short", "@{u}"], SYNC_STATUS_TIMEOUT_MS);
        const aheadCount = await runGit(vaultRoot, ["rev-list", "--count", "@{u}..HEAD"], SYNC_STATUS_TIMEOUT_MS);
        const behindCount = await runGit(vaultRoot, ["rev-list", "--count", "HEAD..@{u}"], SYNC_STATUS_TIMEOUT_MS);
        if (remoteHead.ok) hubHead = remoteHead.stdout?.trim() || null;
        if (aheadCount.ok) ahead = Number.parseInt(aheadCount.stdout?.trim() ?? "", 10);
        if (behindCount.ok) behind = Number.parseInt(behindCount.stdout?.trim() ?? "", 10);
        if (!Number.isFinite(ahead)) ahead = null;
        if (!Number.isFinite(behind)) behind = null;
      } else {
        hubError = outcomeMessage(fetch);
      }
    } else {
      hubError = outcomeMessage(upstream);
    }

    return {
      is_spoke: isSpoke,
      spoke_head: head.stdout?.trim() ?? "",
      hub_head: hubHead,
      ahead,
      behind,
      last_sync_error: sentinel ? { timestamp: sentinel.timestamp, contents: sentinel.contents } : null,
      clean_working_tree: clean.ok ? (clean.stdout ?? "").trim().length === 0 : false,
      hub_error: hubError,
    };
  } catch (e: unknown) {
    return normalizeError(e);
  }
}

export async function sync_retry(
  vaultRoot: string,
  args: { owner?: string; mode?: unknown },
): Promise<SyncRetryResponse | ToolError> {
  try {
    validateOwner(args.owner as string);
    if (args.mode !== "push-only" && args.mode !== "pull-rebase-push") {
      return {
        error: "INVALID_ARG",
        message: "sync_retry requires mode='push-only' or mode='pull-rebase-push'",
      } satisfies ToolError;
    }
    const mode = args.mode;
    if (!(await isSpokeVault(vaultRoot))) {
      return { error: "INVALID_ARG", message: "sync_retry is only available for spoke vaults" } satisfies ToolError;
    }

    const sentinel = await readSyncErrorState(vaultRoot);
    const originalMtimeMs = sentinel?.mtimeMs ?? null;
    const inFlight = inFlightSpokePushes.get(vaultRoot);
    if (inFlight) {
      const outcome = await inFlight;
      if (outcome.ok) {
        return {
          ok: true,
          mode,
          phase: "push",
          retriable: false,
          message: "Existing in-flight push completed successfully.",
          cleared_last_sync_error: await clearSyncErrorIfUnchanged(vaultRoot, originalMtimeMs),
          awaited_in_flight: true,
        };
      }
      return { ...syncFailureResponse(mode, "push", outcome), awaited_in_flight: true };
    }

    if (mode === "pull-rebase-push") {
      const pull = await runSchistSync(vaultRoot, "pull", SYNC_RETRY_TIMEOUT_MS);
      if (!pull.ok) {
        if (isRebaseConflict(pull)) {
          await runGit(vaultRoot, ["rebase", "--abort"], 5_000);
          return {
            ...syncFailureResponse(mode, "pull-rebase", pull),
            retriable: false,
            reason: "Rebase conflict",
          };
        }
        return syncFailureResponse(mode, "pull-rebase", pull);
      }
    }

    const push = await runSchistSync(vaultRoot, "push", SYNC_RETRY_TIMEOUT_MS);
    if (!push.ok) {
      await writeSyncError(vaultRoot, `retry push failed: ${outcomeMessage(push)}`);
      return syncFailureResponse(mode, "push", push);
    }

    return {
      ok: true,
      mode,
      phase: "push",
      retriable: false,
      message: "Sync retry completed successfully.",
      cleared_last_sync_error: await clearSyncErrorIfUnchanged(vaultRoot, originalMtimeMs),
    };
  } catch (e: unknown) {
    return normalizeError(e);
  }
}

/**
 * search_notes tool handler. Runs the cursor pipeline:
 *
 *   canonicalizeQueryHash → (cursor decode + binding OR identical-query
 *   refusal) → SQL fetch (limit+1, with id-ASC tiebreaker in sqlite-reader)
 *   → recordIssued + issueCursor on capped results → { results, cursor? }.
 *
 * No verbose mode — per spec, full bodies are obtained via `get_note`, which
 * is already an explicit two-step protocol. The FTS5 `snippet()` column on
 * each row is fixed-size and adequate for the search-result surface.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function search_notes(
  vaultRoot: string,
  args: {
    query: string;
    limit?: number;
    status?: string;
    tags?: string[];
    scope?: string;
    owner?: string;
    confidence?: "low" | "medium" | "high";
    cursor?: string;
  }
): Promise<SearchNotesResponse | ToolError> {
  const TOOL_NAME = "search_notes" as const;

  if (args.confidence !== undefined && !["low", "medium", "high"].includes(args.confidence)) {
    return {
      error: "VALIDATION_ERROR",
      message: `confidence must be one of: low, medium, high (got "${args.confidence}")`,
    };
  }

  // Step 1: canonicalizeQueryHash. resolveActiveOwner threads per-call
  // `args.owner` first (sqlite-reader's scope=inherit resolution order),
  // then env (NAME → ID → ""). Unified across all 5 cursor handlers via #115.
  const activeOwner = resolveActiveOwner(args.owner);
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 2: Cursor decoding + queryHash binding check. Spec: "Cursor binding
  // to queryHash" — current call's computed queryHash MUST equal the cursor's
  // encoded queryHash. Mismatch → CURSOR_QUERY_MISMATCH (distinct from
  // CURSOR_INVALID_SIGNATURE, which is HMAC-fail = secret rotated on restart).
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_QUERY_MISMATCH",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 3: Identical-query refusal (only when no cursor was presented).
  // verboseEnabled is always false here — search_notes has no verbose mode.
  // vaultRoot is part of the LRU key (#113) so multi-vault deployments don't
  // see one vault's refusal block another vault's identical query.
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1 to detect hasMore. Default 20, cap 100.
  // validateLimit (#108) handles non-numeric / NaN / fractional / out-of-range.
  const effectiveLimit = validateLimit(args.limit, 20, 100);

  let rows: import("./types.js").SearchResult[];
  try {
    rows = sqliteReader.searchNotes(vaultRoot, args.query, {
      limit: effectiveLimit + 1,
      status: args.status,
      tags: args.tags,
      scope: args.scope,
      owner: args.owner,
      confidence: args.confidence,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  const hasMore = rows.length > effectiveLimit;
  const pageRows = hasMore ? rows.slice(0, effectiveLimit) : rows;

  // Step 5: Cursor issuance + recordIssued. Carve-out (#114): only fires
  // when this page was capped — an identical query that returned <= the
  // requested limit has no next page to capture, so refusing the same query
  // again would prevent legitimate re-reads with no UX benefit. Spec intent
  // is "blind retries that would burn context are refused"; a fully-served
  // query isn't a blind retry.
  let cursor: string | undefined;
  if (hasMore) {
    recordIssued({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled: false,
    });
    cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
  }

  const response: SearchNotesResponse = { results: pageRows };
  if (cursor !== undefined) response.cursor = cursor;
  return response;
}

export async function get_note(
  vaultRoot: string,
  args: { id: string }
): Promise<unknown> {
  try {
    const filePath = path.join(vaultRoot, args.id);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Note path is outside vault root" } satisfies ToolError;
    }

    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Note not found: ${args.id}` } satisfies ToolError;
    }

    const { parseNote } = await import("./markdown-parser.js");
    const { metadata, body, connections } = parseNote(content);
    const meta = metadata as Record<string, unknown>;

    const confidence = meta.confidence;
    const fileRef = meta.file_ref;
    return {
      id: args.id,
      title: (meta.title as string) ?? "",
      date: (meta.date as string) ?? "",
      status: (meta.status as string | null) ?? null,
      tags: Array.isArray(meta.tags) ? meta.tags : [],
      concepts: Array.isArray(meta.concepts) ? meta.concepts : [],
      body,
      connections,
      ...(confidence === "low" || confidence === "medium" || confidence === "high"
        ? { confidence }
        : {}),
      ...(typeof fileRef === "string" && fileRef ? { file_ref: fileRef } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}

export async function create_note(
  vaultRoot: string,
  args: {
    owner: string;
    title: string;
    body: string;
    tags?: string[];
    concepts?: string[];
    status?: string;
    connections?: Array<{ target: string; type: string; context?: string }>;
    directory?: string;
    confidence?: "low" | "medium" | "high";
    file_ref?: string;
  },
  config: VaultConfig
): Promise<unknown> {
  try {
    // Identity gate (#63): validate before any path / config checks so an
    // unauthorized caller can't enumerate vault config via error messages.
    // Reassign to the canonicalized owner so the trimmed form flows to
    // both source_agent and the commit subject (avoids divergence when
    // the caller sends e.g. "atwood ").
    const owner = validateOwner(args.owner);
    const syncDirty = await blockWriteIfSyncDirty(vaultRoot);
    if (syncDirty !== null) return syncDirty;
    if (args.confidence !== undefined && !["low", "medium", "high"].includes(args.confidence)) {
      return {
        error: "VALIDATION_ERROR",
        message: `confidence must be one of: low, medium, high (got "${args.confidence}")`,
      } satisfies ToolError;
    }
    const directory = args.directory ?? "notes";
    if (directory.includes("..") || path.isAbsolute(directory)) {
      return {
        error: "VALIDATION_ERROR",
        message: "Invalid directory: must be relative and not contain ..",
      } satisfies ToolError;
    }
    // Top-level segment match so callers can pass nested paths like
    // `projects/brain-states-friends` without having to enumerate every
    // subdirectory in schist.yaml. Mirrors the ACL's parent-grants-child
    // rule (see cli/schist/acl.py:_scope_matches). The `..` and absolute-
    // path guard above is what enforces safety; this check is content
    // configuration, not a security boundary.
    const topLevel = directory.split("/")[0];
    if (!config.directories.includes(topLevel)) {
      return {
        error: "VALIDATION_ERROR",
        message: `Directory "${directory}" not configured. Allowed top-level: ${config.directories.join(", ")}`,
      } satisfies ToolError;
    }

    // NFKC fold before validation/slugify so compatibility digits (fullwidth
    // `２０２６`, Arabic-Indic `٢٠٢٦`) that the user clearly intended as a date
    // prefix can't bypass the date-prefix check below by being stripped as
    // non-ASCII by slugify.
    const normalizedTitle = args.title.normalize("NFKC");

    if (rawSlug(normalizedTitle) === "") {
      return {
        error: "VALIDATION_ERROR",
        message: "Title must contain at least one alphanumeric character",
      } satisfies ToolError;
    }

    const slug = slugify(normalizedTitle);

    // #118: reject titles starting with a YYYY-MM-DD date prefix. The
    // filename builder already prepends the date, so accepting a date-
    // prefixed title silently produces e.g. `2026-05-02-2026-05-02-foo.md`.
    // Match the strict zero-padded form followed by `-` or end-of-slug;
    // looser date-like forms (e.g. `2026/5/2`) are intentionally not
    // covered — they don't produce a doubled-date filename. Allow
    // optional leading hyphens because slugify converts leading whitespace
    // (or a literal leading `-`) into a hyphen that survives `.trim()`.
    if (/^-*\d{4}-\d{2}-\d{2}(-|$)/.test(slug)) {
      return {
        error: "VALIDATION_ERROR",
        message: "Title must not start with a YYYY-MM-DD date prefix — the filename already prefixes the date.",
      } satisfies ToolError;
    }

    const date = today();

    // Guard against same-day same-title collision: append HH-MM-SS suffix when
    // the target path already exists so we never silently overwrite a note or
    // produce a git "nothing to commit" error.
    const baseFilename = `${date}-${slug}.md`;
    const basePath = `${directory}/${baseFilename}`;
    let relPath = basePath;
    try {
      await fs.access(path.join(vaultRoot, basePath));
      // File exists — append time suffix to make the path unique
      const timeSuffix = new Date()
        .toISOString()
        .split("T")[1]
        .slice(0, 8)       // HH:MM:SS
        .replace(/:/g, "-"); // colons not safe in filenames on all OSes
      relPath = `${directory}/${date}-${slug}-${timeSuffix}.md`;
    } catch {
      // File does not exist — use base path as-is
    }

    // #155: intersect with vault.yaml write-grants so we never produce a
    // local commit the hub's pre-receive will reject. Fail-open when
    // vault.yaml is missing or malformed (see loadVaultAcl's comment).
    //
    // PIVOT POINT: if we ever want soft-warn instead of hard-reject
    // (produce the note, attach a warning to the response), flip this
    // early-return into a syncWarning accumulator entry alongside the
    // existing one. One branch to change — keep it that way.
    const acl = loadVaultAcl(vaultRoot);
    if (acl !== null) {
      const scope = deriveScope(relPath);
      const aclIdentity = resolveAclIdentity(owner);
      if (!canWrite(acl, aclIdentity, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${aclIdentity}' is not granted write access to scope ` +
            `'${scope}' by vault.yaml. Hub push would reject this write. ` +
            `Ask the hub admin to extend your write grant.`,
        } satisfies ToolError;
      }
    }

    const metadata: Record<string, unknown> = {
      title: args.title,
      date,
      tags: args.tags ?? [],
      concepts: args.concepts ?? [],
      status: args.status ?? "draft",
      source_agent: owner,
    };
    if (args.confidence !== undefined) {
      metadata.confidence = args.confidence;
    }
    if (typeof args.file_ref === "string" && args.file_ref) {
      metadata.file_ref = args.file_ref;
    }

    const noteContent = buildNote(metadata, args.body, args.connections);
    const result = await writeNote(vaultRoot, relPath, noteContent, args.title, owner);

    // Always-fire even on dedup'd writes: triggerSpokePush is the SOLE spoke→hub
    // mechanism (no git hook handles it), so gating it on `committed` would
    // silently strand any catch-up push for previously-failed sync attempts.
    // triggerIngestion follows suit for symmetry; cost is negligible and the
    // alternative (stale SQLite if an external editor mutated the vault between
    // commits) is worse than a redundant spawn.
    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    // Surface any pending sync-failure sentinel. Typically catches PRIOR
    // pushes (this call's push is fire-and-forget and usually still in
    // flight), but a fast-failing spawn (e.g. ENOENT on schist binary) can
    // synchronously enqueue child.on("error") soon enough that THIS call's
    // failure is included too — either case is a real signal the agent
    // should see, so we don't try to distinguish.
    const syncWarning = await readSyncWarning(vaultRoot);
    return {
      id: relPath,
      path: relPath,
      commitSha: result.commitSha,
      ...(syncWarning !== undefined ? { syncWarning } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function add_connection(
  vaultRoot: string,
  args: { owner: string; source: string; target: string; type: string; context?: string }
): Promise<unknown> {
  try {
    // Identity gate (#63): same ordering as create_note. Reassign to the
    // canonicalized owner for downstream stamps.
    const owner = validateOwner(args.owner);
    const syncDirty = await blockWriteIfSyncDirty(vaultRoot);
    if (syncDirty !== null) return syncDirty;
    const filePath = path.join(vaultRoot, args.source);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Source path is outside vault root" } satisfies ToolError;
    }

    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Source note not found: ${args.source}` } satisfies ToolError;
    }

    // #155: ACL check — mirror create_note's guard. args.source is the
    // vault-relative path; scope derivation uses the same rule as
    // pre_receive.py:derive_scope on the hub.
    const acl = loadVaultAcl(vaultRoot);
    if (acl !== null) {
      const scope = deriveScope(args.source);
      const aclIdentity = resolveAclIdentity(owner);
      if (!canWrite(acl, aclIdentity, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${aclIdentity}' is not granted write access to scope ` +
            `'${scope}' by vault.yaml. Hub push would reject this write. ` +
            `Ask the hub admin to extend your write grant.`,
        } satisfies ToolError;
      }
    }

    const conn = { target: args.target, type: args.type, context: args.context };
    const connLine = buildConnectionLine(conn);

    let newContent: string;
    if (content.includes("## Connections")) {
      newContent = content.replace(/(## Connections\n(?:.*\n)*?)(\n## |\s*$)/, (match, section, after) => {
        return section.trimEnd() + "\n" + connLine + "\n" + after;
      });
    } else {
      newContent = content.trimEnd() + "\n\n## Connections\n\n" + connLine + "\n";
    }

    const result = await writeNote(
      vaultRoot,
      args.source,
      newContent,
      `connection ${args.type} → ${args.target} on ${args.source}`,
      owner
    );

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    // Surface pending sync-failure sentinel — see create_note for the
    // capture-timing notes.
    const syncWarning = await readSyncWarning(vaultRoot);
    return {
      source: args.source,
      target: args.target,
      type: args.type,
      commitSha: result.commitSha,
      ...(syncWarning !== undefined ? { syncWarning } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

/**
 * list_concepts tool handler. Runs the cursor pipeline:
 *
 *   canonicalizeQueryHash → (cursor decode + binding OR identical-query
 *   refusal) → SQL fetch (limit+1, with slug ASC tiebreaker in sqlite-reader)
 *   → recordIssued + issueCursor on capped results → { concepts, cursor? }.
 *
 * No verbose mode — per spec, list_* tools are excluded from verbose.
 * queryHash binds to (tags?, search?, limit, owner).
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number; cursor?: string }
): Promise<ListConceptsResponse | ToolError> {
  const TOOL_NAME = "list_concepts" as const;

  const activeOwner = resolveActiveOwner();
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_QUERY_MISMATCH",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  const effectiveLimit = validateLimit(args.limit, 50, 200);

  let concepts: import("./types.js").Concept[];
  try {
    concepts = sqliteReader.listConcepts(vaultRoot, {
      tags: args.tags,
      search: args.search,
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  const hasMore = concepts.length > effectiveLimit;
  if (hasMore) concepts = concepts.slice(0, effectiveLimit);

  const response: ListConceptsResponse = { concepts };
  if (hasMore) {
    recordIssued({ tool: TOOL_NAME, queryHash, owner: activeOwner, vaultRoot, verboseEnabled: false });
    response.cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
  }

  return response;
}

/**
 * query_graph tool handler. Runs the cursor pipeline:
 *
 *   canonicalizeQueryHash → (cursor decode + binding OR identical-query
 *   refusal) → SQL fetch (subquery-wrapped, limit+1) → recordIssued +
 *   issueCursor on capped results → { columns, rows, rowCount, cursor? }.
 *
 * **Breaking change (spec PR 5):** the server wraps every caller query as
 * `SELECT * FROM (<caller_sql>) AS user_query LIMIT :limit OFFSET :offset`.
 * Default outer limit is 100, hard cap 1000. A caller passing
 * `SELECT * FROM docs` on a 1000-doc vault used to get all 1000 rows; it
 * now gets 100 rows + a cursor. The caller's own LIMIT/ORDER BY/OFFSET
 * inside the SQL are respected verbatim.
 *
 * No verbose mode — per spec, `query_graph`'s response shape is the natural
 * unit; "verbose mode" doesn't apply. Concurrent-ingest caveat from the
 * spec's "Concurrent-ingest limitation" subsection applies.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function query_graph(
  vaultRoot: string,
  args: { sql: string; params?: unknown[]; limit?: number; cursor?: string }
): Promise<QueryGraphResponse | ToolError> {
  const TOOL_NAME = "query_graph" as const;

  // Step 1: canonicalizeQueryHash. resolveActiveOwner (#115) unifies the
  // env chain across all 5 cursor handlers; NAME → ID → "" for consistency.
  // query_graph has no per-call owner arg (the tool schema doesn't expose
  // one) so resolveActiveOwner is called with no argument.
  const activeOwner = resolveActiveOwner();
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 2: Cursor decoding + queryHash binding check.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_QUERY_MISMATCH",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 3: Identical-query refusal (only when no cursor was presented).
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1 to detect hasMore. Default 100, cap 1000.
  const effectiveLimit = validateLimit(args.limit, 100, 1000);

  let result: { columns: string[]; rows: unknown[][]; rowCount: number };
  try {
    result = await sqliteReader.queryGraph(vaultRoot, args.sql, args.params, {
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }

  const hasMore = result.rowCount > effectiveLimit;
  const pageRows = hasMore ? result.rows.slice(0, effectiveLimit) : result.rows;

  // Step 5: Cursor issuance + recordIssued (carve-out #114: only on hasMore).
  let cursor: string | undefined;
  if (hasMore) {
    recordIssued({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled: false,
    });
    cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
  }

  const response: QueryGraphResponse = {
    columns: result.columns,
    rows: pageRows,
    rowCount: pageRows.length,
  };
  if (cursor !== undefined) response.cursor = cursor;
  return response;
}

/**
 * compose_brief is a read-only context packer for external filing workflows.
 * It does not create GitHub/Jira/etc. issues; it returns markdown plus metadata
 * for the caller to edit and file with their preferred tool.
 */
export async function compose_brief(
  vaultRoot: string,
  args: {
    topic: string;
    scope?: string[];
    related_notes?: string[];
    related_external?: string[];
    session_paths?: boolean;
  }
): Promise<ComposeBriefResponse | ToolError> {
  if (typeof args.topic !== "string" || args.topic.trim().length === 0) {
    return { error: "VALIDATION_ERROR", message: "topic is required" };
  }
  if (args.session_paths !== undefined && typeof args.session_paths !== "boolean") {
    return { error: "VALIDATION_ERROR", message: "session_paths must be a boolean" };
  }

  const scope = ensureStringArray(args.scope, "scope");
  if (!Array.isArray(scope)) return scope;
  const pinned = ensureStringArray(args.related_notes, "related_notes");
  if (!Array.isArray(pinned)) return pinned;
  const relatedExternal = ensureStringArray(args.related_external, "related_external");
  if (!Array.isArray(relatedExternal)) return relatedExternal;

  const byId = new Map<string, BriefNote>();
  const tagCounts = new Map<string, number>();

  try {
    const searchRows = sqliteReader.searchNotes(vaultRoot, args.topic, { limit: 12 });
    for (const row of searchRows) {
      if (!matchesScopePath(row.id, scope)) continue;
      if (!byId.has(row.id)) byId.set(row.id, briefNoteFromSearch(row, "topic search"));
      addTags(tagCounts, row.tags);
      if (byId.size >= 5) break;
    }

    for (const id of pinned) {
      if (!matchesScopePath(id, scope)) continue;
      const note = sqliteReader.getNote(vaultRoot, id);
      if (!note) continue;
      byId.set(id, briefNoteFromNote(note, "pinned"));
      addTags(tagCounts, note.tags);
    }

    const seedIds = [...byId.keys()];
    if (seedIds.length > 0) {
      const placeholders = seedIds.map(() => "?").join(", ");
      const graph = await sqliteReader.queryGraph(
        vaultRoot,
        `
          SELECT e.source, e.target, e.type, d.id, d.title, d.tags, d.body
          FROM edges e
          JOIN docs d
            ON d.id = CASE WHEN e.source IN (${placeholders}) THEN e.target ELSE e.source END
          WHERE e.source IN (${placeholders}) OR e.target IN (${placeholders})
          ORDER BY d.date DESC, d.id ASC
        `,
        [...seedIds, ...seedIds, ...seedIds],
        { limit: 10 },
      );
      const idx = Object.fromEntries(graph.columns.map((col, i) => [col, i]));
      for (const row of graph.rows) {
        const id = row[idx.id] as string;
        if (!id || byId.has(id) || !matchesScopePath(id, scope)) continue;
        const title = (row[idx.title] as string) || id;
        const body = (row[idx.body] as string) || "";
        const edgeType = (row[idx.type] as string) || "related";
        byId.set(id, {
          id,
          title,
          reason: `graph ${edgeType}`,
          tags: typeof row[idx.tags] === "string" ? JSON.parse(row[idx.tags] as string) : [],
          annotation: oneLine(body) || title,
        });
        addTags(tagCounts, row[idx.tags]);
      }
    }
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  const relatedNotes = [...byId.values()].slice(0, 10);
  const recent = args.session_paths === false ? [] : await recentAddedPaths(vaultRoot, scope);
  const suggestedTags = [...tagCounts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 8)
    .map(([tag]) => tag);
  const crossRefs = [...new Set([...relatedExternal, ...relatedNotes.map((note) => note.id)])];

  const noteLines = relatedNotes.length > 0
    ? relatedNotes.map((note) => `- \`${note.id}\` — ${note.title}: ${note.annotation} (${note.reason})`)
    : ["- None found."];
  const pathLines = recent.length > 0
    ? recent.map((entry) => `- Added: \`${entry.path}\` @ \`${entry.commit || "unknown"}\``)
    : ["- None found in recent local git history."];
  const refLines = crossRefs.length > 0
    ? crossRefs.map((ref) => `- ${ref}`)
    : ["- None identified."];

  const markdown = [
    "## Summary",
    args.topic.trim(),
    "",
    "## Related vault notes",
    ...noteLines,
    "",
    "## Recent session context",
    ...pathLines,
    "",
    "## Suggested cross-references",
    ...refLines,
    "",
    "## Acceptance criteria",
    "- [ ] Fill in the concrete acceptance criteria before filing.",
    "",
    "## Repro / evidence",
    "- Fill in commands, paths, logs, or screenshots before filing.",
  ].join("\n");

  return {
    markdown,
    suggested_tags: suggestedTags,
    cross_refs: crossRefs,
    related_notes: relatedNotes.map(({ id, title, reason }) => ({ id, title, reason })),
    recent_paths: recent,
  };
}

/**
 * get_context tool handler. Adopts reason-string verbose (#50 PR 7).
 *
 *   parseVerbose → effective-depth resolution → spoke pull → sentinel read →
 *   SQLite read → optional logVerbose + noteHighFrequency → assemble response.
 *
 * Soft-downgrade semantics (spec §"Reason-string verbose"):
 *   - depth="full" + valid verbose (≥12 cp) → run tagCloud, log audit line.
 *   - depth="full" + missing/whitespace verbose → silently run as depth="standard"
 *     and attach a verboseNote hinting at the upgrade path. NOT an error —
 *     callers that lazily ask for "full" should still get a usable response.
 *   - depth=anything + verbose: true (boolean) or <12 cp string → INVALID_ARG.
 *     parseVerbose rejects type/length misuse identically; no per-depth bypass.
 *   - depth!="full" + valid verbose → verbose validated for type only; ignored
 *     semantically (no logVerbose). Matches search_memory's "validate first" pattern.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function get_context(
  vaultRoot: string,
  args: { depth?: "minimal" | "standard" | "full"; verbose?: string }
): Promise<GetContextResponse | ToolError> {
  const TOOL_NAME = "get_context" as const;

  // Step 1: parseVerbose. Reject INVALID_ARG (boolean / non-string / too-short)
  // before any I/O. Whitespace-only and omitted both return { enabled: false }
  // with no error — handled identically below as "no verbose intent."
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: effective depth resolution. If the caller asked for "full" but
  // didn't supply a valid verbose reason, downgrade to "standard" and prepare
  // a soft hint. Any other (depth, verbose) combination passes through.
  const requestedDepth = args.depth ?? "minimal";
  let effectiveDepth: "minimal" | "standard" | "full" = requestedDepth;
  let downgradeNote: string | undefined;
  if (requestedDepth === "full" && !verboseEnabled) {
    effectiveDepth = "standard";
    downgradeNote =
      'depth="full" requires verbose: "<reason ≥12 chars>"; downgraded to "standard"';
  }

  await maybeSpokePull(vaultRoot);

  // Step 3: Surface any pending background-sync-failure sentinel without
  // clearing it. A dirty spoke should stay machine-readable until a successful
  // push path (`triggerSpokePush` or `sync_retry`) clears the sentinel; merely
  // reading context is not proof that local commits reached the hub.
  let syncWarning: string | undefined;
  syncWarning = await readSyncWarning(vaultRoot);

  // Step 4: SQLite read at effectiveDepth.
  let context: Record<string, unknown>;
  try {
    context = sqliteReader.getContext(vaultRoot, effectiveDepth) as Record<string, unknown>;
  } catch (e: unknown) {
    const err = normalizeError(e, "INGEST_ERROR");
    return syncWarning ? { ...err, syncWarning } : err;
  }
  if (syncWarning) context.syncWarning = syncWarning;

  // Step 5: verbose audit log + rate-limit hint (only on the true depth="full"
  // path — downgraded calls already carry a verboseNote).
  let freqNote: string | undefined;
  const activeOwner = resolveActiveOwner();
  if (effectiveDepth === "full" && verboseEnabled && verboseReason !== undefined) {
    logVerbose({ tool: TOOL_NAME, owner: activeOwner, reason: verboseReason });
    const note = noteHighFrequency({
      tool: TOOL_NAME,
      owner: activeOwner,
      reason: verboseReason,
    });
    if (note !== null) freqNote = note;
  }

  // Step 6: assemble response. verboseNote is set if either (a) the call was
  // downgraded, or (b) the rate-limit tracker fired. Concatenate when both.
  const verboseNote =
    downgradeNote !== undefined && freqNote !== undefined
      ? `${downgradeNote}; ${freqNote}`
      : downgradeNote ?? freqNote;
  if (verboseNote !== undefined) context.verboseNote = verboseNote;

  return context as GetContextResponse;
}

// ── Memory V2 Tools ────────────────────────────────────────────────────────

// READ-ONLY memory tools (no capability gate)

/**
 * search_memory tool handler. Runs the protocol pipeline:
 *
 *   parseVerbose → canonicalizeQueryHash → (cursor decode + binding OR
 *   identical-query refusal) → SQL fetch (limit+1) → snippet vs full content
 *   → recordIssued + issueCursor on capped results → logVerbose +
 *   noteHighFrequency on verbose → { entries, cursor?, verboseNote? }.
 *
 * All 8 stages are implemented in this file; see the numbered Step comments
 * inline. This handler is the prototype for the cursor-adopting tools:
 * search_notes (landed in PR 4) and query_graph (landed in PR 5 — both
 * defined above), then list_concepts, get_context.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function search_memory(
  // Was `_vaultRoot` (underscore-unused) — now consumed as a refusal-LRU key
  // segment (#113) so multi-vault deployments don't see cross-vault refusal
  // collision. The memory DB itself is still at the separate path resolved
  // inside sqliteReader (process.env.SCHIST_MEMORY_DB or ~/.openclaw/...).
  vaultRoot: string,
  args: {
    query?: string;
    owner?: string;
    entry_type?: string;
    date_from?: string;
    date_to?: string;
    limit?: number;
    cursor?: string;
    verbose?: string;
  }
): Promise<SearchMemoryResponse | ToolError> {
  const TOOL_NAME = "search_memory" as const;

  // Step 1: parseVerbose. Reject INVALID_ARG before any SQL or canonicalize work.
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: canonicalizeQueryHash. resolveActiveOwner (#115) unifies the
  // env chain across all 5 cursor handlers; NAME → ID → "". search_memory
  // has no per-call owner arg (the `args.owner` is a FILTER on entries,
  // not the caller identity — see schema).
  const activeOwner = resolveActiveOwner();
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 3: Cursor decoding + queryHash binding check.
  // Binding policy: current call's computed queryHash MUST equal the cursor's
  // encoded queryHash. Mismatch → CURSOR_QUERY_MISMATCH (distinct from
  // CURSOR_INVALID_SIGNATURE which is HMAC-fail; see #112 for the disambiguation).
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_QUERY_MISMATCH",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 4: Identical-query refusal (only when no cursor was presented).
  // The verbose-newly-set bypass is enforced inside checkRefusal — false→true
  // bypasses, true→true and true→false remain refused (spec line 145 + the
  // PR 2 protocol unit tests at protocol/cursor.test.ts).
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 5: SQL fetch with limit + 1 to detect hasMore. Default 50, cap 200.
  const effectiveLimit = validateLimit(args.limit, 50, 200);

  let rows: import("./types.js").MemoryEntry[];
  try {
    rows = sqliteReader.searchMemory({
      query: args.query,
      owner: args.owner,
      entry_type: args.entry_type,
      date_from: args.date_from,
      date_to: args.date_to,
      limit: effectiveLimit + 1,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }

  const hasMore = rows.length > effectiveLimit;
  const pageRows = hasMore ? rows.slice(0, effectiveLimit) : rows;

  // Step 6: Snippet vs full content. Default response carries a 200-cp
  // snippet; verbose mode returns the full content. snippetContent preserves
  // the original string when it fits (no decompose/recompose round-trip).
  const entries = verboseEnabled
    ? pageRows
    : pageRows.map(r => ({ ...r, content: snippetContent(r.content) }));

  // Step 7: Cursor issuance + recordIssued (only when this page was capped).
  // recordIssued's verboseEnabled is the state of THIS call (the call that
  // issued the cursor) — checkRefusal compares it to the next call's state.
  let cursor: string | undefined;
  if (hasMore) {
    // recordIssued runs before issueCursor. issueCursor is pure (HMAC + base64
    // encoding of a known-good payload) and cannot throw under normal
    // operation. If a future implementer makes issueCursor fallible, flip
    // these two so the LRU isn't left with a phantom record.
    recordIssued({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      vaultRoot,
      verboseEnabled,
    });
    cursor = issueCursor({
      tool: TOOL_NAME,
      queryHash,
      offset: offset + effectiveLimit,
    });
  }

  // Step 8: Verbose audit log + frequency tracker.
  let verboseNote: string | undefined;
  // The verboseReason !== undefined check is defensive — when v.enabled is
  // true, parseVerbose guarantees v.reason is a string. Keeping the explicit
  // check helps TypeScript narrow `verboseReason` to `string` inside the
  // block without an assertion. Copy-paste this pattern into PRs 4–7.
  if (verboseEnabled && verboseReason !== undefined) {
    logVerbose({ tool: TOOL_NAME, owner: activeOwner, reason: verboseReason });
    const note = noteHighFrequency({
      tool: TOOL_NAME,
      owner: activeOwner,
      reason: verboseReason,
    });
    if (note !== null) verboseNote = note;
  }

  const response: SearchMemoryResponse = { entries };
  if (cursor !== undefined) response.cursor = cursor;
  if (verboseNote !== undefined) response.verboseNote = verboseNote;
  return response;
}

export async function get_agent_state(
  _vaultRoot: string,
  args: { key: string }
): Promise<unknown> {
  try {
    return sqliteReader.getAgentState(args.key);
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }
}

// WRITE memory tools (require write capability gate)

export async function add_memory(
  _vaultRoot: string,
  args: {
    owner: string;
    entry_type: string;
    content: string;
    date?: string;
    tags?: string[];
    related_doc?: string;
    source_ref?: string;
    confidence?: string;
  }
): Promise<unknown> {
  try {
    // Canonicalize so `agent_memory.owner` stores the trimmed form — pre-#131
    // a caller passing "atwood " would have been silently accepted (allowlist
    // already trimmed at parse time) and stored under a key that diverges
    // from every "atwood" write by the same agent.
    const owner = validateOwner(args.owner);
    return sqliteReader.addMemory({ ...args, owner });
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function set_agent_state(
  _vaultRoot: string,
  args: { key: string; value: unknown; owner: string; ttl_hours?: number }
): Promise<unknown> {
  try {
    const owner = validateOwner(args.owner);
    return sqliteReader.setAgentState(args.key, args.value, owner, args.ttl_hours);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function delete_agent_state(
  _vaultRoot: string,
  args: { key: string; owner: string }
): Promise<unknown> {
  try {
    const owner = validateOwner(args.owner);
    return sqliteReader.deleteAgentState(args.key, owner);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function add_concept_alias(
  vaultRoot: string,
  args: { duplicate_slug: string; canonical_slug: string; reason?: string; created_by: string }
): Promise<unknown> {
  try {
    const createdBy = validateOwner(args.created_by);
    return sqliteReader.addConceptAlias(vaultRoot, args.duplicate_slug, args.canonical_slug, args.reason, createdBy);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}
