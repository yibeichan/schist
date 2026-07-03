import Database from "better-sqlite3";
import * as path from "path";
import * as os from "os";
import * as fs from "fs";
import { spawn, spawnSync } from "child_process";
import { createRequire } from "module";
import { load as yamlLoad } from "js-yaml";
import type { SearchResult, Note, Concept, Connection, MemoryEntry, AgentStateEntry, ConceptAlias } from "./types.js";
import { CONNECTION_RE, parseConnections as parseConnectionsSync } from "./markdown-parser.js";
import { validateOwner } from "./agent-identity.js";

// ── Agent scope map (loaded from vault.yaml) ─────────────────────────────

/** Map of vaultRoot → (agent name → default scope), loaded once per vault from vault.yaml */
const agentScopeCache: Map<string, Map<string, string>> = new Map();

/**
 * Load participant default scopes from vault.yaml.
 * Supports both string[] and object[] participant formats.
 * Cached per vaultRoot after first load.
 */
export function loadAgentScopeMap(vaultRoot: string): Map<string, string> {
  const cached = agentScopeCache.get(vaultRoot);
  if (cached) return cached;

  const agentScopeMap = new Map<string, string>();
  agentScopeCache.set(vaultRoot, agentScopeMap);
  try {
    const vaultYamlPath = path.join(vaultRoot, "vault.yaml");
    if (!fs.existsSync(vaultYamlPath)) return agentScopeMap;

    const raw = yamlLoad(fs.readFileSync(vaultYamlPath, "utf-8")) as Record<string, unknown>;
    const participants = raw.participants;
    if (!Array.isArray(participants)) return agentScopeMap;

    for (const p of participants) {
      if (typeof p === "string") {
        agentScopeMap.set(p, "global");
      } else if (p && typeof p === "object" && "name" in p) {
        const obj = p as { name: string; default_scope?: string };
        agentScopeMap.set(obj.name, obj.default_scope ?? "global");
      }
    }
  } catch {
    // vault.yaml missing or malformed — use empty map
  }
  return agentScopeMap;
}

/** Reset cached scope map (for testing) */
export function resetAgentScopeMap(): void {
  agentScopeCache.clear();
}

/** Sanitize user input for FTS5 MATCH: quote each token to prevent query syntax injection. */
function sanitizeFtsQuery(raw: string): string {
  return raw
    .split(/\s+/)
    .filter(Boolean)
    .map(token => `"${token.replace(/"/g, '""')}"`)
    .join(" ");
}

// ── Schema-drift detection (#69) ─────────────────────────────────────────
//
// Pre-#69 vaults built their SQLite from an older schema.sql that lacked
// columns the current readers SELECT. The Python `get_db` (cli/schist/
// sqlite_query.py) only re-ingests on missing-file or missing-`docs`-table
// — it doesn't detect column drift. So on upgrade day, the first read
// against a pre-existing DB would throw `no such column` until something
// else triggered a rebuild (post-commit hook, spoke pull, manual ingest).
//
// Fix: before every `openDb`, verify the `docs` table has the columns this
// reader needs. If any are missing, synchronously spawn `schist-ingest` to
// rebuild from markdown, then continue. The check is cached per-vault per-
// process so it runs once per server start. Generalizes to all future
// schema additions: bump REQUIRED_DOCS_COLUMNS alongside schema.sql edits
// and existing deployments self-heal on next read.
const REQUIRED_DOCS_COLUMNS: ReadonlySet<string> = new Set([
  "id", "title", "date", "status", "tags", "concepts",
  "body", "scope", "source", "confidence", "file_ref",
]);
// Keep in sync with cli/schist/sqlite_query.py REQUIRED_TABLES. concept_aliases
// is included so vaults upgraded from a pre-concept_aliases schema (which still
// have docs + paper_metadata, so the drift check would otherwise pass) trigger
// an ingest rebuild before add_concept_alias hits "no such table". See #224.
const REQUIRED_TABLES: ReadonlySet<string> = new Set(["paper_metadata", "concept_aliases"]);

const verifiedVaults = new Set<string>();
const requireForWorker = createRequire(import.meta.url);
const betterSqlite3Path = requireForWorker.resolve("better-sqlite3");

const QUERY_GRAPH_DEFAULT_TIMEOUT_MS = 5_000;
const QUERY_GRAPH_DEFAULT_BYTE_BUDGET = 10 * 1024 * 1024;

function positiveIntEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

// Escape SQL LIKE wildcards so caller-supplied values match literally. The
// returned string is meant to be embedded in a LIKE pattern used with an
// `ESCAPE '\'` clause; backslash itself is escaped first. Without this, a `_`
// or `%` in a tag/scope/search term acts as a wildcard and produces
// false-positive matches (e.g. tag "machine_learning" matching
// "machine-learning"). See #225 / #229.
function escapeLike(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/%/g, "\\%").replace(/_/g, "\\_");
}

