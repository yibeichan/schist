import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import { search_notes } from "../src/tools.js";
import { resetCursorForTesting, issueCursor } from "../src/protocol/index.js";
import { resetAgentScopeMap } from "../src/sqlite-reader.js";

// search_notes hits the vault SQLite DB at <vault>/.schist/schist.db.
// Tests build a minimal docs + docs_fts schema and seed N rows so the
// cursor-pipeline behaviors can be exercised without a real ingest run.

async function makeVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-sn-tool-test-"));
  const dbDir = path.join(dir, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");

  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE docs (
      id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      date TEXT,
      status TEXT DEFAULT 'draft',
      tags TEXT,
      concepts TEXT,
      domain TEXT,
      body TEXT NOT NULL DEFAULT '',
      scope TEXT DEFAULT 'global',
      source TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE VIRTUAL TABLE docs_fts USING fts5(
      title, body, tags, scope UNINDEXED, domain UNINDEXED,
      content='docs', content_rowid='rowid'
    );
    CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
      INSERT INTO docs_fts(rowid, title, body, tags, scope, domain)
      VALUES (new.rowid, new.title, new.body, new.tags, new.scope, new.domain);
    END;
  `);
  db.close();
  return dir;
}

// Seed N notes whose body matches the FTS query "haystack" so search_notes
// has results to paginate over. IDs sort lexicographically (zero-padded) so
// the id-ASC tiebreaker yields predictable pagination order.
async function seed(vaultRoot: string, n: number): Promise<void> {
  const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
  try {
    const stmt = db.prepare(
      `INSERT INTO docs (id, title, body, scope) VALUES (?, ?, ?, 'global')`,
    );
    for (let i = 0; i < n; i++) {
      const idx = String(i).padStart(3, "0");
      stmt.run(`notes/${idx}.md`, `Note ${idx}`, `haystack body ${idx}`);
    }
  } finally {
    db.close();
  }
}

let vaultRoot: string;
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = ["SCHIST_AGENT_ID", "SCHIST_AGENT_NAME", "SCHIST_ALLOWED_AGENTS"] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  vaultRoot = await makeVault();
  resetCursorForTesting();
  resetAgentScopeMap();
});

afterEach(async () => {
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  await fs.rm(vaultRoot, { recursive: true, force: true });
});

// ── canonicalize errors ────────────────────────────────────────────────────

describe("search_notes tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await search_notes(vaultRoot, { query: "haystack", limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});

// ── cursor decoding ────────────────────────────────────────────────────────

describe("search_notes tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    await seed(vaultRoot, 5);
    const r = await search_notes(vaultRoot, { query: "haystack", cursor: "garbage.notreallya.cursor" });
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "search_memory", queryHash: "deadbeef", offset: 5 });
    const r = await search_notes(vaultRoot, { query: "haystack", cursor: c });
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("search_memory") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding policy)", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "search_notes", queryHash: "0".repeat(64), offset: 2 });
    const r = await search_notes(vaultRoot, { query: "haystack", cursor: c });
    expect(r).toEqual({
      error: "CURSOR_QUERY_MISMATCH",
      message: expect.stringContaining("different query"),
    });
  });

  it("accepts a cursor whose encoded queryHash matches the current args (round-trip)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash } = await import("../src/protocol/index.js");
    const args = { query: "haystack", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    expect(ch.ok).toBe(true);
    if (!ch.ok) return;
    const c = issueCursor({ tool: "search_notes", queryHash: ch.queryHash, offset: 3 });
    const r = await search_notes(vaultRoot, { ...args, cursor: c });
    expect(r).toHaveProperty("results");
    expect(r).not.toHaveProperty("error");
  });
});

// ── identical-query refusal ────────────────────────────────────────────────

describe("search_notes tool — identical-query refusal", () => {
  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { query: "haystack", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_notes", queryHash: ch.queryHash, owner: "", vaultRoot, verboseEnabled: false });
    const r = await search_notes(vaultRoot, args);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });

  it("does NOT refuse when activeOwner differs (per-call owner namespace)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    // Record under owner "yibei" (different from the call's resolved activeOwner=="")
    const argsAsRecorded = { query: "haystack", limit: 3 };
    const ch = canonicalizeQueryHash(argsAsRecorded, "yibei");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_notes", queryHash: ch.queryHash, owner: "yibei", vaultRoot, verboseEnabled: false });
    const r = await search_notes(vaultRoot, argsAsRecorded);
    expect(r).toHaveProperty("results");
  });

  it("does NOT bypass on verbose-newly-set (search_notes has no verbose mode)", async () => {
    // search_notes ignores `verbose` — the canonicalize rule strips it before
    // hashing, and the handler always passes verboseEnabled=false to checkRefusal.
    // So a verbose "upgrade" must NOT bypass refusal on identical queries.
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { query: "haystack", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_notes", queryHash: ch.queryHash, owner: "", vaultRoot, verboseEnabled: false });
    const r = await search_notes(vaultRoot, {
      ...args,
      verbose: "this verbose is ignored by search_notes",
    } as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.any(String),
    });
  });
});

// ── pagination + cursor issuance ───────────────────────────────────────────

describe("search_notes tool — pagination + cursor issuance", () => {
  it("returns a cursor when results are capped and rows.length === limit", async () => {
    await seed(vaultRoot, 10);
    const r = await search_notes(vaultRoot, { query: "haystack", limit: 3 });
    if (!("results" in r)) throw new Error("expected results");
    expect(r.results.length).toBe(3);
    expect(typeof r.cursor).toBe("string");
  });

  it("does NOT return a cursor when results fit (rows.length < limit)", async () => {
    await seed(vaultRoot, 2);
    const r = await search_notes(vaultRoot, { query: "haystack", limit: 50 });
    if (!("results" in r)) throw new Error("expected results");
    expect(r.results.length).toBe(2);
    expect(r.cursor).toBeUndefined();
  });

  it("cursor advances pagination — page 1 + cursor → page 2 (no overlap, deterministic order)", async () => {
    await seed(vaultRoot, 10);
    const r1 = await search_notes(vaultRoot, { query: "haystack", limit: 3 });
    if (!("results" in r1)) throw new Error("expected results");
    expect(r1.cursor).toBeDefined();
    const r2 = await search_notes(vaultRoot, {
      query: "haystack",
      limit: 3,
      cursor: r1.cursor,
    });
    if (!("results" in r2)) throw new Error("expected results");
    expect(r2.results.length).toBe(3);
    const ids1 = new Set(r1.results.map(e => e.id));
    for (const e of r2.results) {
      expect(ids1.has(e.id)).toBe(false);
    }
  });

  it("the last page does NOT return a cursor", async () => {
    await seed(vaultRoot, 5);
    const r1 = await search_notes(vaultRoot, { query: "haystack", limit: 3 });
    if (!("results" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await search_notes(vaultRoot, {
      query: "haystack",
      limit: 3,
      cursor: r1.cursor,
    });
    if (!("results" in r2)) throw new Error("expected results");
    expect(r2.results.length).toBe(2);
    expect(r2.cursor).toBeUndefined();
  });

  it("clamps limit at 100 (cap from spec)", async () => {
    await seed(vaultRoot, 150);
    const r = await search_notes(vaultRoot, { query: "haystack", limit: 9999 });
    if (!("results" in r)) throw new Error("expected results");
    expect(r.results.length).toBeLessThanOrEqual(100);
  });

  it("collapses limit: 0 to default 20", async () => {
    await seed(vaultRoot, 25);
    const r = await search_notes(vaultRoot, { query: "haystack", limit: 0 });
    if (!("results" in r)) throw new Error("expected results");
    expect(r.results.length).toBe(20);
    expect(r.cursor).toBeDefined();
  });

  it("returns empty results + no cursor when no rows match", async () => {
    await seed(vaultRoot, 3);
    const r = await search_notes(vaultRoot, { query: "nonexistent-token-zzz" });
    if (!("results" in r)) throw new Error("expected results");
    expect(r.results).toEqual([]);
    expect(r.cursor).toBeUndefined();
  });
});

// ── tiebreaker stability ───────────────────────────────────────────────────

// ── scope=inherit interaction with cursor pipeline ─────────────────────────

describe("search_notes tool — scope=inherit + cursor", () => {
  // The orderClauses refactor moved the scope-inherit `CASE WHEN scope=...` from
  // a string concat into an array layered with bm25 + id-ASC. This test pins
  // that pagination over scope=inherit returns each row exactly once, with the
  // CASE prefix still ranking the caller's scope above 'global'.

  async function makeScopedVault(): Promise<string> {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-sn-scope-test-"));
    const dbDir = path.join(dir, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const dbPath = path.join(dbDir, "schist.db");

    const db = new Database(dbPath);
    db.exec(`
      CREATE TABLE docs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        date TEXT,
        status TEXT DEFAULT 'draft',
        tags TEXT,
        concepts TEXT,
        domain TEXT,
        body TEXT NOT NULL DEFAULT '',
        scope TEXT DEFAULT 'global',
        source TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
      );
      CREATE VIRTUAL TABLE docs_fts USING fts5(
        title, body, tags, scope UNINDEXED, domain UNINDEXED,
        content='docs', content_rowid='rowid'
      );
      CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
        INSERT INTO docs_fts(rowid, title, body, tags, scope, domain)
        VALUES (new.rowid, new.title, new.body, new.tags, new.scope, new.domain);
      END;
    `);
    // Seed 5 global + 5 octopus + 5 sansan notes, all matching "haystack"
    const stmt = db.prepare(
      `INSERT INTO docs (id, title, body, scope) VALUES (?, ?, 'haystack body', ?)`,
    );
    for (const scope of ["global", "octopus", "sansan"] as const) {
      for (let i = 0; i < 5; i++) {
        const idx = String(i).padStart(3, "0");
        stmt.run(`notes/${scope}-${idx}.md`, `${scope} ${idx}`, scope);
      }
    }
    db.close();

    await fs.writeFile(
      path.join(dir, "vault.yaml"),
      "name: scope-test\nparticipants:\n  - { name: octopus, default_scope: octopus }\n  - { name: sansan, default_scope: sansan }\n",
    );
    return dir;
  }

  it("paginates scope=inherit results exactly once across pages, with caller's scope ranked above global", async () => {
    const scopedVault = await makeScopedVault();
    try {
      const seen = new Set<string>();
      let cursor: string | undefined;
      const firstPageIds: string[] = [];
      let page = 0;
      while (page < 10) {
        const r: import("../src/types.js").SearchNotesResponse | import("../src/types.js").ToolError =
          await search_notes(scopedVault, {
            query: "haystack",
            scope: "inherit",
            owner: "octopus",
            limit: 3,
            cursor,
          });
        if (!("results" in r)) throw new Error(`unexpected error on page ${page}: ${JSON.stringify(r)}`);
        for (const row of r.results) {
          expect(seen.has(row.id)).toBe(false);
          seen.add(row.id);
          if (page === 0) firstPageIds.push(row.id);
        }
        cursor = r.cursor;
        page++;
        if (cursor === undefined) break;
      }
      // octopus should see 5 octopus notes + 5 global notes = 10 total
      // (sansan-scoped notes are filtered out by the WHERE clause).
      expect(seen.size).toBe(10);
      // First page (3 rows) must be all octopus — the CASE prefix ranks
      // scope==callingScope above scope=='global'. If the CASE layer were
      // dropped or re-ordered, global rows could leak into page 1.
      for (const id of firstPageIds) {
        expect(id.startsWith("notes/octopus-")).toBe(true);
      }
    } finally {
      await fs.rm(scopedVault, { recursive: true, force: true });
    }
  });
});

describe("search_notes tool — id-ASC tiebreaker stability", () => {
  // When bm25 scores tie (e.g. identical body content), the id-ASC tiebreaker
  // is what makes OFFSET pagination deterministic. Without it, the same row
  // can appear on multiple pages or be skipped entirely.

  it("paginates all rows exactly once across page boundaries (no duplicates, no skips)", async () => {
    await seed(vaultRoot, 10);
    // Seeded rows have the FTS query verbatim in body — bm25 ties on identical
    // length / token shape, so the tiebreaker is what enforces order.
    const seen = new Set<string>();
    let cursor: string | undefined;
    let page = 0;
    while (page < 10) {
      const r: import("../src/types.js").SearchNotesResponse | import("../src/types.js").ToolError =
        await search_notes(vaultRoot, { query: "haystack", limit: 3, cursor });
      if (!("results" in r)) throw new Error(`unexpected error on page ${page}: ${JSON.stringify(r)}`);
      for (const row of r.results) {
        expect(seen.has(row.id)).toBe(false);
        seen.add(row.id);
      }
      cursor = r.cursor;
      page++;
      if (cursor === undefined) break;
    }
    expect(seen.size).toBe(10);
  });
});
