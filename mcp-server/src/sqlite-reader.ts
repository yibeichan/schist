import Database from "better-sqlite3";
import * as path from "path";
import type { SearchResult, Note, Concept, Connection } from "./types.js";
import { CONNECTION_RE, parseConnections as parseConnectionsSync } from "./markdown-parser.js";

function openDb(vaultRoot: string): Database.Database {
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  return new Database(dbPath, { readonly: true });
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
    const params: unknown[] = [query];

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