function maskSqlLiteralsAndComments(sql: string): string {
  const chars = [...sql];
  let i = 0;
  let state: "normal" | "single" | "double" | "backtick" | "bracket" | "line_comment" | "block_comment" = "normal";

  while (i < chars.length) {
    const ch = chars[i];
    const next = chars[i + 1] ?? "";

    if (state === "normal") {
      if (ch === "'") {
        state = "single";
      } else if (ch === "\"") {
        state = "double";
      } else if (ch === "`") {
        state = "backtick";
      } else if (ch === "[") {
        state = "bracket";
      } else if (ch === "-" && next === "-") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
        state = "line_comment";
      } else if (ch === "/" && next === "*") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
        state = "block_comment";
      }
    } else if (state === "single") {
      if (ch === "'" && next === "'") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
      } else if (ch === "'") {
        state = "normal";
      } else {
        chars[i] = " ";
      }
    } else if (state === "double") {
      if (ch === "\"" && next === "\"") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
      } else if (ch === "\"") {
        state = "normal";
      } else {
        chars[i] = " ";
      }
    } else if (state === "backtick") {
      // SQLite also accepts MySQL-style `...` identifier quoting; without this
      // state, a column legitimately named e.g. `delete` reaches the DML
      // keyword scan unmasked and gets falsely rejected as INVALID_SQL. See #253.
      if (ch === "`" && next === "`") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
      } else if (ch === "`") {
        state = "normal";
      } else {
        chars[i] = " ";
      }
    } else if (state === "bracket") {
      // Same for SQL-Server-style [...] identifier quoting (no escape form;
      // a `]` always closes the identifier).
      if (ch === "]") {
        state = "normal";
      } else {
        chars[i] = " ";
      }
    } else if (state === "line_comment") {
      if (ch === "\n") {
        state = "normal";
      } else {
        chars[i] = " ";
      }
    } else if (state === "block_comment") {
      if (ch === "*" && next === "/") {
        chars[i] = " ";
        chars[i + 1] = " ";
        i += 1;
        state = "normal";
      } else {
        chars[i] = " ";
      }
    }

    i += 1;
  }

  return chars.join("");
}

/** Test-only — clears the per-process verified-vaults cache so drift detection re-fires. */
export function resetSchemaCacheForTesting(): void {
  verifiedVaults.clear();
}

function ensureSchemaCurrent(vaultRoot: string): void {
  if (verifiedVaults.has(vaultRoot)) return;

  // Skip drift detection on inline-DB test fixtures (no schist.yaml present).
  // Production vaults always carry a schist.yaml config — its presence is
  // what distinguishes "real vault that should auto-heal" from "test
  // fixture with a hand-crafted DB that should be left alone".
  if (!fs.existsSync(path.join(vaultRoot, "schist.yaml"))) {
    verifiedVaults.add(vaultRoot);
    return;
  }

  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  // Narrowed scope: only flag drift when the `docs` table EXISTS but lacks
  // required columns. Missing-table / missing-file cases are already
  // handled upstream by the Python `get_db` (cli/schist/sqlite_query.py),
  // and triggering an ingest here on a test fixture that hand-crafts a
  // minimal DB without docs would wipe its seed data. The upgrade hazard
  // this guards against is "docs table exists from an older schema and
  // lacks a column the current reader SELECTs" — that's the only case we
  // need to repair from the TS side.
  const checkMissing = (): string[] => {
    try {
      const db = new Database(dbPath, { readonly: true });
      try {
        const tableRows = db.prepare(
          "SELECT name FROM sqlite_master WHERE type = 'table'",
        ).all() as Array<{ name: string }>;
        const tables = new Set(tableRows.map((r) => r.name));
        const missingTables = [...REQUIRED_TABLES]
          .filter((t) => !tables.has(t))
          .map((t) => `table:${t}`);
        const cols = db.pragma("table_info(docs)") as Array<{ name: string }>;
        if (cols.length === 0) return missingTables; // no docs table — only report side-table drift
        const present = new Set(cols.map((c) => c.name));
        return [
          ...missingTables,
          ...[...REQUIRED_DOCS_COLUMNS].filter((c) => !present.has(c)),
        ];
      } finally {
        db.close();
      }
    } catch {
      return []; // DB file missing or corrupt — out of scope
    }
  };

  if (checkMissing().length === 0) {
    verifiedVaults.add(vaultRoot);
    return;
  }

  runIngestSync(vaultRoot);

  // Verify ingest actually fixed the drift. Catches the "installed
  // schist-ingest is older than this MCP server" case — the rebuild
  // succeeds but produces the same out-of-date schema. Without this
  // recheck the next query would throw `no such column` with no hint
  // that the underlying problem is a version mismatch.
  const stillMissing = checkMissing();
  if (stillMissing.length > 0) {
    throw new Error(
      `Schema drift persists after schist-ingest rebuild — docs table missing columns: ${stillMissing.join(", ")}. ` +
      `The installed schist-ingest is likely older than this MCP server. ` +
      `Upgrade it: \`uv tool install --reinstall --force <path-to-schist/cli>\`.`,
    );
  }
  verifiedVaults.add(vaultRoot);
}

