import Database from "better-sqlite3";
import * as path from "path";
import * as os from "os";
import * as fs from "fs";
import type { SearchResult, Note, Concept, Connection, MemoryEntry, AgentStateEntry, Domain, ConceptAlias } from "./types.js";
import { CONNECTION_RE, parseConnections as parseConnectionsSync } from "./markdown-parser.js";

function openDb(vaultRoot: string, opts?: { readonly?: boolean }): Database.Database {
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  return new Database(dbPath, { readonly: opts?.readonly ?? true });
}

/** Sanitize user input for FTS5 MATCH: quote each token to prevent query syntax injection. */
function sanitizeFtsQuery(raw: string): string {
  return raw
    .split(/\s+/)
    .filter(Boolean)
    .map(token => `"${token.replace(/"/g, '""')}"`)
    .join(" ");
}

export function searchNotes(
  vaultRoot: string,
  query: string,
  opts?: { limit?: number; status?: string; tags?: string[] }
): SearchResult[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 20;
    let sql = `
      SELECT docs.id, docs.title, docs.date, docs.status, docs.tags,
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
        sql += ` AND docs.tags LIKE ?`;
        params.push(`%"${tag}"%`);
      }
    }

    sql += ` LIMIT ?`;
    params.push(limit);

    const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
    return rows.map((row) => ({
      id: row.id as string,
      title: row.title as string,
      date: (row.date as string) ?? "",
      status: (row.status as string) ?? "draft",
      tags: row.tags ? JSON.parse(row.tags as string) : [],
      snippet: (row.snippet as string) ?? "",
    }));
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

    return {
      id: row.id as string,
      title: row.title as string,
      date: (row.date as string) ?? "",
      status: (row.status as string) ?? "draft",
      tags: row.tags ? JSON.parse(row.tags as string) : [],
      concepts: row.concepts ? JSON.parse(row.concepts as string) : [],
      body,
      connections,
    };
  } finally {
    db.close();
  }
}

export function listConcepts(
  vaultRoot: string,
  opts?: { tags?: string[]; search?: string; limit?: number }
): Concept[] {
  const db = openDb(vaultRoot);
  try {
    const limit = opts?.limit ?? 50;
    let sql = `
      SELECT c.slug, c.title, c.description, c.tags,
             COUNT(e.id) as edgeCount
      FROM concepts c
      LEFT JOIN edges e ON e.source = c.slug OR e.target = c.slug
    `;
    const params: unknown[] = [];
    const where: string[] = [];

    if (opts?.search) {
      where.push(`(c.title LIKE ? OR c.description LIKE ?)`);
      params.push(`%${opts.search}%`, `%${opts.search}%`);
    }

    if (opts?.tags && opts.tags.length > 0) {
      for (const tag of opts.tags) {
        where.push(`c.tags LIKE ?`);
        params.push(`%"${tag}"%`);
      }
    }

    if (where.length > 0) sql += ` WHERE ${where.join(" AND ")}`;
    sql += ` GROUP BY c.slug ORDER BY edgeCount DESC LIMIT ?`;
    params.push(limit);

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

export function queryGraph(
  vaultRoot: string,
  sql: string,
  params?: unknown[]
): { columns: string[]; rows: unknown[][]; rowCount: number } {
  const trimmed = sql.trim();
  if (
    !trimmed.match(/^(SELECT|WITH)\b/i) ||
    trimmed.match(/\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE)\b/i)
  ) {
    // Return a ToolError result rather than throwing a plain object.
    // Throwing non-Error objects loses stack traces and breaks instanceof checks
    // in callers. The tools.ts normalizeError handler then serialises this cleanly.
    throw Object.assign(
      new Error("Only SELECT and WITH queries are allowed"),
      { error: "INVALID_SQL" }
    );
  }

  const db = openDb(vaultRoot);
  try {
    const stmt = db.prepare(sql);
    const rows = stmt.all(...(params ?? [])) as Record<string, unknown>[];
    if (rows.length === 0) {
      const columns = stmt.columns().map((c) => c.name);
      return { columns, rows: [], rowCount: 0 };
    }
    const columns = Object.keys(rows[0]);
    return {
      columns,
      rows: rows.map((r) => columns.map((c) => r[c])),
      rowCount: rows.length,
    };
  } finally {
    db.close();
  }
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
      .prepare("SELECT id, title, date, status FROM docs ORDER BY updated_at DESC LIMIT 10")
      .all() as Record<string, unknown>[];

    const hotConcepts = db
      .prepare(`
        SELECT c.slug, c.title, COUNT(e.id) as edgeCount
        FROM concepts c
        LEFT JOIN edges e ON e.source = c.slug OR e.target = c.slug
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

/** Validate caller identity — SCHIST_AGENT_ID must be set and must match owner */
function assertOwner(owner: string): void {
  const agentId = process.env.SCHIST_AGENT_ID;
  if (!agentId) {
    throw Object.assign(new Error("SCHIST_AGENT_ID env var is required for write operations"), { error: "CONFIG_ERROR" });
  }
  if (agentId !== owner) {
    throw Object.assign(new Error(`Owner '${owner}' does not match SCHIST_AGENT_ID '${agentId}'`), { error: "VALIDATION_ERROR" });
  }
}

/** Validate agent_state key prefix (Ninjia fix) */
function assertKeyPrefix(key: string, owner: string): void {
  const keyPrefix = key.split(".")[0];
  if (keyPrefix !== owner && keyPrefix !== "team") {
    throw Object.assign(new Error(`agent_state: key '${key}' prefix must match owner '${owner}'`), { error: "VALIDATION_ERROR" });
  }
  if (keyPrefix === "team" && owner !== "eleven") {
    throw Object.assign(new Error("agent_state: team.* keys require owner=eleven"), { error: "VALIDATION_ERROR" });
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
  assertOwner(entry.owner);
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
}): MemoryEntry[] {
  const db = openMemoryDb();
  try {
    // Expire TTL-based agent_state rows while we have the DB open
    db.exec(`DELETE FROM agent_state WHERE ttl_hours IS NOT NULL AND
      datetime(updated_at, '+' || ttl_hours || ' hours') < datetime('now')`);

    const limit = opts.limit ?? 50;
    const conditions: string[] = [];
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
      sql += " LIMIT ?";
      params.push(limit);
      const rows = db.prepare(sql).all(...params) as Record<string, unknown>[];
      return rows.map(rowToMemoryEntry);
    } else {
      let sql = "SELECT * FROM agent_memory WHERE 1=1";
      if (opts.owner) { sql += " AND owner = ?"; params.push(opts.owner); }
      if (opts.entry_type) { sql += " AND entry_type = ?"; params.push(opts.entry_type); }
      if (opts.date_from) { sql += " AND date >= ?"; params.push(opts.date_from); }
      if (opts.date_to) { sql += " AND date <= ?"; params.push(opts.date_to); }
      sql += " ORDER BY created_at DESC LIMIT ?";
      params.push(limit);
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
    related_doc: row.related_doc as string | undefined,
    source_ref: row.source_ref as string | undefined,
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
  assertOwner(owner);
  assertKeyPrefix(key, owner);
  const db = openMemoryDb();
  try {
    // H4 fix: prevent silent ownership hijack — check existing key owner before upsert
    const existing = db.prepare("SELECT owner FROM agent_state WHERE key = ?").get(key) as { owner: string } | undefined;
    if (existing && existing.owner !== owner) {
      throw Object.assign(new Error("Cannot overwrite state key owned by another agent"), { error: "OWNERSHIP_ERROR" });
    }
    db.prepare(`
      INSERT INTO agent_state (key, value, owner, ttl_hours)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(key) DO UPDATE SET value=excluded.value,
        ttl_hours=excluded.ttl_hours, updated_at=datetime('now')
    `).run(key, JSON.stringify(value), owner, ttl_hours ?? null);
    const row = db.prepare("SELECT updated_at FROM agent_state WHERE key = ?").get(key) as { updated_at: string };
    return { key, updated_at: row.updated_at };
  } finally {
    db.close();
  }
}

export function deleteAgentState(key: string, owner: string): { deleted: boolean } {
  assertOwner(owner);
  assertKeyPrefix(key, owner);
  const db = openMemoryDb();
  try {
    const result = db.prepare("DELETE FROM agent_state WHERE key = ? AND owner = ?").run(key, owner);
    return { deleted: result.changes > 0 };
  } finally {
    db.close();
  }
}

// ── Domain + alias tools (use schist.db) ──────────────────────────────────

export function listDomains(vaultRoot: string): Domain[] {
  const db = openDb(vaultRoot);
  try {
    const rows = db.prepare("SELECT * FROM research_domains ORDER BY parent_slug NULLS FIRST, slug").all() as Record<string, unknown>[];
    return rows.map(r => ({
      slug: r.slug as string,
      label: r.label as string,
      description: r.description as string | undefined,
      parent_slug: r.parent_slug as string | undefined,
    }));
  } catch {
    // Table may not exist in older DBs — return empty
    return [];
  } finally {
    db.close();
  }
}

export function addConceptAlias(
  vaultRoot: string,
  duplicate_slug: string,
  canonical_slug: string,
  reason: string | undefined,
  created_by: string
): ConceptAlias {
  assertOwner(created_by);
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
