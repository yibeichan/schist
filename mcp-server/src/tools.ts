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
import type { VaultConfig, ToolError, SearchMemoryResponse, SearchNotesResponse, QueryGraphResponse, ListConceptsResponse, GetContextResponse } from "./types.js";
import { loadVaultAcl, canWrite, deriveScope } from "./vault-acl.js";
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

/**
 * Write a sync-failure sentinel so agents have a visible trace when a
 * background push silently fails. `get_context` reads this and surfaces it
 * to the caller on the next read; `readSyncWarning` (below) also surfaces it
 * on the NEXT write tool's response so write-heavy sessions don't have to
 * call get_context to notice.
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
 * Read the sync-failure sentinel WITHOUT clearing it, formatted as a warning
 * string. Returns undefined if the file is missing, empty, or unreadable.
 *
 * Called from successful write-tool responses so an agent in a write-heavy
 * session (which rarely calls get_context between writes) sees the warning
 * on the next write rather than discovering the divergence at session end.
 *
 * Clearing is left to get_context — it's the explicit "I am syncing with
 * the world" call and a natural acknowledge point. Repeating the warning
 * across N writes until then is by design: until the agent acknowledges
 * via get_context, each write is committing into a vault that's diverging
 * from hub, and the agent should keep being told.
 *
 * Spec context: see #120 — pre-fix, agents accumulated 13+ MCP commits with
 * silent push failures before noticing the spoke had diverged from hub.
 */
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
  // Descriptive phrasing (not imperative). Agents reading this on every
  // write should NOT abandon their current task to "acknowledge" — the
  // sentinel will be cleared the next time get_context runs anyway.
  return `Recent background sync failure: ${errText}. Writes may not have reached the hub. \`get_context\` will clear this on its next call.`;
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
const inFlightSpokePushes = new Set<string>();

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
    inFlightSpokePushes.add(vaultRoot);

    const child = spawn(
      schistCliBin("schist"),
      ["--vault", vaultRoot, "sync", "push"],
      { cwd: vaultRoot, stdio: "ignore", env: process.env, detached: true }
    );
    child.unref();

    // Clean up the in-flight marker on terminal events. Wrap in helper so
    // both error and exit can call it idempotently — handlers can fire in
    // either order depending on the failure mode.
    const cleanup = (): void => { inFlightSpokePushes.delete(vaultRoot); };

    child.on("error", (err) => {
      cleanup();
      // spawn error = schist binary not on PATH, or permission denied.
      // Silent by default is a footgun — write a sentinel so the next
      // get_context (or write-tool response — see readSyncWarning) can
      // surface it. Also log for operators watching stderr.
      console.error("[schist] spoke push failed:", err);
      writeSyncError(vaultRoot, `push spawn failed: ${err.message}`);
    });
    child.on("exit", (code, signal) => {
      cleanup();
      if (code !== null && code !== 0) {
        writeSyncError(vaultRoot, `push exited with code ${code}`);
      } else if (code === null && signal) {
        // Signal-killed: SIGTERM / SIGKILL from OOM killer, parent shutdown
        // sending TERM down the process group, etc. Without this branch the
        // push died silently and the next get_context would report "healthy."
        writeSyncError(vaultRoot, `push killed by signal ${signal}`);
      }
    });
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
    return {
      id: args.id,
      title: (meta.title as string) ?? "",
      date: (meta.date as string) ?? "",
      status: (meta.status as string) ?? "draft",
      tags: Array.isArray(meta.tags) ? meta.tags : [],
      concepts: Array.isArray(meta.concepts) ? meta.concepts : [],
      body,
      connections,
      ...(confidence === "low" || confidence === "medium" || confidence === "high"
        ? { confidence }
        : {}),
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
      if (!canWrite(acl, owner, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${owner}' is not granted write access to scope ` +
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
      if (!canWrite(acl, owner, scope)) {
        return {
          error: "ACL_DENIED",
          message:
            `Identity '${owner}' is not granted write access to scope ` +
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
    result = sqliteReader.queryGraph(vaultRoot, args.sql, args.params, {
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

  // Step 3: Read (and clear) any pending background-sync-failure sentinel so
  // agents don't silently work against a stale local view. errText is
  // sanitized via the same helper readSyncWarning uses, so write-path and
  // get_context surfacing share one trust-boundary policy on sentinel content.
  //
  // Atomic clear (#124): rename → read → unlink. A concurrent write-tool's
  // readSyncWarning call against the canonical path either runs BEFORE the
  // rename (sees the sentinel; will surface the warning — fine, the agent
  // gets one extra warning) or AFTER (sees no file; healthy state). It
  // never observes a partially-unlinked state, and the warning isn't
  // surfaced for follow-up writes after this get_context call has
  // acknowledged it.
  let syncWarning: string | undefined;
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  const consumedPath = `${sentinelPath}.consumed-${process.pid}-${Date.now()}`;
  try {
    await fs.rename(sentinelPath, consumedPath);
    // From here on, the canonical path doesn't exist — concurrent
    // readSyncWarning calls see ENOENT and report healthy.
    const errText = sanitizeSentinelContent(await fs.readFile(consumedPath, "utf-8"));
    if (errText) {
      syncWarning = `Recent background sync failure: ${errText}. Writes may not have reached the hub.`;
    }
    await fs.unlink(consumedPath).catch(() => {});
  } catch {
    // ENOENT on rename = no sentinel — healthy state.
  }

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
