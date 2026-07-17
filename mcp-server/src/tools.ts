import * as fs from "fs/promises";
import { readFileSync } from "fs";
import * as path from "path";
import { fileURLToPath } from "url";
import { spawn } from "child_process";
import { load as yamlLoad } from "js-yaml";
import * as sqliteReader from "./sqlite-reader.js";
import { writeNote, updateNote, deleteNote } from "./git-writer.js";
import { buildNote, buildConnectionLine, insertConnectionLine, parseNote, CONNECTION_RE, SLUG_WS_CHARS, splitLinesLikePython, containsLineBoundary, isRoundTrippableTarget } from "./markdown-parser.js";
import { validateOwner, resolveActiveOwner } from "./agent-identity.js";
import { noteIdShapeError } from "./note-id.js";
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

// #338: title slugs must be byte-identical across languages — the slug is
// embedded in the note id (filename), and cli/schist/markdown_io.py's slugify
// is the other producer. Native \s membership drifts between engines (see
// SLUG_WS_CHARS in markdown-parser.ts), so a title containing e.g. U+0085
// yielded note id `a-b` from Python but `ab` from TS. Both languages now use
// the explicit whitespace union; schema/title-slug-parity.json pins them.
const TITLE_NON_SLUG_RUN = new RegExp(`[^a-z0-9${SLUG_WS_CHARS}-]+`, "g");
const TITLE_WS_RUN = new RegExp(`[${SLUG_WS_CHARS}]+`, "g");

/** Linear edge-dash strip — index scan, never `^-+|-+$` (an anchored
 * alternated regex backtracks quadratically over interior runs; see
 * trimSlugWs). */
function trimDashes(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && value[start] === "-") start++;
  while (end > start && value[end - 1] === "-") end--;
  return start === 0 && end === value.length ? value : value.slice(start, end);
}

/** @internal — exported for the schema/title-slug-parity.json fixture test.
 * Mirrors cli/schist/markdown_io.py's slugify byte-for-byte (#338). */
export function titleSlug(title: string): string {
  return trimDashes(
    title
      .toLowerCase()
      .replace(TITLE_NON_SLUG_RUN, "")
      .replace(TITLE_WS_RUN, "-")
      .replace(/-+/g, "-")
  );
}

function slugify(title: string): string {
  return titleSlug(title) || "untitled";
}

/** Returns the raw slug without the "untitled" fallback — used to detect empty-slug titles */
function rawSlug(title: string): string {
  return titleSlug(title);
}

function today(): string {
  return new Date().toISOString().split("T")[0];
}

/**
 * Validate a caller-supplied note id for the in-place edit tools (update_note,
 * delete_note). Unlike create_note — which BUILDS the path from a validated
 * `directory` + slug — these accept a full path, so the lexical vault-root
 * check is not enough: `.git/hooks/post-commit`, `.git/config`, and `.schist/*`
 * all resolve INSIDE the vault root. Writing there is arbitrary-file-write
 * (the post-commit hook path is RCE-on-next-commit). Constrain to a `.md` file
 * under a configured top-level directory, with no `..`, absolute path, or
 * dot-prefixed segment. The config-independent shape checks live in
 * `noteIdShapeError` (note-id.ts) so add_memory's related_doc validation
 * shares the exact same rule without needing vault config (D4). Returns a
 * ToolError to surface, or null when valid.
 */
function validateNoteId(id: string, config: VaultConfig): ToolError | null {
  if (typeof id !== "string" || id.length === 0) {
    return { error: "VALIDATION_ERROR", message: "Note id is required" };
  }
  const shapeProblem = noteIdShapeError(id);
  if (shapeProblem !== null) {
    return { error: "VALIDATION_ERROR", message: `Invalid note id: ${shapeProblem}` };
  }
  if (!config.directories.includes(id.split("/")[0])) {
    return {
      error: "VALIDATION_ERROR",
      message: `Note id must be under a configured directory. Allowed top-level: ${config.directories.join(", ")}`,
    };
  }
  return null;
}

/**
 * Validate add_memory's optional `related_doc` — defined (docs/data-model.md
 * D4) as "a vault note id (`notes/….md`)". SHAPE only, by design: memory is
 * the fuel station and must stay writable when the vault is unavailable, so
 * there is no FK, no existence check, and no vault-DB or filesystem access —
 * just the shared string rule from note-id.ts. Returns a ToolError naming
 * the parameter, or null when valid.
 */
function validateRelatedDoc(relatedDoc: unknown): ToolError | null {
  if (typeof relatedDoc !== "string" || relatedDoc.length === 0) {
    return {
      error: "VALIDATION_ERROR",
      message: "related_doc must be a non-empty string when provided (a vault note id like notes/topic.md)",
    };
  }
  const shapeProblem = noteIdShapeError(relatedDoc);
  if (shapeProblem !== null) {
    return {
      error: "VALIDATION_ERROR",
      message: `Invalid related_doc: ${shapeProblem}. Expected a vault note id like notes/topic.md (the note's existence is deliberately not checked).`,
    };
  }
  return null;
}

// Frontmatter keys update_note may patch. Deliberately excludes `scope` (would
// let a path-authorized caller spoof graph read-visibility, since ingest
// prefers frontmatter scope over the directory) and `source`/`source_agent`
// (provenance must not be forgeable). Mirrors the fields create_note controls.
// Pinned by schema/frontmatter-contract.json (#130 slice A) — extend the
// contract (and its Python conformance test) when adding a key here.
/** @internal — exported for the schema/frontmatter-contract.json conformance test. */
export const PATCHABLE_FRONTMATTER_KEYS = new Set([
  "title", "date", "status", "tags", "concepts", "confidence", "file_ref",
]);

function validateNonEmptyStringArray(value: unknown, label: string): ToolError | null {
  if (!Array.isArray(value) || value.some((v) => typeof v !== "string" || !v.trim())) {
    return { error: "VALIDATION_ERROR", message: `${label} must be an array of non-empty strings` };
  }
  return null;
}