function runIngestSync(vaultRoot: string): void {
  // Mirrors SCHIST_INGEST_BIN handling in tools.ts:schistCliBin. Duplicated
  // here (1 line) rather than imported to avoid a tools→reader→tools cycle.
  const ingestBin = process.env.SCHIST_INGEST_BIN?.trim() || "schist-ingest";
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  // A full vault rebuild can be slow on large vaults / slow disks, so the
  // ceiling is generous — but it MUST exist. Without it, a stalled
  // schist-ingest (I/O contention, filesystem lock) blocks the MCP server
  // forever on the first read after schema drift, since openDb → every read
  // tool routes through here (#247). Env-overridable.
  const timeoutMs = Number(process.env.SCHIST_INGEST_TIMEOUT_MS) || 120000;
  const res = spawnSync(ingestBin, ["--vault", vaultRoot, "--db", dbPath], {
    cwd: vaultRoot,
    stdio: ["ignore", "ignore", "pipe"],
    encoding: "utf-8",
    timeout: timeoutMs,
  });
  // spawnSync sets res.error to an ETIMEDOUT Error and kills the child on
  // timeout; surface that as an actionable message rather than a raw spawn err.
  if (res.error && (res.error as NodeJS.ErrnoException).code === "ETIMEDOUT") {
    throw new Error(
      `schist-ingest timed out after ${timeoutMs}ms during schema-drift rebuild — ` +
      `the vault may be very large or the process stalled. ` +
      `Raise SCHIST_INGEST_TIMEOUT_MS if the vault legitimately needs longer.`,
    );
  }
  if (res.error || res.status !== 0) {
    const stderr = (res.stderr ?? "").toString().trim();
    throw new Error(
      `schist-ingest failed during schema-drift rebuild: ${
        res.error?.message ?? stderr ?? `exit ${res.status}`
      }`,
    );
  }
}

function openDb(vaultRoot: string, opts?: { readonly?: boolean }): Database.Database {
  ensureSchemaCurrent(vaultRoot);
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  const readonly = opts?.readonly ?? true;
  const db = new Database(dbPath, { readonly });
  if (!readonly) {
    db.pragma("foreign_keys = ON");
  }
  return db;
}

export function searchNotes(
  vaultRoot: string,
  query: string,
  opts?: { limit?: number; status?: string; tags?: string[]; scope?: string; owner?: string; offset?: number; confidence?: "low" | "medium" | "high" }
): SearchResult[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 20;
    const offset = opts?.offset ?? 0;
    let sql = `
      SELECT docs.id, docs.title, docs.date, docs.status, docs.tags, docs.scope, docs.confidence,
             snippet(docs_fts, 1, '<b>', '</b>', '...', 20) as snippet
      FROM docs_fts
      JOIN docs ON docs.rowid = docs_fts.rowid
      WHERE docs_fts MATCH ?
    `;
    const params: unknown[] = [sanitizeFtsQuery(query)];

    if (opts?.status) {
      sql += ` AND docs.status = ?`;
      params.push(opts.status);
    }

    if (opts?.tags && opts.tags.length > 0) {
      for (const tag of opts.tags) {
        sql += ` AND docs.tags LIKE ? ESCAPE '\\'`;
        params.push(`%"${escapeLike(tag)}"%`);
      }
    }

    if (opts?.confidence) {
      sql += ` AND docs.confidence = ?`;
      params.push(opts.confidence);
    }

    // ORDER BY is assembled in three layers so OFFSET pagination is stable:
    //   1. scope=inherit prepends a `CASE WHEN scope=callingScope THEN 0 ELSE 1`
    //      so the agent's own scope outranks 'global'.
    //   2. bm25(docs_fts) provides FTS relevance ordering.
    //   3. docs.id ASC is the deterministic tiebreaker (required for stable
    //      LIMIT/OFFSET — see docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md).
    const orderClauses: string[] = [];

    if (opts?.scope) {
      if (opts.scope === "inherit") {
        // Resolve calling scope: per-call owner > SCHIST_AGENT_NAME > SCHIST_AGENT_ID > "".
        // Per-call owner is the only signal under SCHIST_ALLOWED_AGENTS-only
        // deployments (shared MCP server, no per-process env identity); without
        // it, scope-inherit silently collapses to "global" for every agent.
        const scopeMap = loadAgentScopeMap(vaultRoot);
        const agentName =
          opts.owner ?? process.env.SCHIST_AGENT_NAME ?? process.env.SCHIST_AGENT_ID;
        const callingScope = scopeMap.get(agentName ?? "") ?? "global";
        // The LIKE branch matches sub-scopes (`callingScope/...`); escape the
        // value so a `_`/`%` in the scope name can't act as a wildcard and pull
        // in a sibling scope (e.g. "my_project" matching "my-project"). The two
        // `= ?` comparisons are exact equality and need no escaping. See #229.
        sql += ` AND (docs.scope = 'global' OR docs.scope = ? OR docs.scope LIKE ? || '/%' ESCAPE '\\')`;
        params.push(callingScope, escapeLike(callingScope));
        orderClauses.push(`CASE WHEN docs.scope = ? THEN 0 ELSE 1 END`);
        params.push(callingScope);
      } else {
        sql += ` AND docs.scope = ?`;
        params.push(opts.scope);
      }
    }

    orderClauses.push("bm25(docs_fts)");
    orderClauses.push("docs.id ASC");
    sql += ` ORDER BY ${orderClauses.join(", ")}`;

    sql += ` LIMIT ? OFFSET ?`;
    params.push(limit, offset);

    const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
    return rows.map((row) => {
      const conf = row.confidence;
      return {
        id: row.id as string,
        title: row.title as string,
        date: (row.date as string) ?? "",
        status: (row.status as string | null) ?? null,
        tags: row.tags ? JSON.parse(row.tags as string) : [],
        snippet: (row.snippet as string) ?? "",
        scope: (row.scope as string) ?? undefined,
        confidence: (conf === "low" || conf === "medium" || conf === "high") ? conf : undefined,
      };
    });
  } finally {
    db.close();
  }
}

