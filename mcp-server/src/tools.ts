import * as fs from "fs/promises";
import * as path from "path";
import { spawn } from "child_process";
import { load as yamlLoad } from "js-yaml";
import * as sqliteReader from "./sqlite-reader.js";
import { writeNote } from "./git-writer.js";
import { buildNote, buildConnectionLine } from "./markdown-parser.js";
import { validateOwner } from "./agent-identity.js";
import type { VaultConfig, ToolError, SearchMemoryResponse, SearchNotesResponse, QueryGraphResponse } from "./types.js";
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
    directories: getStringList("directories", ["notes", "papers", "concepts"]),
    connectionTypes: getStringList("connection_types", [
      "extends", "contradicts", "supports", "replicates",
      "applies-method-of", "reinterprets", "related",
    ]),
    statuses: getStringList("statuses", ["draft", "review", "final", "archived"]),
    writeBranch: getString("write_branch", "drafts"),
  };
}

function triggerIngestion(vaultRoot: string): void {
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");

  // Spawn the `schist-ingest` console script registered by the schist
  // CLI package (cli/pyproject.toml). Works for both `pip install schist`
  // and `pip install -e ./cli` setups; ENOENTs cleanly if the CLI was
  // never installed (in which case the post-commit hook also can't run).
  const child = spawn("schist-ingest", ["--vault", vaultRoot, "--db", dbPath], {
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
 * to the caller on the next read.
 */
async function writeSyncError(vaultRoot: string, message: string): Promise<void> {
  try {
    const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
    await fs.mkdir(path.dirname(sentinelPath), { recursive: true });
    const entry = `${new Date().toISOString()} ${message}\n`;
    await fs.writeFile(sentinelPath, entry);
  } catch {
    // Can't write the sentinel either — truly nothing we can do.
  }
}

/** Fire-and-forget spoke push after a write. No-op for non-spoke vaults. */
export function triggerSpokePush(vaultRoot: string): void {
  const spokeConfig = path.join(vaultRoot, ".schist", "spoke.yaml");
  fs.access(spokeConfig).then(() => {
    const child = spawn(
      "python3",
      ["-m", "schist", "--vault", vaultRoot, "sync", "push"],
      { cwd: vaultRoot, stdio: "ignore", env: process.env, detached: true }
    );
    child.unref();
    child.on("error", (err) => {
      // spawn error = python3 not on PATH, or permission denied. Silent by
      // default is a footgun — write a sentinel so the next get_context can
      // surface it. Also log for operators watching stderr.
      console.error("[schist] spoke push failed:", err);
      writeSyncError(vaultRoot, `push spawn failed: ${err.message}`);
    });
    child.on("exit", (code) => {
      if (code !== null && code !== 0) {
        writeSyncError(vaultRoot, `push exited with code ${code}`);
      }
    });
  }).catch(() => {
    // Not a spoke vault — silent no-op
  });
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
      "python3",
      ["-m", "schist", "--vault", vaultRoot, "sync", "pull"],
      { cwd: vaultRoot, stdio: "ignore", env: process.env, detached: true }
    );
    // `detached: true` puts the child in its own process group. On timeout we
    // must signal the whole group (negative PID) — child.kill() only signals
    // python3, leaving git-fetch/git-rebase grandchildren alive with a live
    // .git/index.lock. SIGTERM first, then SIGKILL after a short grace in
    // case git ignores SIGTERM mid-rebase.
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
    cursor?: string;
  }
): Promise<SearchNotesResponse | ToolError> {
  const TOOL_NAME = "search_notes" as const;

  // Step 1: canonicalizeQueryHash. Active owner is per-call owner first
  // (matches sqlite-reader's scope=inherit resolution order), then env.
  const activeOwner =
    args.owner ?? process.env.SCHIST_AGENT_NAME ?? process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 2: Cursor decoding + queryHash binding check. Spec: "Cursor binding
  // to queryHash" — current call's computed queryHash MUST equal the cursor's
  // encoded queryHash. Mismatch → CURSOR_INVALID_SIGNATURE.
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_INVALID_SIGNATURE",
        message: "cursor was issued for a different query — restart pagination from page 1",
      };
    }
    offset = d.offset;
    consumingCursor = true;
  }

  // Step 3: Identical-query refusal (only when no cursor was presented).
  // verboseEnabled is always false here — search_notes has no verbose mode.
  if (!consumingCursor) {
    const refusal = checkRefusal({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1 to detect hasMore. Default 20, cap 100.
  // Negative / zero / missing all collapse to 20 (mirrors canonicalize's
  // limit:0 → missing rule so the queryHash on `limit: 0` matches omitted).
  const requested = args.limit;
  const effectiveLimit = (requested === undefined || requested === null || requested <= 0)
    ? 20
    : Math.min(requested, 100);

  let rows: import("./types.js").SearchResult[];
  try {
    rows = sqliteReader.searchNotes(vaultRoot, args.query, {
      limit: effectiveLimit + 1,
      status: args.status,
      tags: args.tags,
      scope: args.scope,
      owner: args.owner,
      offset,
    });
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }

  const hasMore = rows.length > effectiveLimit;
  const pageRows = hasMore ? rows.slice(0, effectiveLimit) : rows;

  // Step 5: Cursor issuance + recordIssued (only when this page was capped).
  let cursor: string | undefined;
  if (hasMore) {
    recordIssued({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
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

    return {
      id: args.id,
      title: (meta.title as string) ?? "",
      date: (meta.date as string) ?? "",
      status: (meta.status as string) ?? "draft",
      tags: Array.isArray(meta.tags) ? meta.tags : [],
      concepts: Array.isArray(meta.concepts) ? meta.concepts : [],
      domain: (meta.domain as string) ?? undefined,
      body,
      connections,
    };
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}

export async function create_note(
  vaultRoot: string,
  args: {
    title: string;
    body: string;
    tags?: string[];
    concepts?: string[];
    status?: string;
    connections?: Array<{ target: string; type: string; context?: string }>;
    directory?: string;
  },
  config: VaultConfig
): Promise<unknown> {
  try {
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

    if (rawSlug(args.title) === "") {
      return {
        error: "VALIDATION_ERROR",
        message: "Title must contain at least one alphanumeric character",
      } satisfies ToolError;
    }

    const slug = slugify(args.title);
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

    const metadata: Record<string, unknown> = {
      title: args.title,
      date,
      tags: args.tags ?? [],
      concepts: args.concepts ?? [],
      status: args.status ?? "draft",
      source_agent: "mcp",
    };

    const noteContent = buildNote(metadata, args.body, args.connections);
    const result = await writeNote(vaultRoot, relPath, noteContent);

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    return { id: relPath, path: relPath, commitSha: result.commitSha };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function add_connection(
  vaultRoot: string,
  args: { source: string; target: string; type: string; context?: string }
): Promise<unknown> {
  try {
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

    const result = await writeNote(vaultRoot, args.source, newContent);

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    return { source: args.source, target: args.target, type: args.type, commitSha: result.commitSha };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function assign_domain(
  vaultRoot: string,
  args: { id: string; domain: string }
): Promise<unknown> {
  try {
    const filePath = path.join(vaultRoot, args.id);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Note path is outside vault root" } satisfies ToolError;
    }

    // Validate domain exists in vault.yaml domains list
    const domains = sqliteReader.listDomains(vaultRoot);
    const validSlugs = new Set(domains.map((d: { slug: string }) => d.slug));
    // If no domains are defined, allow any domain (matches CLI behavior)
    if (validSlugs.size > 0 && !validSlugs.has(args.domain)) {
      return {
        error: "INVALID_DOMAIN",
        message: `Domain "${args.domain}" not found in vault.yaml. Valid domains: ${[...validSlugs].join(", ")}`,
      } satisfies ToolError;
    }

    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Note not found: ${args.id}` } satisfies ToolError;
    }

    // Use gray-matter to modify frontmatter
    const matter = (await import("./markdown-parser.js")).parseNote;
    const { metadata, body, connections } = matter(content);
    const { buildNote } = await import("./markdown-parser.js");

    // Update domain in metadata
    const newMetadata = { ...metadata, domain: args.domain };
    const newContent = buildNote(newMetadata, body, connections);

    const result = await writeNote(vaultRoot, args.id, newContent);

    triggerIngestion(vaultRoot);
    triggerSpokePush(vaultRoot);

    return { id: args.id, domain: args.domain, commitSha: result.commitSha };
  } catch (e: unknown) {
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number }
): Promise<unknown> {
  try {
    return sqliteReader.listConcepts(vaultRoot, args);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
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

  // Step 1: canonicalizeQueryHash. query_graph has no `owner` arg in the
  // tool schema; activeOwner comes from env only (same as search_memory).
  const activeOwner = process.env.SCHIST_AGENT_ID ?? "";
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
        error: "CURSOR_INVALID_SIGNATURE",
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
      verboseEnabled: false,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 4: SQL fetch with limit + 1 to detect hasMore. Default 100, cap 1000.
  // limit:0 / negative / missing collapse to the default (mirrors canonicalize).
  const requested = args.limit;
  const effectiveLimit = (requested === undefined || requested === null || requested <= 0)
    ? 100
    : Math.min(requested, 1000);

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

  // Step 5: Cursor issuance + recordIssued (only when this page was capped).
  let cursor: string | undefined;
  if (hasMore) {
    recordIssued({
      tool: TOOL_NAME,
      queryHash,
      owner: activeOwner,
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

export async function get_context(
  vaultRoot: string,
  // Default to "minimal" for agent session-start: only note/concept/edge counts
  // + last 3 modified. Agents that need richer context request standard/full.
  args: { depth?: "minimal" | "standard" | "full" }
): Promise<unknown> {
  await maybeSpokePull(vaultRoot);

  // Read (and clear) any pending background-sync-failure sentinel so agents
  // don't silently work against a stale local view. This runs independently
  // of the SQLite read — even if the DB query fails, we surface the warning
  // on the error result so the operator knows to check the hub connection.
  let syncWarning: string | undefined;
  const sentinelPath = path.join(vaultRoot, SYNC_ERROR_SENTINEL);
  try {
    const errText = (await fs.readFile(sentinelPath, "utf-8")).trim();
    if (errText) {
      syncWarning = `Recent background sync failure: ${errText}. Writes may not have reached the hub.`;
      await fs.unlink(sentinelPath).catch(() => {});
    }
  } catch {
    // No sentinel — healthy state
  }

  try {
    const context = sqliteReader.getContext(vaultRoot, args.depth ?? "minimal") as Record<string, unknown>;
    if (syncWarning) context.syncWarning = syncWarning;
    return context;
  } catch (e: unknown) {
    const err = normalizeError(e, "INGEST_ERROR");
    if (syncWarning) {
      return { ...err, syncWarning };
    }
    return err;
  }
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
 * defined above), then list_concepts, list_domains, get_context.
 *
 * Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md
 */
export async function search_memory(
  _vaultRoot: string,
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

  // Step 2: canonicalizeQueryHash. Active owner is SCHIST_AGENT_ID or "".
  const activeOwner = process.env.SCHIST_AGENT_ID ?? "";
  const ch = canonicalizeQueryHash(args as Record<string, unknown>, activeOwner);
  if (!ch.ok) return ch.error;
  const queryHash = ch.queryHash;

  // Step 3: Cursor decoding + queryHash binding check.
  // Binding policy: current call's computed queryHash MUST equal the cursor's
  // encoded queryHash. Mismatch → CURSOR_INVALID_SIGNATURE with explanatory
  // message. Locked in Task 3.0 spec amendment ("Cursor binding to queryHash").
  let offset = 0;
  let consumingCursor = false;
  if (typeof args.cursor === "string" && args.cursor.length > 0) {
    const d = decodeCursor(args.cursor, TOOL_NAME);
    if (!d.ok) return d.error;
    if (d.queryHash !== queryHash) {
      return {
        error: "CURSOR_INVALID_SIGNATURE",
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
      verboseEnabled,
    });
    if (refusal.refuse) return refusal.error;
  }

  // Step 5: SQL fetch with limit + 1 to detect hasMore. Server clamps limit
  // (max 200, 0 → default 50 to match the canonicalize collapse rule so the
  // queryHash on `limit: 0` equals the queryHash on omitted limit).
  const requested = args.limit;
  // Negative / zero / missing limit all collapse to the default 50; matches
  // canonicalize's collapse rule for `limit: 0` and gives a sensible default
  // for malformed inputs. Positive requests are clamped to the spec cap (200).
  const effectiveLimit = (requested === undefined || requested === null || requested <= 0)
    ? 50
    : Math.min(requested, 200);

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

export async function list_domains(
  vaultRoot: string,
  _args: Record<string, never>
): Promise<unknown> {
  try {
    return sqliteReader.listDomains(vaultRoot);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
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
    validateOwner(args.owner);
    return sqliteReader.addMemory(args);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function set_agent_state(
  _vaultRoot: string,
  args: { key: string; value: unknown; owner: string; ttl_hours?: number }
): Promise<unknown> {
  try {
    validateOwner(args.owner);
    return sqliteReader.setAgentState(args.key, args.value, args.owner, args.ttl_hours);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function delete_agent_state(
  _vaultRoot: string,
  args: { key: string; owner: string }
): Promise<unknown> {
  try {
    validateOwner(args.owner);
    return sqliteReader.deleteAgentState(args.key, args.owner);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function add_concept_alias(
  vaultRoot: string,
  args: { duplicate_slug: string; canonical_slug: string; reason?: string; created_by: string }
): Promise<unknown> {
  try {
    validateOwner(args.created_by);
    return sqliteReader.addConceptAlias(vaultRoot, args.duplicate_slug, args.canonical_slug, args.reason, args.created_by);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}