function normalizeTag(value: string): string {
  return value.trim().replace(/^#+/, "").trim();
}

function validateTags(value: unknown, label: string): ToolError | null {
  if (!Array.isArray(value) || value.some((v) => typeof v !== "string" || !normalizeTag(v))) {
    return { error: "VALIDATION_ERROR", message: `${label} must be an array of non-empty tags` };
  }
  return null;
}

function normalizeTags(value: string[]): string[] {
  return value.map(normalizeTag);
}

/**
 * Validate update_note's frontmatter_patch against the allowlist + per-key
 * types. A `null` value (delete-the-key) is always allowed. Rejects unknown
 * keys (notably `scope`/`source`/`source_agent`) and wrong types that would
 * either silently corrupt the note or, worse, be reinterpreted by ingest
 * (e.g. tags must stay a string array). Returns a ToolError or null.
 */
function validateFrontmatterPatch(
  patch: Record<string, unknown> | undefined,
  config: VaultConfig
): ToolError | null {
  if (patch === undefined) return null;
  for (const [key, value] of Object.entries(patch)) {
    if (!PATCHABLE_FRONTMATTER_KEYS.has(key)) {
      return {
        error: "VALIDATION_ERROR",
        message: `frontmatter_patch key '${key}' is not patchable. Allowed: ${[...PATCHABLE_FRONTMATTER_KEYS].join(", ")}.`,
      };
    }
    if (value === null) continue; // null = delete the key
    if (key === "tags") {
      const tagsError = validateTags(value, "frontmatter_patch.tags");
      if (tagsError !== null) return tagsError;
    } else if (key === "concepts") {
      const arrayError = validateNonEmptyStringArray(value, `frontmatter_patch.${key}`);
      if (arrayError !== null) return arrayError;
    } else if (key === "confidence") {
      if (!["low", "medium", "high"].includes(value as string)) {
        return { error: "VALIDATION_ERROR", message: `confidence must be one of: low, medium, high (got "${value}")` };
      }
    } else if (key === "status") {
      if (typeof value !== "string") {
        return { error: "VALIDATION_ERROR", message: "frontmatter_patch.status must be a string" };
      }
      if (!config.statuses.includes(value)) {
        return { error: "VALIDATION_ERROR", message: `status must be one of: ${config.statuses.join(", ")} (got "${value}")` };
      }
    } else if (typeof value !== "string") {
      return { error: "VALIDATION_ERROR", message: `frontmatter_patch.${key} must be a string` };
    }
  }
  return null;
}

/**
 * True when `relPath` resolves (after following symlinks) to a path inside the
 * vault root. validateNoteId and the lexical path guards are string-only, so a
 * tracked symlink inside a note directory could redirect a write outside the
 * vault (arbitrary-file-write / RCE-on-next-commit). This compares real paths.
 * The vault root itself may legitimately be a symlink (env vars often point at
 * one), so both sides are realpath-resolved before comparison. Returns false on
 * any resolution error — callers run this only after confirming the target
 * exists, so a failure means the path is suspect. #119.
 */
async function resolvesInsideVault(vaultRoot: string, relPath: string): Promise<boolean> {
  try {
    const realRoot = await fs.realpath(vaultRoot);
    const realTarget = await fs.realpath(path.join(vaultRoot, relPath));
    return realTarget === realRoot || realTarget.startsWith(realRoot + path.sep);
  } catch {
    return false;
  }
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

  // #413: a connection-type entry serializes into `- {type}: target` lines,
  // so it must satisfy the same single-token rule as a target (non-empty, no
  // SLUG_WS_CHARS — CONNECTION_RE's type group is one non-whitespace run). A
  // whitespace-carrying entry passes the membership check but then writes a
  // line that never round-trips (dead edge) or splits on read (forged
  // entry) — the config-reachable version of the #398/#408 target holes.
  // Warn-and-drop, not hard-error: one junk entry must not brick the server,
  // and a dropped type is simply rejected at write time by the membership
  // check. Statuses are deliberately NOT token-validated: they live in YAML
  // frontmatter, which round-trips arbitrary strings — no structural failure
  // mode. Pinned against the CLI by schema/vocab-token-parity.json.
  const connectionTypes = getStringList("connection_types", [...DEFAULT_CONNECTION_TYPES])
    .filter((t) => {
      if (isRoundTrippableTarget(t)) return true;
      console.error(
        `[schist] Ignoring connection_types entry ${JSON.stringify(t)}: ` +
        `not a single whitespace-free token — a connection line using it could never round-trip through the parser`
      );
      return false;
    });

  return {
    name: getString("name", path.basename(vaultRoot)),
    path: vaultRoot,
    directories: getStringList("directories", [...loadCanonicalDirectories()]),
    connectionTypes,
    statuses: getStringList("statuses", [...DEFAULT_STATUSES]),
    writeBranch: getString("write_branch", "drafts"),
  };
}

// Fallback vocabularies for a schist.yaml that exists but omits the key.
// These MUST match cli/schist/default.yaml — the canonical default `schist
// init` writes and the CLI's own fallback — or the two write paths enforce
// different vocabularies on the same partial-config vault (#403: this list
// omitted "references", so MCP rejected an edge type `schist link` accepted).
// Exported for the parity test that pins them against the YAML file.
// KNOWN ASYMMETRY (#414): `directories` falls back via a RUNTIME read of that
// same default.yaml (loadCanonicalDirectories) while these two are baked at
// build time — a default.yaml patched in place skews vocab but not
// directories, and independently-versioned pip/npm installs can re-skew the
// baked copy with no repo test running. Both are tracked in #414 (doctor
// version-skew check + unify-or-document the fallback split); don't silently
// "fix" one side here without reading that issue first.
export const DEFAULT_CONNECTION_TYPES = [
  "extends", "contradicts", "supports", "replicates",
  "applies-method-of", "reinterprets", "related", "references",
] as const;
export const DEFAULT_STATUSES = ["draft", "review", "final", "archived"] as const;

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
  opts: { cwd: string; timeoutMs: number; env?: NodeJS.ProcessEnv; capture?: boolean; stdin?: string }
): Promise<SyncCommandOutcome> {
  return await new Promise<SyncCommandOutcome>((resolve) => {
    const child = spawn(command, args, {
      cwd: opts.cwd,
      env: opts.env ?? process.env,
      detached: true,
      stdio: [
        opts.stdin !== undefined ? "pipe" : "ignore",
        ...(opts.capture ? (["pipe", "pipe"] as const) : (["ignore", "ignore"] as const)),
      ],
    });
    if (opts.stdin !== undefined) {
      // EPIPE if the child exits before consuming stdin (e.g. usage error) —
      // swallow it; the exit-code path already reports the failure.
      child.stdin?.on("error", () => { /* ignored */ });
      child.stdin?.end(opts.stdin);
    }
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

/**
 * The vault's current "ingest generation" — the HEAD commit SHA (#90). schist's
 * post-commit hook drops + rebuilds the vault DB on every commit, so a change in
 * HEAD is exactly the event that can reorder OFFSET-paginated rows. Vault-DB
 * cursor handlers stamp this into the cursor on issue and pass it on decode;
 * decodeCursor returns CURSOR_STALE when it has moved.
 *
 * Failure (no commits yet, git error) returns a stable sentinel. Both issue and
 * decode resolve the same sentinel, so an empty repo never produces a false
 * CURSOR_STALE; a transient git failure between pages conservatively reads as
 * stale (caller restarts) rather than silently serving a possibly-shifted page.
 */
async function vaultGeneration(vaultRoot: string): Promise<string> {
  const head = await runGit(vaultRoot, ["rev-parse", "HEAD"], SYNC_STATUS_TIMEOUT_MS);
  const sha = head.ok ? (head.stdout ?? "").trim() : "";
  return sha || "no-head";
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
  // Non-empty required (#237): an empty-string scope element makes
  // matchesScopePath false for every vault id (ids never start with "/"),
  // silently returning an empty brief; empty note ids/refs are equally
  // meaningless in the other two fields.
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item.trim())) {
    return { error: "VALIDATION_ERROR", message: `${field} must be an array of non-empty strings` };
  }
  return value;
}

function oneLine(text: string, cap = 180): string {
  // No frontmatter strip here: bodies stored in SQLite are already
  // frontmatter-free (Python ingest stores post.content), and FTS snippets
  // never carry a YAML block. A `/m`-flagged `^---...---` strip would instead
  // match Markdown horizontal rules in the body and silently delete content
  // between them (see #230).
  const cleaned = text
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
    // Strip searchNotes' FTS5 highlight markers (<b>…</b>) before oneLine:
    // its markdown-char class blanks `>` but not `<`, so the markers would
    // otherwise leak into every topic-search annotation as `<b …< /b` noise.
    // See #236.
    annotation: oneLine(note.snippet.replace(/<\/?b>/g, "")) || note.title,
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

async function recentAddedPaths(
  vaultRoot: string,
  scope: string[]
): Promise<{ ok: boolean; rows: Array<{ path: string; commit: string }> }> {
  const outcome = await runGit(vaultRoot, [
    "log",
    "--since=24 hours ago",
    "--diff-filter=A",
    "--name-only",
    "--pretty=format:commit:%h",
  ], 2_000);
  // Distinguish "git failed/timed out" from "no files added" (#238): the
  // 2s timeout fires exactly on the slow-git deployments where an agent
  // would otherwise trust an empty "Recent session context" section. One
  // failure IS an accurate "none": a repo with zero commits exits 128 with
  // "does not have any commits" — schist init always seeds a commit, but a
  // hand-rolled `git init` vault shouldn't read as "unavailable" forever.
  if (!outcome.ok) {
    const noCommitsYet = (outcome.stderr ?? "").includes("does not have any commits");
    return { ok: noCommitsYet, rows: [] };
  }
  if (!outcome.stdout) return { ok: true, rows: [] };

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
  return { ok: true, rows };
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

// Basename patterns of OS/editor litter the ignore guard warns about and
// skips instead of hard-failing (#388) — a hub admin ignoring `.DS_Store`
// must not brick every macOS spoke. Keep textually identical to
// IGNORE_GUARD_JUNK_BASENAMES in cli/schist/git_ops.py so the CLI guard and
// this sync_status probe always agree on what blocks a push; a drift test
// in tests/tools.test.ts pins the two lists together.
export const IGNORE_GUARD_JUNK_BASENAMES = [".DS_Store", "Thumbs.db", "desktop.ini", "*~"] as const;

function isJunkBasename(filePath: string): boolean {
  // Ignored *directories* surface from the porcelain probe with a trailing
  // slash; their basename is then "" which matches nothing — directories are
  // never junk-skipped. Mirrors cli/schist/git_ops.py _is_junk_basename.
  // A basename match is only a CANDIDATE — confirmedJunk must attribute the
  // exclusion to a junk-shaped .gitignore pattern before it stops blocking.
  const base = filePath.slice(filePath.lastIndexOf("/") + 1);
  return IGNORE_GUARD_JUNK_BASENAMES.some((pattern) =>
    pattern === "*~" ? base.endsWith("~") : base === pattern,
  );
}

// True when a .gitignore PATTERN is itself a junk-allowlist entry. Strips
// the anchoring prefix (a leading "/" or "**/") and any trailing "/", then
// requires exact equality: "*~" and "**/.DS_Store" are junk-shaped;
// "secret*" and "research/.DS_Store" are not. Mirrors
// cli/schist/git_ops.py _is_junk_shaped_pattern.
function isJunkShapedPattern(pattern: string): boolean {
  let p = pattern;
  if (p.startsWith("**/")) p = p.slice(3);
  else if (p.startsWith("/")) p = p.slice(1);
  p = p.replace(/\/+$/, "");
  return (IGNORE_GUARD_JUNK_BASENAMES as readonly string[]).includes(p);
}

/**
 * Subset of junk-basename candidates whose exclusion `git check-ignore
 * --verbose` attributes to a junk-shaped .gitignore pattern (#388 review).
 * Mirrors cli/schist/git_ops.py _confirmed_junk: classification is by
 * CAUSE, not name — `secret*` matching `secret-plan~` is a content rule
 * silently eating a note, so that file must keep counting as blocking.
 *
 * On probe failure the unconfirmed candidates stay BLOCKING (returns an
 * empty set) — the opposite of the porcelain probe's availability-over-
 * strictness stance, because by this point ignored files are KNOWN to
 * exist and guessing "junk" is exactly the silent drop being guarded.
 */
async function confirmedJunk(vaultRoot: string, candidates: string[]): Promise<Set<string>> {
  if (candidates.length === 0) return new Set();
  // -z: NUL-separated <source> <linenum> <pattern> <pathname> records —
  // nothing is C-quoted. git only accepts -z with --stdin, so candidates
  // go on stdin (NUL-terminated) rather than argv. Exit 0 = >=1 path
  // ignored (expected: porcelain said ALL are); 1 = none; 128 = error.
  // runCommand treats non-zero as !ok, which correctly confirms nothing
  // for both failure shapes.
  const probe = await runCommand(
    "git",
    ["check-ignore", "--verbose", "--stdin", "-z"],
    {
      cwd: vaultRoot,
      timeoutMs: SYNC_STATUS_TIMEOUT_MS,
      env: process.env,
      capture: true,
      stdin: candidates.join("\0") + "\0",
    },
  );
  if (!probe.ok) return new Set();
  const fields = (probe.stdout ?? "").split("\0");
  const confirmed = new Set<string>();
  // The trailing NUL leaves a final "" element; walk whole 4-field records.
  for (let i = 0; i + 3 < fields.length; i += 4) {
    const pattern = fields[i + 2];
    const pathname = fields[i + 3];
    if (isJunkShapedPattern(pattern)) confirmed.add(pathname);
  }
  return new Set(candidates.filter((c) => confirmed.has(c)));
}

/** Pathspecs the spoke's scope stages — mirrors cli/schist/git_ops.py
 *  _scope_targets so the probe below looks at exactly what `schist sync push`
 *  stages. Returns [] when the vault isn't a spoke or spoke.yaml is
 *  unreadable (probe is then skipped). */
async function spokeScopeTargets(vaultRoot: string): Promise<string[]> {
  try {
    const raw = yamlLoad(
      await fs.readFile(path.join(vaultRoot, ".schist", "spoke.yaml"), "utf-8"),
    ) as Record<string, unknown> | null;
    const scope = raw && typeof raw.scope === "string" ? raw.scope : "";
    if (!scope) return [];
    // Unlike git_ops._global_scope_targets, no existence/tracked filtering:
    // `git status` accepts pathspecs that match nothing (exit 0, no output),
    // so passing every canonical dir is equivalent and one probe cheaper.
    if (scope === "global") return loadCanonicalDirectories().map((d) => `${d}/`);
    return [scope.replace(/\/+$/, "") + "/"];
  } catch {
    return [];
  }
}

/**
 * #388: the same ignored-files probe the CLI ignore guard runs
 * (cli/schist/git_ops.py ignored_scope_files), minus confirmed junk.
 * Non-empty means `schist sync push` would hard-fail (#361) even though
 * plain `git status --porcelain` — and thus clean_working_tree — omits
 * ignored files entirely. Porcelain-probe failures return []
 * (availability over strictness, same as the CLI guard).
 */
async function blockingIgnoredScopeFiles(vaultRoot: string): Promise<string[]> {
  const targets = await spokeScopeTargets(vaultRoot);
  if (targets.length === 0) return [];
  // quotePath=off matches the CLI probe: porcelain v1 would C-quote
  // non-ASCII paths, garbling them in the reported list.
  const probe = await runGit(
    vaultRoot,
    ["-c", "core.quotePath=off", "status", "--porcelain", "--ignored=matching", "--", ...targets],
    SYNC_STATUS_TIMEOUT_MS,
  );
  if (!probe.ok) return [];
  const ignored = (probe.stdout ?? "")
    .split(/\r?\n/)
    .filter((line) => line.startsWith("!! "))
    .map((line) => line.slice(3));
  // Two-step junk classification, same as the CLI guard: basename shape
  // only nominates a candidate; the exclusion must also be attributed to a
  // junk-shaped pattern. Common path (no junk-looking files) pays nothing.
  const confirmed = await confirmedJunk(vaultRoot, ignored.filter(isJunkBasename));
  return ignored.filter((p) => !confirmed.has(p));
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

    // #388: clean_working_tree can read true while a push would still
    // hard-fail on an ignored-only change — report that skew explicitly.
    const blockingIgnored = isSpoke ? await blockingIgnoredScopeFiles(vaultRoot) : [];

    return {
      is_spoke: isSpoke,
      spoke_head: head.stdout?.trim() ?? "",
      hub_head: hubHead,
      ahead,
      behind,
      last_sync_error: sentinel ? { timestamp: sentinel.timestamp, contents: sentinel.contents } : null,
      clean_working_tree: clean.ok ? (clean.stdout ?? "").trim().length === 0 : false,
      blocked_by_ignored: blockingIgnored.length > 0,
      blocking_ignored_paths: blockingIgnored.slice(0, 10),
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
  // Ingest generation for cursor staleness (#90): a commit between pages
  // rebuilds the vault DB and reorders OFFSET rows. Resolve once for both
  // decode (reject a cursor from an older generation) and issue.
  const generation = await vaultGeneration(vaultRoot);
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME, generation);
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
      generation,
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

/**
 * Yields each `## Connections` edge line in a raw body that ingest would
 * index. Mirrors cli/schist/ingest.py's parse_connections: the same
 * splitlines() line boundaries (splitLinesLikePython, NOT split("\n") — see
 * #359), the same section tracking, the same CONNECTION_RE, and the same
 * bracket-reference skip. Anything else (malformed lines, non-edge lines)
 * is skipped, matching ingest. Lines are yielded trimmed.
 */
function* bodyConnectionEdgeLines(body: string): Generator<{ line: string; type: string }> {
  let inSection = false;
  for (const rawLine of splitLinesLikePython(body)) {
    const stripped = rawLine.trim();
    if (stripped.startsWith("## Connections")) {
      inSection = true;
      continue;
    }
    if (inSection && stripped.startsWith("## ")) break;
    if (!inSection || !stripped.startsWith("- ")) continue;
    const m = stripped.match(CONNECTION_RE);
    if (!m) continue; // malformed line — ingest skips it
    if (m[2].startsWith("[")) continue; // bracket reference — ingest skips it
    yield { line: stripped, type: m[1] };
  }
}

/**
 * #317: validate the connection-type vocabulary for a `## Connections`
 * section embedded in a raw body (i.e. NOT generated from structured
 * `connections`). Exactly the lines ingest would index as edges are the
 * lines checked here (see bodyConnectionEdgeLines).
 *
 * #363: `grandfathered` (trimmed connection lines already present in the
 * note's current on-disk body) exempts pre-existing edges from the
 * vocabulary check. A note authored before the vocabulary existed (or via
 * the Python CLI, or a direct git edit) may carry an out-of-vocabulary
 * type; without the exemption, every full-body update_note — even one
 * touching unrelated prose — would hard-fail until that line is fixed.
 * Only lines NOT in the current body are new edges and stay hard errors.
 */
function validateBodyConnectionTypes(
  body: string,
  config: VaultConfig,
  grandfathered?: ReadonlySet<string>
): ToolError | null {
  for (const { line, type } of bodyConnectionEdgeLines(body)) {
    if (config.connectionTypes.includes(type)) continue;
    if (grandfathered?.has(line)) continue; // pre-existing edge — #363
    return {
      error: "VALIDATION_ERROR",
      message:
        `connection type must be one of: ${config.connectionTypes.join(", ")} ` +
        `(got "${type}" in body line "${line}")`,
    } satisfies ToolError;
  }
  return null;
}

/**
 * Validate a connection target for both write paths (add_connection and
 * create_note's structured-connections loop). One definition so the two sites
 * can't drift — the manual-mirroring burden that let #398's guard land in one
 * place and #408's round-trip gap open in the other (#408). Two checks with
 * distinct messages: the boundary check keeps the precise #398 wording (its
 * set is a strict subset of the round-trip check's SLUG_WS_CHARS, so it fires
 * first for the boundary case). Coerce with String() because args are NOT
 * schema-validated at runtime (index.ts casts unchecked) — a non-string
 * target stringifies WITH its contents when buildConnectionLine interpolates
 * it, so the guard must check exactly that written form. A nullish target is
 * "missing" → treated as empty "", never the literal token "undefined".
 * On success the COERCED string is returned and callers must write THAT, not
 * the raw value — validating one coercion and serializing another re-opens
 * the gap for a stateful toString() (clean on the validation call, payload on
 * the interpolation call). Coerce once, validate it, write it.
 */
function validateConnectionTarget(target: unknown): { error: ToolError } | { target: string } {
  const written = target == null ? "" : String(target);
  if (containsLineBoundary(written)) {
    return {
      error: {
        error: "VALIDATION_ERROR",
        message: "connection target must not contain line-break characters",
      } satisfies ToolError,
    };
  }
  if (!isRoundTrippableTarget(written)) {
    return {
      error: {
        error: "VALIDATION_ERROR",
        message: "connection target must be a non-empty token without whitespace (the connection line could not round-trip through the parser)",
      } satisfies ToolError,
    };
  }
  // A '['-leading target round-trips through CONNECTION_RE but Python ingest
  // explicitly skips bracket references (ingest.py parse_connections), so the
  // write would succeed and commit while the edge is never indexed — the same
  // silent-no-op failure mode as a whitespace target, reached one parser
  // later. Reject at write time; the read-side TS/Python divergence on
  // pre-existing bracket lines is tracked in #415.
  if (written.startsWith("[")) {
    return {
      error: {
        error: "VALIDATION_ERROR",
        message: "connection target must not start with '[' (bracket references are skipped by the indexer, so the edge would never be indexed)",
      } satisfies ToolError,
    };
  }
  return { target: written };
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
    if (args.tags !== undefined) {
      const tagsError = validateTags(args.tags, "tags");
      if (tagsError !== null) return tagsError;
    }
    if (args.concepts !== undefined) {
      const arrayError = validateNonEmptyStringArray(args.concepts, "concepts");
      if (arrayError !== null) return arrayError;
    }
    // #276: the JSON-Schema enum in tool-registry is a client-side hint only;
    // enforce server-side like update_note's frontmatter_patch.status check.
    // Validate the RESOLVED value, not just an explicit arg: on a vault whose
    // custom statuses exclude "draft", the bare default must not slip an
    // out-of-vocabulary status onto disk — fall back to the vault's first
    // configured status instead.
    const status = args.status
      ?? (config.statuses.includes("draft") ? "draft" : config.statuses[0]);
    if (!config.statuses.includes(status)) {
      return {
        error: "VALIDATION_ERROR",
        message: `status must be one of: ${config.statuses.join(", ")} (got "${status}")`,
      } satisfies ToolError;
    }
    // #317: a non-array `connections` (object, string) previously fell
    // through to the for-of below — `{}` threw TypeError (surfaced as a
    // misleading GIT_ERROR) and a string iterated per-character. Shape-check
    // first so both come back as typed validation failures.
    if (args.connections !== undefined && !Array.isArray(args.connections)) {
      return {
        error: "VALIDATION_ERROR",
        message: "connections must be an array of { target, type, context? } objects",
      } satisfies ToolError;
    }
    // #304: connections carry a controlled type vocabulary (vault.yaml
    // connection_types); unchecked strings silently drift the graph taxonomy
    // and a type with whitespace/newlines produces `## Connections` lines
    // CONNECTION_RE can't round-trip.
    for (const conn of args.connections ?? []) {
      if (!config.connectionTypes.includes(conn.type)) {
        return {
          error: "VALIDATION_ERROR",
          message: `connection type must be one of: ${config.connectionTypes.join(", ")} (got "${conn.type}")`,
        } satisfies ToolError;
      }
      // #398 (line boundaries) + #408 (empty/whitespace round-trip): both
      // checks live in validateConnectionTarget so this loop and add_connection
      // can't drift. Reassign the validated coercion so buildNote serializes
      // exactly the string the guard checked (see the helper's contract).
      const validated = validateConnectionTarget(conn.target);
      if ("error" in validated) return validated.error;
      conn.target = validated.target;
      // context is sanitized (not rejected) downstream, but sanitizeContext
      // assumes a string — a JSON number/array context would TypeError inside
      // it and surface as a misleading GIT_ERROR. Same String()-coercion rule
      // as the target (#402: cover every free-text field on the line).
      if (conn.context != null) conn.context = String(conn.context);
    }
    // #317: the #304 loop above only covers STRUCTURED connections. buildNote
    // rewrites the `## Connections` section only when structured connections
    // are passed; with none (or an empty array) a section written literally
    // in `body` reaches disk verbatim and ingest indexes its edges with
    // out-of-vocabulary types. Validate at the same write boundary.
    if (args.connections === undefined || args.connections.length === 0) {
      const bodyConnError = validateBodyConnectionTypes(args.body, config);
      if (bodyConnError !== null) return bodyConnError;
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
    // the target path is taken so we never silently overwrite a note or
    // produce a git "nothing to commit" error (#408, CLI sibling #406).
    // The race-safe design has two parts, split across this function and
    // writeNote:
    // - the O_EXCL write in writeNote is the AUTHORITATIVE guard (see its
    //   retry loop below). It closes both the concurrent-create race and the
    //   same-second sequential case the old single-probe design lost.
    // - a DANGLING symlink at the write path is NOT a write-through risk on
    //   the MCP side: withWriteLock's in-lock assertResolvesInside (#323)
    //   lstats the path post-checkout and rejects any symlink with
    //   PATH_TRAVERSAL before writeFile runs. (This differs from the CLI,
    //   whose write path has no such in-lock guard — do NOT delete #323
    //   assuming the pre-probe covers it; the pre-probe is pre-lock and
    //   race-able.) The pre-probe's lstat only lets a stray symlink be
    //   suffixed AROUND (parity with the CLI's lexists) rather than turned
    //   into a hard error.
    // pathTaken is a BEST-EFFORT pre-probe only — it picks a nice filename and
    // lets a stray symlink at a candidate be suffixed around (parity with the
    // CLI's lexists) instead of hard-erroring in-lock. It is NOT the collision
    // guard: the authoritative, race-safe check is writeNote's O_EXCL write
    // below (a pre-lock probe is a TOCTOU — see the retry loop). ENOENT is the
    // only "free" signal; any OTHER lstat error (EACCES, EMFILE, ELOOP) is
    // rethrown rather than mis-read as "path free" (#401's distinguish-error-
    // kinds lesson — a bare catch here silently disabled the guard under fd
    // exhaustion). The rethrow is a ToolError shape so normalizeError passes
    // it through as a typed IO_ERROR carrying the vault-RELATIVE path — a raw
    // Node error here surfaced as GIT_ERROR (nothing git-related failed) with
    // the absolute vault path and a stack in details.
    const pathTaken = async (rel: string): Promise<boolean> => {
      try {
        await fs.lstat(path.join(vaultRoot, rel));
        return true;
      } catch (e) {
        const code = e !== null && typeof e === "object" && "code" in e
          ? (e as { code?: string }).code
          : undefined;
        if (code === "ENOENT") return false;
        throw {
          error: "IO_ERROR",
          message: `cannot probe candidate path "${rel}"${code ? ` (${code})` : ""} — refusing to treat an unreadable path as free`,
          details: { code },
        } satisfies ToolError;
      }
    };
    const timeSuffix = new Date()
      .toISOString()
      .split("T")[1]
      .slice(0, 8)       // HH:MM:SS
      .replace(/:/g, "-"); // colons not safe in filenames on all OSes
    // Candidate filenames in priority order: base, then time-suffixed, then
    // -2, -3, … Single source of the sequence, consumed by the pre-probe and
    // the O_EXCL retry loop so both walk the same names.
    // Generator<string, never>: the compiler enforces the sequence is
    // infinite, so .next().value is string with no cast — a future return
    // path would surface as a type error here instead of an undefined
    // silently laundered through `as string`.
    function* candidatePaths(): Generator<string, never> {
      yield `${directory}/${date}-${slug}.md`;
      yield `${directory}/${date}-${slug}-${timeSuffix}.md`;
      for (let counter = 2; ; counter++) {
        yield `${directory}/${date}-${slug}-${timeSuffix}-${counter}.md`;
      }
    }
    const candidates = candidatePaths();
    // Advance to the first candidate the pre-probe reports free. Resumable:
    // the O_EXCL retry loop calls this again to skip past a name a concurrent
    // writer just took.
    const nextFreeCandidate = async (): Promise<string> => {
      for (;;) {
        const cand = candidates.next().value;
        if (!(await pathTaken(cand))) return cand;
      }
    };
    let relPath = await nextFreeCandidate();

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
      tags: args.tags !== undefined ? normalizeTags(args.tags) : [],
      // #302: mirror the tags normalization — ingest stores the normalized
      // slug in the index, so writing the raw value to disk makes the file
      // and the DB disagree on the concept identifier.
      concepts: args.concepts !== undefined ? args.concepts.map(normalizeConceptSlug) : [],
      status,
      source_agent: owner,
    };
    if (args.confidence !== undefined) {
      metadata.confidence = args.confidence;
    }
    if (typeof args.file_ref === "string" && args.file_ref) {
      metadata.file_ref = args.file_ref;
    }

    const noteContent = buildNote(metadata, args.body, args.connections);
    // O_EXCL write with EEXIST-retry: the ONLY race-safe collision guard
    // (#408). The pre-probe above chose relPath, but between that probe and
    // the write a concurrent create_note (the "vault push burst" pattern) can
    // take the same name — writeNote's exclusive flag makes that loser's write
    // throw EEXIST INSIDE the mutex, so we advance to the next free candidate
    // and retry instead of truncating the winner's note. The counter is
    // unbounded in principle; MAX_COLLISION_RETRIES caps the pathological case
    // (every candidate racing) so a wedged filesystem can't spin forever.
    const MAX_COLLISION_RETRIES = 50;
    let result: Awaited<ReturnType<typeof writeNote>> | undefined;
    for (let attempt = 0; ; attempt++) {
      try {
        result = await writeNote(vaultRoot, relPath, noteContent, args.title, owner, { exclusive: true });
        break;
      } catch (e) {
        // A filename collision is EEXIST from the exclusive OPEN specifically.
        // withWriteLock's mkdir(dirname, {recursive}) also throws EEXIST when
        // the parent dir exists as a FILE on the write branch (branch-skew) —
        // syscall-blind retrying would misread that as a collision and burn a
        // full checkout cycle per candidate before failing anyway.
        const err = e !== null && typeof e === "object" ? (e as { code?: string; syscall?: string }) : {};
        const isCollision = err.code === "EEXIST" && err.syscall === "open";
        if (!isCollision) throw e;
        if (attempt >= MAX_COLLISION_RETRIES) {
          // Typed instead of rethrowing the raw EEXIST: the outer catch's
          // normalizeError fallback is GIT_ERROR (nothing git-related failed)
          // and the raw Node message carries the absolute vault path.
          return {
            error: "COLLISION_RETRIES_EXHAUSTED",
            message: `could not find a free filename for "${directory}/${date}-${slug}*.md" after ${MAX_COLLISION_RETRIES} collision retries — extreme concurrent create_note contention on this title; retry, or vary the title`,
          } satisfies ToolError;
        }
        relPath = await nextFreeCandidate();
      }
    }

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
      ...(result.commitWarning ? { commitWarning: result.commitWarning } : {}),
      ...(syncWarning !== undefined ? { syncWarning } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function add_connection(
  vaultRoot: string,
  args: { owner: string; source: string; target: string; type: string; context?: string },
  config: VaultConfig
): Promise<unknown> {
  try {
    // Identity gate (#63): same ordering as create_note. Reassign to the
    // canonicalized owner for downstream stamps.
    const owner = validateOwner(args.owner);
    const syncDirty = await blockWriteIfSyncDirty(vaultRoot);
    if (syncDirty !== null) return syncDirty;
    // #304: same controlled-vocabulary check as create_note's connections.
    if (!config.connectionTypes.includes(args.type)) {
      return {
        error: "VALIDATION_ERROR",
        message: `connection type must be one of: ${config.connectionTypes.join(", ")} (got "${args.type}")`,
      } satisfies ToolError;
    }
    // #398 (line boundaries) + #408 (empty/whitespace round-trip): a forged
    // multi-line target would index an extra edge only the SOURCE write was
    // ACL-gated for; an empty/whitespace target writes a line that never
    // round-trips. Shared with create_note via validateConnectionTarget so
    // the two sites can't drift. Ordering note: this VALIDATION_ERROR fires
    // before the ACL check below — a malformed request is rejected on its
    // own merits and we don't disclose grant state for input that could
    // never have produced an edge.
    const validatedTarget = validateConnectionTarget(args.target);
    if ("error" in validatedTarget) return validatedTarget.error;
    // Write (and echo back) the validated coercion, never the raw value —
    // see validateConnectionTarget's coerce-once contract.
    args.target = validatedTarget.target;
    // Same String()-coercion for context as create_note's structured loop:
    // sanitizeContext assumes a string and a non-string context would
    // TypeError into a misleading GIT_ERROR.
    if (args.context != null) args.context = String(args.context);
    const srcIdError = validateNoteId(args.source, config);
    if (srcIdError !== null) return srcIdError;
    const filePath = path.join(vaultRoot, args.source);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Source path is outside vault root" } satisfies ToolError;
    }
    if (!(await resolvesInsideVault(vaultRoot, args.source))) {
      return { error: "PATH_TRAVERSAL", message: "Source path resolves outside the vault (symlink?)" } satisfies ToolError;
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

    // Line-scan insert shared with the CLI's append_connection (parity pinned
    // by schema/connection-append-parity.json). The former regex here anchored
    // on a bare `## Connections\n`, so a CRLF note silently dropped the edge
    // while reporting success (#366) — see insertConnectionLine's contract.
    const newContent = insertConnectionLine(content, connLine);

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
      ...(result.commitWarning ? { commitWarning: result.commitWarning } : {}),
      ...(syncWarning !== undefined ? { syncWarning } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

/**
 * Tokens a `## Connections` line might use to reference the note `id`. Always
 * the full vault path; for a concept note (`concepts/<slug>.md`) also the bare
 * `<slug>`, because edges to concepts are stored either way (see
 * sqlite-reader's conceptEdgeJoinCondition). Without this, deleting a concept
 * note would miss inbound edges written as the bare slug.
 */
function noteRefTokens(id: string): string[] {
  const tokens = new Set<string>([id]);
  const m = id.match(/^concepts\/(.+)\.md$/);
  if (m) tokens.add(m[1]);
  return [...tokens];
}

// Explicit whitespace set shared verbatim with cli/schist/ingest.py's
// _normalize_concept_slug — the UNION of both engines' \s sets, so either
// language's notion of whitespace becomes a slug separator (#303/#318).
// Single-sourced in markdown-parser.ts since #338 extended it to title
// slugs and connection lines. schema/concept-slug-parity.json pins both
// implementations to the same table.
const SLUG_WS_SET = new Set(SLUG_WS_CHARS);
// None of the members are regex metacharacters, so they can sit in a class raw.
const SLUG_WS_RUN = new RegExp(`[${SLUG_WS_CHARS}]+`, "g");

/**
 * Edge-strip via index scan — LINEAR — not a `^[ws]+|[ws]+$` regex, which
 * backtracks quadratically over interior whitespace runs (~6s on a
 * 100k-space string; `concepts` args reach this with no length validation,
 * and the server is single-threaded).
 */
function trimSlugWs(value: string): string {
  let start = 0;
  let end = value.length;
  while (start < end && SLUG_WS_SET.has(value[start])) start++;
  while (end > start && SLUG_WS_SET.has(value[end - 1])) end--;
  return start === 0 && end === value.length ? value : value.slice(start, end);
}

/** @internal — exported for the schema/concept-slug-parity.json fixture test. */
export function normalizeConceptSlug(value: string): string {
  return trimSlugWs(value).toLowerCase().replace(SLUG_WS_RUN, "-");
}

/**
 * Remove every `## Connections` line in `content` whose target matches any of
 * `targets`. Returns the rewritten content and the count removed. Used by
 * delete_note's cascade path to repair notes that linked to a deleted note.
 * Matching mirrors markdown-parser's parseConnections (trim, CONNECTION_RE,
 * group 2 = target) so the lines we strip are exactly the edges ingest produced.
 */
function stripConnectionsTo(content: string, targets: string[]): { content: string; removed: number } {
  const targetSet = new Set(targets);
  const out: string[] = [];
  let inSection = false;
  let removed = 0;
  // splitLinesLikePython (not split("\n")): ingest indexed the edge across
  // whatever splitlines() boundary separated it from the heading, so cascade
  // repair must recognize the same boundaries or the dangling edge survives
  // (#359). Reconstruction rejoins with "\n" — repair normalizes exotic line
  // separators to LF, which is the canonical form create_note/buildNote emit.
  // splitLinesLikePython keeps no trailing empty segment, so join("\n") on
  // its output drops the source's terminal newline at BOTH exits below —
  // cascade repairs then wrote notes without a trailing "\n" (#382; the
  // push("") special case only healed the emptied-section shape). Repair is
  // normalizing (see above), so every non-empty result gets the canonical
  // LF terminator, matching insertConnectionLine's contract.
  const finalize = (lines: string[]): string => {
    const s = lines.join("\n");
    return s === "" || s.endsWith("\n") ? s : s + "\n";
  };
  for (const line of splitLinesLikePython(content)) {
    const stripped = line.trim();
    if (stripped.startsWith("## Connections")) {
      inSection = true;
      out.push(line);
      continue;
    }
    if (inSection && stripped.startsWith("## ")) {
      inSection = false;
      out.push(line);
      continue;
    }
    if (inSection) {
      const m = stripped.match(CONNECTION_RE);
      if (m && targetSet.has(m[2])) {
        removed++;
        continue;
      }
    }
    out.push(line);
  }
  if (removed === 0) return { content: finalize(out), removed };

  const cleaned: string[] = [];
  for (let i = 0; i < out.length; i++) {
    const stripped = out[i].trim();
    if (!stripped.startsWith("## Connections")) {
      cleaned.push(out[i]);
      continue;
    }

    let j = i + 1;
    while (j < out.length && out[j].trim() === "") j++;
    const next = j < out.length ? out[j].trim() : "";
    if (j >= out.length || next.startsWith("## ")) {
      while (cleaned.length > 0 && cleaned[cleaned.length - 1].trim() === "") cleaned.pop();
      // Push a single "" either way: as the blank separator before the next
      // section (j < out.length), or — when Connections was the last section
      // (j >= out.length) — as the terminal element that join("\n") turns back
      // into the file's trailing newline (#280; finalize below now also
      // guarantees the terminator for every other shape).
      cleaned.push("");
      i = j - 1;
      continue;
    }

    cleaned.push(out[i]);
  }

  return { content: finalize(cleaned), removed };
}

/**
 * Remove any of `targets` from a note's `concepts:` frontmatter list. Edges to
 * a concept are created by ingest BOTH from `## Connections` lines AND from the
 * bare slug in another note's `concepts:` frontmatter (ingest.py inserts a
 * `references` edge per frontmatter concept). delete_note's cascade strips the
 * body lines via stripConnectionsTo; without this, the frontmatter reference
 * survives and the very next ingest recreates the deleted concept as a
 * placeholder plus a dangling edge — so cascade-deleting a concept wouldn't
 * stick. Returns the rewritten content and whether anything changed. #119.
 */
function stripConceptRefs(content: string, targets: string[]): { content: string; changed: boolean } {
  const { metadata, body } = parseNote(content);
  const concepts = metadata.concepts;
  if (!Array.isArray(concepts)) return { content, changed: false };
  const targetSet = new Set(targets.flatMap((target) => [target, normalizeConceptSlug(target)]));
  const filtered = concepts.filter((c) => typeof c !== "string" || !targetSet.has(normalizeConceptSlug(c)));
  if (filtered.length === concepts.length) return { content, changed: false };
  const newMeta: Record<string, unknown> = { ...metadata, concepts: filtered };
  // Same Date-coercion guard as update_note: don't let the round-trip rewrite
  // an untouched `date:` into a full ISO timestamp.
  for (const [key, value] of Object.entries(newMeta)) {
    if (value instanceof Date) newMeta[key] = value.toISOString().split("T")[0];
  }
  return { content: buildNote(newMeta, body), changed: true };
}

/**
 * delete_note tool handler (#119). Removes a note via `git rm`.
 *
 * Inbound-edge safety: by default, refuses with INBOUND_EDGES if other notes
 * link to this one (listing them so the caller can clean up first). Pass
 * cascade=true to delete anyway AND strip the now-dangling `## Connections`
 * lines from those linking notes in the same commit.
 *
 * Mirrors create_note/add_connection ordering: identity gate → sync-dirty gate
 * → path-traversal guard → existence → ACL → mutate → trigger ingest + push.
 */
export async function delete_note(
  vaultRoot: string,
  args: { owner: string; id: string; cascade?: boolean },
  config: VaultConfig
): Promise<unknown> {
  try {
    const owner = validateOwner(args.owner);
    const syncDirty = await blockWriteIfSyncDirty(vaultRoot);
    if (syncDirty !== null) return syncDirty;

    const idError = validateNoteId(args.id, config);
    if (idError !== null) return idError;

    const filePath = path.join(vaultRoot, args.id);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Note path is outside vault root" } satisfies ToolError;
    }

    try {
      await fs.access(filePath);
    } catch {
      return { error: "NOT_FOUND", message: `Note not found: ${args.id}` } satisfies ToolError;
    }

    // Resolve symlinks (see resolvesInsideVault): a tracked symlink at args.id
    // pointing outside the vault would otherwise let `git rm` operate out of
    // tree. The lexical guard above is string-only. #119.
    if (!(await resolvesInsideVault(vaultRoot, args.id))) {
      return { error: "PATH_TRAVERSAL", message: "Note path resolves outside the vault (symlink?)" } satisfies ToolError;
    }

    // ACL — mirror create_note/add_connection. args.id is the vault-relative
    // path; same scope derivation the hub's pre_receive uses.
    const acl = loadVaultAcl(vaultRoot);
    const aclIdentity = acl !== null ? resolveAclIdentity(owner) : "";
    if (acl !== null) {
      const scope = deriveScope(args.id);
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

    // Inbound edges from the graph index. Surfaced for refusal, or repaired on
    // cascade. The index can lag a freshly-written note (see inboundEdges doc);
    // we re-read each source from disk below so a stale row is harmless.
    //
    // If the index can't be read at all (brand-new vault with no DB yet,
    // corrupt index), we proceed rather than block: the delete is a `git rm`
    // and fully recoverable from history, so the worst case is a dangling
    // reference left in another note — surfaced via indexWarning so the caller
    // can repair it. The ACL gate above (the real security boundary) is
    // independent of the index.
    const refTokens = noteRefTokens(args.id);
    let inbound: Array<{ source: string; type: string }> = [];
    let indexWarning: string | undefined;
    try {
      inbound = sqliteReader.inboundEdges(vaultRoot, refTokens, args.id);
    } catch {
      indexWarning =
        "Inbound-edge check skipped: the graph index could not be read " +
        "(run `schist ingest` to build it). Any references to this note in " +
        "other notes were not detected or repaired.";
    }

    if (inbound.length > 0 && args.cascade !== true) {
      return {
        error: "INBOUND_EDGES",
        message:
          `Cannot delete '${args.id}': ${inbound.length} note(s) link to it. ` +
          `Remove those connections first, or pass cascade=true to delete and ` +
          `auto-strip the dangling references.`,
        inbound_edges: inbound,
      } satisfies ToolError & { inbound_edges: Array<{ source: string; type: string }> };
    }

    // Cascade: strip dangling connection lines from each distinct linking note.
    const repairs: Array<{ relPath: string; content: string }> = [];
    if (args.cascade === true && inbound.length > 0) {
      const sources = [...new Set(inbound.map((e) => e.source))];
      for (const source of sources) {
        // The source path comes from the graph index, which indexes EVERY *.md
        // outside hidden dirs — not just configured note dirs. Re-validate it
        // as a note id (and resolve symlinks) before we write to it, so a
        // cascade can't mutate a non-note file or follow a symlink out of the
        // vault. A source that fails validation is skipped, not repaired. #119.
        if (validateNoteId(source, config) !== null) continue;
        if (!(await resolvesInsideVault(vaultRoot, source))) continue;
        let content: string;
        try {
          content = await fs.readFile(path.join(vaultRoot, source), "utf-8");
        } catch {
          // Source vanished since the index was built — nothing to repair.
          continue;
        }
        // Strip both the body `## Connections` lines AND any matching
        // `concepts:` frontmatter entry (ingest derives `references` edges from
        // both; repairing only the body would let the next ingest resurrect a
        // deleted concept — see stripConceptRefs).
        const { content: bodyStripped, removed } = stripConnectionsTo(content, refTokens);
        const { content: repaired, changed } = stripConceptRefs(bodyStripped, refTokens);
        if (removed > 0 || changed) repairs.push({ relPath: source, content: repaired });
      }

      // Cascade repairs MUTATE other notes — they must each pass the caller's
      // write ACL, or a delete grant becomes a license to edit notes in
      // scopes the caller can't write (which the hub pre-receive would then
      // reject anyway). Refuse the whole cascade if any linker is out of scope.
      if (acl !== null) {
        const denied = repairs
          .map((r) => r.relPath)
          .filter((rel) => !canWrite(acl, aclIdentity, deriveScope(rel)));
        if (denied.length > 0) {
          return {
            error: "ACL_DENIED",
            message:
              `Cascade would modify notes outside your write scope: ${denied.join(", ")}. ` +
              `Clean up those connections yourself, or ask the hub admin to extend your grant.`,
          } satisfies ToolError;
        }
      }
    }

    let title = args.id;
    try {
      title = (parseNote(await fs.readFile(filePath, "utf-8")).metadata.title as string) || args.id;
    } catch {
      // fall back to id for the commit subject
    }

    const result = await deleteNote(vaultRoot, args.id, title, owner, repairs);

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    const syncWarning = await readSyncWarning(vaultRoot);
    return {
      id: args.id,
      deleted: true,
      commitSha: result.commitSha,
      repaired: repairs.map((r) => r.relPath),
      ...(result.commitWarning ? { commitWarning: result.commitWarning } : {}),
      ...(indexWarning !== undefined ? { indexWarning } : {}),
      ...(syncWarning !== undefined ? { syncWarning } : {}),
    };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

/**
 * update_note tool handler (#119). Replaces a note's body and/or patches its
 * frontmatter in place. At least one of `body` / `frontmatter_patch` is
 * required.
 *
 * - `body`: replaces the markdown body verbatim (including any `## Connections`
 *   section — the caller owns the full body when they pass it).
 * - `frontmatter_patch`: shallow-merged into existing frontmatter. A `null`
 *   value deletes that key. Does NOT rename the file even when `title` changes
 *   (filename and title may diverge, same as create_note).
 *
 * Body is left untouched when only `frontmatter_patch` is given, so existing
 * connections survive. The dedup path makes a no-op update return committed:
 * false. Mirrors create_note ordering for the identity / sync / ACL gates.
 */
export async function update_note(
  vaultRoot: string,
  args: {
    owner: string;
    id: string;
    body?: string;
    frontmatter_patch?: Record<string, unknown>;
  },
  config: VaultConfig
): Promise<unknown> {
  try {
    const owner = validateOwner(args.owner);
    const syncDirty = await blockWriteIfSyncDirty(vaultRoot);
    if (syncDirty !== null) return syncDirty;

    if (args.body === undefined && args.frontmatter_patch === undefined) {
      return {
        error: "VALIDATION_ERROR",
        message: "update_note requires at least one of: body, frontmatter_patch",
      } satisfies ToolError;
    }
    if (
      args.frontmatter_patch !== undefined &&
      (typeof args.frontmatter_patch !== "object" ||
        args.frontmatter_patch === null ||
        Array.isArray(args.frontmatter_patch))
    ) {
      return {
        error: "VALIDATION_ERROR",
        message: "frontmatter_patch must be an object",
      } satisfies ToolError;
    }
    const patchError = validateFrontmatterPatch(args.frontmatter_patch, config);
    if (patchError !== null) return patchError;

    const idError = validateNoteId(args.id, config);
    if (idError !== null) return idError;

    const filePath = path.join(vaultRoot, args.id);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Note path is outside vault root" } satisfies ToolError;
    }

    let existing: string;
    try {
      existing = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Note not found: ${args.id}` } satisfies ToolError;
    }

    // #317: update_note never passes structured connections to buildNote, so
    // a replaced body carries its `## Connections` section to disk verbatim —
    // the same vocabulary bypass as create_note's raw-body path.
    //
    // #363: unlike create_note, GRANDFATHER edges already present in the
    // current on-disk body (trimmed exact-line match). This check runs after
    // the read above (it needs the current body), so a bad body against a
    // missing note now reports NOT_FOUND first. The grandfather set comes
    // from the pre-lock read — same staleness window the commit subject
    // already accepts; the transform still validates nothing in-lock, so a
    // racing rewrite can at worst re-allow a line that was on disk moments
    // ago, never a line that was never on disk.
    if (args.body !== undefined) {
      const grandfathered = new Set(
        Array.from(bodyConnectionEdgeLines(parseNote(existing).body), (e) => e.line)
      );
      const bodyConnError = validateBodyConnectionTypes(args.body, config, grandfathered);
      if (bodyConnError !== null) return bodyConnError;
    }

    // Resolve symlinks: validateNoteId + the lexical guard above are purely
    // string-based, so a tracked symlink inside a note dir (e.g.
    // notes/x.md -> ../.git/hooks/post-commit) would pass and the write would
    // follow it out of the vault (arbitrary-file-write / RCE). Compare the
    // real path against the real vault root. #119.
    if (!(await resolvesInsideVault(vaultRoot, args.id))) {
      return { error: "PATH_TRAVERSAL", message: "Note path resolves outside the vault (symlink?)" } satisfies ToolError;
    }

    const acl = loadVaultAcl(vaultRoot);
    if (acl !== null) {
      const scope = deriveScope(args.id);
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

    // Build the new content from the note's FRESH on-disk state, read inside
    // the write lock (see updateNote). Computing it here from `existing` (read
    // before the lock) would let a concurrent delete + rewrite resurrect a
    // just-deleted note with stale content. #119.
    const transform = (current: string): string => {
      const { metadata, body } = parseNote(current);
      const mergedBody = args.body !== undefined ? args.body : body;
      const mergedMeta: Record<string, unknown> = { ...metadata };
      if (args.frontmatter_patch !== undefined) {
        for (const [key, value] of Object.entries(args.frontmatter_patch)) {
          if (value === null) delete mergedMeta[key];
          // #302: concepts normalize like tags do — see create_note.
          else if (key === "tags") mergedMeta[key] = normalizeTags(value as string[]);
          else if (key === "concepts") mergedMeta[key] = (value as string[]).map(normalizeConceptSlug);
          else mergedMeta[key] = value;
        }
      }
      // gray-matter parses YAML timestamps into JS Dates; matter.stringify would
      // re-emit them as full ISO datetimes, silently rewriting `date: 2026-06-18`
      // to `2026-06-18T00:00:00.000Z` on EVERY edit (even a body-only one). ingest
      // then stores the changed string, breaking date-equality queries. Coerce any
      // Date back to the date-only string create_note writes. #119.
      for (const [key, value] of Object.entries(mergedMeta)) {
        if (value instanceof Date) mergedMeta[key] = value.toISOString().split("T")[0];
      }
      // No connections arg: mergedBody already carries its own `## Connections`
      // section, so buildNote must not re-append one.
      return buildNote(mergedMeta, mergedBody);
    };

    // Commit subject only — derived from the pre-lock read. A retitle landing in
    // the lock window would make the subject slightly stale, but the committed
    // CONTENT is always built from the fresh in-lock read above.
    const commitTitle = (parseNote(existing).metadata.title as string) || args.id;
    const result = await updateNote(vaultRoot, args.id, commitTitle, owner, transform);

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    const syncWarning = await readSyncWarning(vaultRoot);
    return {
      id: args.id,
      updated: result.committed,
      commitSha: result.commitSha,
      ...(result.commitWarning ? { commitWarning: result.commitWarning } : {}),
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

  // Ingest generation for cursor staleness (#90); see search_notes.
  const generation = await vaultGeneration(vaultRoot);
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME, generation);
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
      generation,
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
  // Ingest generation for cursor staleness (#90); see search_notes.
  const generation = await vaultGeneration(vaultRoot);
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME, generation);
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
      generation,
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
    // searchNotes ranks globally; scope is applied in-memory below via
    // matchesScopePath (a path-prefix test, not the exact docs.scope column
    // searchNotes filters on). When a scope is requested, over-fetch so the
    // top window isn't fully consumed by higher-ranked out-of-scope notes,
    // which would otherwise leave the brief with zero topic matches (see #232).
    const searchLimit = scope.length > 0 ? Math.min(60, 12 * (scope.length + 4)) : 12;
    const searchRows = sqliteReader.searchNotes(vaultRoot, args.topic, { limit: searchLimit });
    for (const row of searchRows) {
      if (!matchesScopePath(row.id, scope)) continue;
      if (!byId.has(row.id)) byId.set(row.id, briefNoteFromSearch(row, "topic search"));
      addTags(tagCounts, row.tags);
      if (byId.size >= 5) break;
    }

    // No scope filter here (#259): scope narrows DISCOVERY (topic search
    // above, graph expansion below); related_notes is an explicit caller
    // directive — the caller already decided these belong in the brief, and
    // silently dropping them produced briefs missing requested context.
    for (const id of pinned) {
      const note = sqliteReader.getNote(vaultRoot, id);
      if (!note) continue;
      byId.set(id, briefNoteFromNote(note, "pinned"));
      addTags(tagCounts, note.tags);
    }

    // Cap the graph-expansion seeds. queryGraph binds the seed list three
    // times plus its own LIMIT/OFFSET (3 × seeds + 2 parameters); past ~332
    // seeds that exceeds SQLite's default 999-variable limit and surfaces as
    // an opaque INGEST_ERROR ("too many SQL variables"). A large related_notes
    // list (each added to byId above) is the usual trigger. Bounding the seeds
    // here loses nothing: the final brief is sliced to 10 notes regardless, and
    // pinned notes already sit in byId whether or not they seed graph lookups
    // (see #231).
    const MAX_GRAPH_SEEDS = 200;
    const seedIds = [...byId.keys()].slice(0, MAX_GRAPH_SEEDS);
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
  const recentOutcome = args.session_paths === false
    ? { ok: true, rows: [] }
    : await recentAddedPaths(vaultRoot, scope);
  const recent = recentOutcome.rows;
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
    : [recentOutcome.ok
        ? "- None found in recent local git history."
        : "- Recent git history unavailable (git log failed or timed out) — not necessarily empty."];
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
    ...(recentOutcome.ok ? {} : { recent_paths_unavailable: true as const }),
  };
}

// Snippet budget for get_context's recentMemory entries (docs/data-model.md
// D4). Half of search_memory's 200 — the block is an orientation teaser;
// full rows come from search_memory.
const RECENT_MEMORY_SNIPPET_CODE_POINTS = 100;

/**
 * get_context tool handler. Adopts reason-string verbose (#50 PR 7) and the
 * recentMemory block (docs/data-model.md D4, slice C).
 *
 *   parseVerbose → memory-owner resolution → effective-depth resolution →
 *   spoke pull → sentinel read → SQLite read → recentMemory append →
 *   optional logVerbose + noteHighFrequency → assemble response.
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
  args: { depth?: "minimal" | "standard" | "full"; verbose?: string; owner?: string }
): Promise<GetContextResponse | ToolError> {
  const TOOL_NAME = "get_context" as const;

  // Step 1: parseVerbose. Reject INVALID_ARG (boolean / non-string / too-short)
  // before any I/O. Whitespace-only and omitted both return { enabled: false }
  // with no error — handled identically below as "no verbose intent."
  const v = parseVerbose(args.verbose);
  if ("error" in v) return v.error;
  const verboseEnabled = v.enabled;
  const verboseReason: string | undefined = v.enabled ? v.reason : undefined;

  // Step 2: memory-owner resolution for the recentMemory block (slice C,
  // docs/data-model.md D4). Mirrors the memory write tools' identity policy:
  // an explicit `owner` arg goes through validateOwner (SCHIST_ALLOWED_AGENTS
  // allowlist when defined, else exact SCHIST_AGENT_ID match) and is rejected
  // up front — before any I/O — like the parseVerbose gate above. When
  // omitted, fall back to SCHIST_AGENT_ID (the memory identity axis;
  // SCHIST_AGENT_NAME is the vault.yaml axis and deliberately not consulted —
  // agent_memory.owner rows are stamped with validateOwner-checked ids). No
  // resolvable owner just means no recentMemory block: reads never require
  // identity to be configured.
  //
  // BOTH paths gate through validateOwner, but fail differently on purpose:
  // an explicit arg is a caller assertion, so rejecting it loudly is right;
  // the env fallback is ambient config, so an inconsistent pair (e.g.
  // SCHIST_ALLOWED_AGENTS set without SCHIST_AGENT_ID in it) degrades to an
  // absent block instead. Skipping validation on the fallback would let an
  // identity the allowlist refuses to write as still read its memory back —
  // the asymmetric-gating trap (PR #175): trigger and recovery must be gated
  // identically, and here the "recovery" is the always-safe absent block.
  let memoryOwner: string | undefined;
  if (args.owner !== undefined) {
    try {
      memoryOwner = validateOwner(args.owner);
    } catch (e: unknown) {
      return normalizeError(e, "VALIDATION_ERROR");
    }
  } else {
    const envAgentId = process.env.SCHIST_AGENT_ID?.trim();
    if (envAgentId) {
      try {
        memoryOwner = validateOwner(envAgentId);
      } catch {
        // Degrade, never error: a broken identity config must not break
        // vault context reads any more than a broken memory DB does.
      }
    }
  }

  // Step 3: effective depth resolution. If the caller asked for "full" but
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

  // Step 4: Surface any pending background-sync-failure sentinel without
  // clearing it. A dirty spoke should stay machine-readable until a successful
  // push path (`triggerSpokePush` or `sync_retry`) clears the sentinel; merely
  // reading context is not proof that local commits reached the hub.
  let syncWarning: string | undefined;
  syncWarning = await readSyncWarning(vaultRoot);

  // Step 5: SQLite read at effectiveDepth.
  let context: Record<string, unknown>;
  try {
    context = sqliteReader.getContext(vaultRoot, effectiveDepth) as Record<string, unknown>;
  } catch (e: unknown) {
    const err = normalizeError(e, "INGEST_ERROR");
    return syncWarning ? { ...err, syncWarning } : err;
  }
  if (syncWarning) context.syncWarning = syncWarning;

  // Step 6: recentMemory append (slice C, docs/data-model.md D4) — the
  // ephemeral fuel station, namespaced under its own key so vault-derived
  // fields and agent memory stay visually distinct. standard/full only;
  // "minimal" stays counts-only. getRecentMemory returns null — and the
  // block stays ABSENT, never an error — when the memory DB is missing,
  // unreadable, or lacks the agent_memory table: a broken fuel station must
  // not break vault context reads. Reachable-but-empty memory yields
  // entries: [] (a present block), which is deliberately distinct from
  // "unavailable".
  if (effectiveDepth !== "minimal" && memoryOwner !== undefined) {
    const memoryEntries = sqliteReader.getRecentMemory(memoryOwner);
    if (memoryEntries !== null) {
      context.recentMemory = {
        owner: memoryOwner,
        entries: memoryEntries.map((entry) => ({
          ...entry,
          content: snippetContent(entry.content, RECENT_MEMORY_SNIPPET_CODE_POINTS),
        })),
      };
    }
  }

  // Step 7: verbose audit log + rate-limit hint (only on the true depth="full"
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

  // Step 8: assemble response. verboseNote is set if either (a) the call was
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
    // related_doc is the memory → vault back-reference (docs/data-model.md
    // D4): shape-validated here, never existence-checked — see
    // validateRelatedDoc. `!= null` treats a JSON null like an omission
    // (both mean "no back-reference"; addMemory stores NULL either way).
    if (args.related_doc != null) {
      const relatedDocError = validateRelatedDoc(args.related_doc);
      if (relatedDocError !== null) return relatedDocError;
    }
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
    // #338/#317: create/update/delete normalize concept slugs before they hit
    // the index (#302/#303), so an alias stored raw ("Neural Networks") can
    // never match a concepts row — the FK insert fails at best, and at worst
    // the next ingest garbage-collects the orphan row silently. Normalize
    // both sides at the same boundary the other write tools use.
    const duplicateSlug = normalizeConceptSlug(args.duplicate_slug);
    const canonicalSlug = normalizeConceptSlug(args.canonical_slug);
    if (duplicateSlug === "" || canonicalSlug === "") {
      return {
        error: "VALIDATION_ERROR",
        message: "duplicate_slug and canonical_slug must be non-empty after normalization",
      } satisfies ToolError;
    }
    return sqliteReader.addConceptAlias(vaultRoot, duplicateSlug, canonicalSlug, args.reason, createdBy);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}