export function getNote(vaultRoot: string, id: string): Note | null {
  const db = openDb(vaultRoot);
  try {
    const row = db.prepare("SELECT * FROM docs WHERE id = ?").get(id) as Record<string, unknown> | undefined;
    if (!row) return null;

    const body = (row.body as string) ?? "";
    const connections = parseConnectionsSync(body);

    const conf = row.confidence;
    return {
      id: row.id as string,
      title: row.title as string,
      date: (row.date as string) ?? "",
      status: (row.status as string | null) ?? null,
      tags: row.tags ? JSON.parse(row.tags as string) : [],
      concepts: row.concepts ? JSON.parse(row.concepts as string) : [],
      body,
      connections,
      scope: (row.scope as string) ?? undefined,
      source: (row.source === "human" || row.source === "agent") ? row.source as "human" | "agent" : undefined,
      confidence: (conf === "low" || conf === "medium" || conf === "high") ? conf : undefined,
      file_ref: typeof row.file_ref === "string" && row.file_ref ? row.file_ref : undefined,
    };
  } finally {
    db.close();
  }
}

/**
 * Return notes that link TO any of `targets` (inbound edges), excluding the
 * note itself (`selfId`). Used by delete_note (#119) to refuse-or-cascade when
 * removing a note would leave dangling `## Connections` references elsewhere.
 *
 * `targets` is usually a single vault-relative path, but for a concept note it
 * also includes the bare slug, because edges to concepts are stored either as
 * `concepts/<slug>.md` or `<slug>` (see conceptEdgeJoinCondition below). A
 * single-token query would miss the bare-slug form.
 *
 * Reads the SQLite graph index, which is rebuilt from markdown on every commit
 * (ingest.py drops + repopulates `edges`). It can lag a just-written note until
 * the post-commit ingest finishes; delete_note re-reads each source file from
 * disk before stripping, so a stale row never causes a bad edit — at worst a
 * missed one.
 */
export function inboundEdges(
  vaultRoot: string,
  targets: string[],
  selfId: string
): Array<{ source: string; type: string }> {
  if (targets.length === 0) return [];
  const db = openDb(vaultRoot);
  try {
    const placeholders = targets.map(() => "?").join(", ");
    return db
      .prepare(
        `SELECT DISTINCT source, type FROM edges WHERE target IN (${placeholders}) AND source != ? ORDER BY source`
      )
      .all(...targets, selfId) as Array<{ source: string; type: string }>;
  } finally {
    db.close();
  }
}

function conceptEdgeJoinCondition(edgeAlias: string, conceptAlias: string): string {
  return `(
    ${edgeAlias}.source = ${conceptAlias}.slug OR
    ${edgeAlias}.target = ${conceptAlias}.slug OR
    ${edgeAlias}.source = 'concepts/' || ${conceptAlias}.slug || '.md' OR
    ${edgeAlias}.target = 'concepts/' || ${conceptAlias}.slug || '.md'
  )`;
}

export function listConcepts(
  vaultRoot: string,
  opts?: { tags?: string[]; search?: string; limit?: number; offset?: number }
): Concept[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 50;
    const offset = opts?.offset ?? 0;
    let sql = `
      SELECT c.slug, c.title, c.description, c.tags,
             COUNT(e.id) as edgeCount
      FROM concepts c
      LEFT JOIN edges e ON ${conceptEdgeJoinCondition("e", "c")}
    `;
    const params: unknown[] = [];
    const where: string[] = [];

    if (opts?.search) {
      where.push(`(c.title LIKE ? ESCAPE '\\' OR c.description LIKE ? ESCAPE '\\')`);
      const escaped = `%${escapeLike(opts.search)}%`;
      params.push(escaped, escaped);
    }

    if (opts?.tags && opts.tags.length > 0) {
      for (const tag of opts.tags) {
        where.push(`c.tags LIKE ? ESCAPE '\\'`);
        params.push(`%"${escapeLike(tag)}"%`);
      }
    }

    if (where.length > 0) sql += ` WHERE ${where.join(" AND ")}`;
    // c.slug ASC tiebreaker is required for stable LIMIT/OFFSET pagination.
    sql += ` GROUP BY c.slug ORDER BY edgeCount DESC, c.slug ASC LIMIT ? OFFSET ?`;
    params.push(limit, offset);

    const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
    return rows.map((row) => ({
      slug: row.slug as string,
      title: row.title as string,
      description: (row.description as string) ?? "",
      tags: row.tags ? JSON.parse(row.tags as string) : [],
      edgeCount: (row.edgeCount as number) ?? 0,
    }));
  } finally {
    db.close();
  }
}

type QueryGraphResult = { columns: string[]; rows: unknown[][]; rowCount: number };

function runQueryGraphChild(
  dbPath: string,
  sql: string,
  params: unknown[],
  opts: { timeoutMs: number; byteBudget: number }
): Promise<QueryGraphResult> {
  const childSource = `
    const Database = require(${JSON.stringify(betterSqlite3Path)});

    function jsonBytes(value) {
      return Buffer.byteLength(JSON.stringify(value), "utf8");
    }

    function respond(payload) {
      process.stdout.write(JSON.stringify(payload));
    }

    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { input += chunk; });
    process.stdin.on("end", () => {
      try {
        const request = JSON.parse(input);
        const db = new Database(request.dbPath, { readonly: true });
        try {
          const stmt = db.prepare(request.sql);
          const columns = stmt.columns().map((c) => c.name);
          const rows = [];
          let totalBytes = jsonBytes({ columns, rows: [], rowCount: 0 });

          for (const row of stmt.iterate(...request.params)) {
            const rowValues = columns.map((c) => row[c]);
            totalBytes += jsonBytes(rowValues) + 1;
            if (totalBytes > request.byteBudget) {
              const err = new Error(
                "query_graph response exceeds byte budget (" +
                totalBytes + " > " + request.byteBudget + " bytes)"
              );
              err.error = "QUERY_RESPONSE_TOO_LARGE";
              throw err;
            }
            rows.push(rowValues);
          }

          respond({
            ok: true,
            result: { columns, rows, rowCount: rows.length },
          });
        } finally {
          db.close();
        }
      } catch (e) {
        respond({
          ok: false,
          error: {
            error: e && (e.error || e.code) || "INVALID_SQL",
            message: e && e.message || String(e),
            stack: e && e.stack,
          },
        });
      }
    });
  `;

  return new Promise<QueryGraphResult>((resolve, reject) => {
    const child = spawn(process.execPath, ["-e", childSource], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let settled = false;
    let stdout = "";
    let stderr = "";
    const finish = (fn: () => void): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn();
    };
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 500).unref?.();
      finish(() => {
        reject(Object.assign(
          new Error(`query_graph exceeded timeout (${opts.timeoutMs}ms)`),
          { error: "QUERY_TIMEOUT" },
        ));
      });
    }, opts.timeoutMs);
    timer.unref?.();

    child.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8"); });
    child.stderr.on("data", (chunk: Buffer) => { stderr += chunk.toString("utf8"); });
    child.on("error", (err) => finish(() => reject(err)));
    child.on("close", (code, signal) => {
      finish(() => {
        if (code !== 0) {
          reject(Object.assign(
            new Error(`query_graph child exited with code ${code ?? "null"}${signal ? ` signal ${signal}` : ""}: ${stderr.trim()}`),
            { error: "QUERY_GRAPH_EXECUTOR_ERROR" },
          ));
          return;
        }
        let message: {
          ok: boolean;
          result?: QueryGraphResult;
          error?: { error?: string; message?: string; stack?: string };
        };
        try {
          message = JSON.parse(stdout);
        } catch {
          reject(Object.assign(
            new Error(`query_graph child returned invalid JSON: ${stdout.slice(0, 200)}`),
            { error: "QUERY_GRAPH_EXECUTOR_ERROR" },
          ));
          return;
        }
        if (message.ok && message.result) {
          resolve(message.result);
          return;
        }
        const err = Object.assign(
          new Error(message.error?.message ?? "query_graph failed"),
          { error: message.error?.error ?? "INVALID_SQL" },
        );
        if (message.error?.stack) err.stack = message.error.stack;
        reject(err);
      });
    });
    child.stdin.end(JSON.stringify({ dbPath, sql, params, byteBudget: opts.byteBudget }));
  });
}

export async function queryGraph(
  vaultRoot: string,
  sql: string,
  params?: unknown[],
  opts?: { limit?: number; offset?: number }
): Promise<QueryGraphResult> {
  // Strip trailing semicolon(s) + whitespace before either guard or wrap.
  // The subquery wrap below produces invalid SQL if the caller_sql ends in a
  // semicolon (`SELECT * FROM (SELECT 1;) ...`); accept the trailing `;` as a
  // common ergonomic affordance and remove it. Multi-statement input is still
  // rejected by `better-sqlite3.prepare()`.
  const trimmed = sql.trim().replace(/;+\s*$/, "");
  // trimStart() after masking: a leading SQL comment (e.g. an LLM's reasoning
  // `-- ...` line) is blanked to spaces by the masker, which would otherwise
  // push the SELECT/WITH keyword off the `^` anchor and get the query wrongly
  // rejected as non-SELECT. The blocked-keyword scan below is unanchored, so
  // trimming doesn't affect it. See #222.
  const guardSql = maskSqlLiteralsAndComments(trimmed).trimStart();
  if (
    !guardSql.match(/^(SELECT|WITH)\b/i) ||
    guardSql.match(/\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b/i)
  ) {
    // Return a ToolError result rather than throwing a plain object.
    // Throwing non-Error objects loses stack traces and breaks instanceof checks
    // in callers. The tools.ts normalizeError handler then serialises this cleanly.
    throw Object.assign(
      new Error("Only SELECT and WITH queries are allowed"),
      { error: "INVALID_SQL" }
    );
  }

  // Subquery-wrap pagination (spec: "query_graph cursor wrapping").
  // Server-controlled outer LIMIT/OFFSET ride on the caller's SQL. The
  // caller's inner LIMIT/ORDER BY/OFFSET stay verbatim — a caller passing
  // `SELECT * FROM docs LIMIT 5` gets exactly 5 rows; a caller passing
  // `ORDER BY date DESC` gets server-paginated results in date order.
  // No regex on caller_sql for LIMIT detection — the subquery wrap is a
  // single deterministic rewrite with no comment/CTE/UNION edge cases.
  //
  // Cap is best-effort, not a security boundary. A caller can construct
  // pathological SQL (e.g. `SELECT * FROM docs) AS x --`) that closes the
  // wrap's subquery early and comments out the trailing `LIMIT ? OFFSET ?`.
  // In every such case better-sqlite3 surfaces a clean parse/bind error
  // (the wrap suffix's `?` placeholders no longer have anywhere to bind), so
  // the worst outcome is INVALID_SQL — never silently-unbounded rows. The
  // vault DB is already fully readable via SELECT; the wrap exists to bound
  // typical-call context burn, not to defend against adversarial callers.
  // Regression tests live in `tests/query-graph-tool.test.ts` under
  // "subquery wrap is structurally safe against comment-escape attempts".
  const limit = opts?.limit ?? 100;
  const offset = opts?.offset ?? 0;
  const wrappedSql = `SELECT * FROM (${trimmed}) AS user_query LIMIT ? OFFSET ?`;
  const allParams = [...(params ?? []), limit, offset];
  const timeoutMs = positiveIntEnv("SCHIST_QUERY_GRAPH_TIMEOUT_MS", QUERY_GRAPH_DEFAULT_TIMEOUT_MS);
  const byteBudget = positiveIntEnv("SCHIST_QUERY_GRAPH_BYTE_BUDGET", QUERY_GRAPH_DEFAULT_BYTE_BUDGET);

  ensureSchemaCurrent(vaultRoot);
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  return runQueryGraphChild(dbPath, wrappedSql, allParams, { timeoutMs, byteBudget });
}

export function getContext(
  vaultRoot: string,
  depth: "minimal" | "standard" | "full" = "standard"
): object {
  const db = openDb(vaultRoot);
  try {
    const noteCount = (db.prepare("SELECT COUNT(*) as c FROM docs").get() as Record<string, number>).c;
    const conceptCount = (db.prepare("SELECT COUNT(*) as c FROM concepts").get() as Record<string, number>).c;
    const edgeCount = (db.prepare("SELECT COUNT(*) as c FROM edges").get() as Record<string, number>).c;

    if (depth === "minimal") {
      return { noteCount, conceptCount, edgeCount };
    }

    const recent = db
      .prepare("SELECT id, title, date, status FROM docs ORDER BY date DESC, id ASC LIMIT 10")
      .all() as Record<string, unknown>[];

    const hotConcepts = db
      .prepare(`
        SELECT c.slug, c.title, COUNT(e.id) as edgeCount
        FROM concepts c
        LEFT JOIN edges e ON ${conceptEdgeJoinCondition("e", "c")}
        GROUP BY c.slug
        ORDER BY edgeCount DESC
        LIMIT 10
      `)
      .all() as Record<string, unknown>[];

    const result: Record<string, unknown> = {
      vault: { path: vaultRoot, noteCount, conceptCount, edgeCount },
      recent,
      hotConcepts,
    };

    if (depth === "full") {
      const allTags = db
        .prepare("SELECT tags FROM docs WHERE tags IS NOT NULL")
        .all() as Record<string, string>[];
      const tagCounts: Record<string, number> = {};
      for (const row of allTags) {
        const tags: string[] = JSON.parse(row.tags ?? "[]");
        for (const tag of tags) {
          tagCounts[tag] = (tagCounts[tag] ?? 0) + 1;
        }
      }
      result.tagCloud = Object.entries(tagCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 30)
        .map(([tag, count]) => ({ tag, count }));
    }

    return result;
  } finally {
    db.close();
  }
}

// ── Memory V2 — agent-state.db (separate from schist.db) ──────────────────

const MEMORY_SCHEMA = `
CREATE TABLE IF NOT EXISTS agent_memory (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  owner        TEXT NOT NULL,
  date         TEXT NOT NULL,
  entry_type   TEXT NOT NULL CHECK(entry_type IN ('decision','lesson','blocker','completion','observation')),
  content      TEXT NOT NULL,
  tags         TEXT,
  related_doc  TEXT,
  source_ref   TEXT,
  confidence   TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('low','medium','high')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(owner, entry_type, content, tags, content='agent_memory', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN
  INSERT INTO agent_memory_fts(rowid, owner, entry_type, content, tags)
  VALUES (new.id, new.owner, new.entry_type, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN
  INSERT INTO agent_memory_fts(agent_memory_fts, rowid, owner, entry_type, content, tags)
  VALUES ('delete', old.id, old.owner, old.entry_type, old.content, old.tags);
END;
CREATE TABLE IF NOT EXISTS agent_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  owner      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  ttl_hours  INTEGER DEFAULT NULL
);
`;

function openMemoryDb(): Database.Database {
  const dbPath = process.env.SCHIST_MEMORY_DB ??
    path.join(os.homedir(), ".openclaw", "memory", "agent-state.db");
  const dir = path.dirname(dbPath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const db = new Database(dbPath);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  db.exec(MEMORY_SCHEMA);
  return db;
}

function getTeamStateOwner(): string | null {
  const value = process.env.SCHIST_TEAM_OWNER?.trim();
  return value || null;
}

/** Validate agent_state key namespace. */
function assertKeyPrefix(key: string, owner: string): void {
  const keyPrefix = key.split(".")[0];
  if (keyPrefix !== owner && keyPrefix !== "team") {
    throw Object.assign(new Error(`agent_state: key '${key}' prefix must match owner '${owner}'`), { error: "VALIDATION_ERROR" });
  }
  if (keyPrefix === "team") {
    const teamOwner = getTeamStateOwner();
    if (!teamOwner) {
      throw Object.assign(new Error("agent_state: team.* keys require SCHIST_TEAM_OWNER to be configured"), { error: "VALIDATION_ERROR" });
    }
    if (owner !== teamOwner) {
      throw Object.assign(new Error("agent_state: team.* keys require owner to match SCHIST_TEAM_OWNER"), { error: "VALIDATION_ERROR" });
    }
  }
}

export function addMemory(entry: {
  owner: string;
  date?: string;
  entry_type: string;
  content: string;
  tags?: string[];
  related_doc?: string;
  source_ref?: string;
  confidence?: string;
}): { id: number; created_at: string } {
  validateOwner(entry.owner);
  const db = openMemoryDb();
  try {
    const date = entry.date ?? new Date().toISOString().split("T")[0];
    const tags = entry.tags ? JSON.stringify(entry.tags) : null;
    const confidence = entry.confidence ?? "medium";
    const stmt = db.prepare(`
      INSERT INTO agent_memory (owner, date, entry_type, content, tags, related_doc, source_ref, confidence)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `);
    const result = stmt.run(entry.owner, date, entry.entry_type, entry.content, tags,
      entry.related_doc ?? null, entry.source_ref ?? null, confidence);
    const row = db.prepare("SELECT created_at FROM agent_memory WHERE id = ?").get(result.lastInsertRowid) as { created_at: string };
    return { id: result.lastInsertRowid as number, created_at: row.created_at };
  } finally {
    db.close();
  }
}

export function searchMemory(opts: {
  query?: string;
  owner?: string;
  entry_type?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}): MemoryEntry[] {
  const db = openMemoryDb();
  try {
    // Expire TTL-based agent_state rows while we have the DB open
    db.exec(`DELETE FROM agent_state WHERE ttl_hours IS NOT NULL AND
      datetime(updated_at, '+' || ttl_hours || ' hours') < datetime('now')`);

    const limit = opts.limit ?? 50;
    const offset = opts.offset ?? 0;
    const params: unknown[] = [];

    let useFts = false;
    if (opts.query) {
      useFts = true;
    }

    if (useFts) {
      let sql = `
        SELECT m.id, m.owner, m.date, m.entry_type, m.content, m.tags,
               m.related_doc, m.source_ref, m.confidence, m.created_at
        FROM agent_memory_fts f
        JOIN agent_memory m ON m.id = f.rowid
        WHERE agent_memory_fts MATCH ?
      `;
      params.push(sanitizeFtsQuery(opts.query!));
      if (opts.owner) { sql += " AND m.owner = ?"; params.push(opts.owner); }
      if (opts.entry_type) { sql += " AND m.entry_type = ?"; params.push(opts.entry_type); }
      if (opts.date_from) { sql += " AND m.date >= ?"; params.push(opts.date_from); }
      if (opts.date_to) { sql += " AND m.date <= ?"; params.push(opts.date_to); }
      // id ASC tiebreaker — required for OFFSET pagination stability when bm25 ties (see docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md).
      sql += " ORDER BY bm25(agent_memory_fts), m.id ASC LIMIT ? OFFSET ?";
      params.push(limit, offset);
      const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
      return rows.map(rowToMemoryEntry);
    } else {
      let sql = "SELECT * FROM agent_memory WHERE 1=1";
      if (opts.owner) { sql += " AND owner = ?"; params.push(opts.owner); }
      if (opts.entry_type) { sql += " AND entry_type = ?"; params.push(opts.entry_type); }
      if (opts.date_from) { sql += " AND date >= ?"; params.push(opts.date_from); }
      if (opts.date_to) { sql += " AND date <= ?"; params.push(opts.date_to); }
      // id ASC tiebreaker — required for OFFSET pagination stability when created_at ties (see docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md).
      sql += " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?";
      params.push(limit, offset);
      const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
      return rows.map(rowToMemoryEntry);
    }
  } finally {
    db.close();
  }
}

function rowToMemoryEntry(row: Record<string, unknown>): MemoryEntry {
  return {
    id: row.id as number,
    owner: row.owner as string,
    date: row.date as string,
    entry_type: row.entry_type as MemoryEntry["entry_type"],
    content: row.content as string,
    tags: row.tags ? JSON.parse(row.tags as string) : [],
    related_doc: row.related_doc == null ? undefined : (row.related_doc as string),
    source_ref: row.source_ref == null ? undefined : (row.source_ref as string),
    confidence: row.confidence as MemoryEntry["confidence"],
    created_at: row.created_at as string,
  };
}

export function getAgentState(key: string): AgentStateEntry | null {
  const db = openMemoryDb();
  try {
    // Expire stale TTL rows
    db.exec(`DELETE FROM agent_state WHERE ttl_hours IS NOT NULL AND
      datetime(updated_at, '+' || ttl_hours || ' hours') < datetime('now')`);
    const row = db.prepare("SELECT * FROM agent_state WHERE key = ?").get(key) as Record<string, unknown> | undefined;
    if (!row) return null;
    return {
      key: row.key as string,
      value: JSON.parse(row.value as string),
      owner: row.owner as string,
      updated_at: row.updated_at as string,
      ttl_hours: row.ttl_hours as number | null,
    };
  } finally {
    db.close();
  }
}

export function setAgentState(key: string, value: unknown, owner: string, ttl_hours?: number): { key: string; updated_at: string } {
  validateOwner(owner);
  assertKeyPrefix(key, owner);
  const db = openMemoryDb();
  try {
    const row = db.prepare(`
      INSERT INTO agent_state (key, value, owner, ttl_hours)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(key) DO UPDATE SET value=excluded.value,
        ttl_hours=excluded.ttl_hours, updated_at=datetime('now')
      WHERE agent_state.owner = excluded.owner
      RETURNING updated_at
    `).get(key, JSON.stringify(value), owner, ttl_hours ?? null) as { updated_at: string } | undefined;
    if (!row) {
      const existing = db.prepare("SELECT owner FROM agent_state WHERE key = ?").get(key) as { owner: string } | undefined;
      if (existing && existing.owner !== owner) {
        throw Object.assign(new Error("Cannot overwrite state key owned by another agent"), { error: "OWNERSHIP_ERROR" });
      }
      throw new Error("Failed to set agent state");
    }
    return { key, updated_at: row.updated_at };
  } finally {
    db.close();
  }
}

export function deleteAgentState(key: string, owner: string): { deleted: boolean } {
  validateOwner(owner);
  assertKeyPrefix(key, owner);
  const db = openMemoryDb();
  try {
    const result = db.prepare("DELETE FROM agent_state WHERE key = ? AND owner = ?").run(key, owner);
    return { deleted: result.changes > 0 };
  } finally {
    db.close();
  }
}

// ── Concept-alias tools (use schist.db) ───────────────────────────────────

export function addConceptAlias(
  vaultRoot: string,
  duplicate_slug: string,
  canonical_slug: string,
  reason: string | undefined,
  created_by: string
): ConceptAlias {
  validateOwner(created_by);
  const db = openDb(vaultRoot, { readonly: false });
  try {
    db.prepare(`
      INSERT OR REPLACE INTO concept_aliases (duplicate_slug, canonical_slug, reason, created_by)
      VALUES (?, ?, ?, ?)
    `).run(duplicate_slug, canonical_slug, reason ?? null, created_by);
    const row = db.prepare("SELECT * FROM concept_aliases WHERE duplicate_slug = ? AND canonical_slug = ?")
      .get(duplicate_slug, canonical_slug) as Record<string, unknown>;
    return {
      duplicate_slug: row.duplicate_slug as string,
      canonical_slug: row.canonical_slug as string,
      reason: row.reason as string | undefined,
      created_by: row.created_by as string,
      created_at: row.created_at as string,
    };
  } finally {
    db.close();
  }
}
