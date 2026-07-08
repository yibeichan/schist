import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { execFileSync } from "child_process";
import Database from "better-sqlite3";
import { query_graph } from "../src/tools.js";
import { resetCursorForTesting, issueCursor } from "../src/protocol/index.js";

// query_graph hits the vault SQLite DB at <vault>/.schist/schist.db.
// Tests build a minimal docs schema and seed N rows so the cursor pipeline +
// subquery wrap can be exercised without a real ingest run.

async function makeVault(n: number = 0): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-qg-tool-test-"));
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
      body TEXT NOT NULL DEFAULT '',
      created_at TEXT DEFAULT (datetime('now'))
    );
  `);
  if (n > 0) {
    const stmt = db.prepare(`INSERT INTO docs (id, title, date) VALUES (?, ?, ?)`);
    const seedDocs = db.transaction((count: number) => {
      for (let i = 0; i < count; i++) {
        const idx = String(i).padStart(4, "0");
        stmt.run(`notes/${idx}.md`, `Note ${idx}`, `2026-05-${(i % 28) + 1}`);
      }
    });
    seedDocs(n);
  }
  db.close();
  return dir;
}

let vaultRoot: string;
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = [
  "SCHIST_AGENT_ID",
  "SCHIST_QUERY_GRAPH_BYTE_BUDGET",
  "SCHIST_QUERY_GRAPH_TIMEOUT_MS",
] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  resetCursorForTesting();
});

afterEach(async () => {
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  if (vaultRoot) await fs.rm(vaultRoot, { recursive: true, force: true });
});

// ── canonicalize errors ────────────────────────────────────────────────────

describe("query_graph tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    vaultRoot = await makeVault();
    const r = await query_graph(vaultRoot, { sql: "SELECT 1", limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});

// ── SQL guards (unchanged behavior) ────────────────────────────────────────

describe("query_graph tool — SQL guards still in force", () => {
  it("rejects DROP TABLE with INVALID_SQL", async () => {
    vaultRoot = await makeVault();
    const r = await query_graph(vaultRoot, { sql: "DROP TABLE docs" });
    expect(r).toMatchObject({ error: "INVALID_SQL" });
  });

  it("rejects CTE-wrapped DELETE with INVALID_SQL", async () => {
    vaultRoot = await makeVault();
    const r = await query_graph(vaultRoot, {
      sql: "WITH x AS (DELETE FROM docs RETURNING *) SELECT * FROM x",
    });
    expect(r).toMatchObject({ error: "INVALID_SQL" });
  });

  it("rejects multi-statement input (better-sqlite3 prepare error)", async () => {
    vaultRoot = await makeVault(3);
    // Two statements joined by ';' — better-sqlite3.prepare() rejects this.
    // The subquery wrap can't rescue it; we expect an INVALID_SQL envelope.
    const r = await query_graph(vaultRoot, {
      sql: "SELECT 1; SELECT 2",
    });
    expect(r).toMatchObject({ error: expect.any(String) });
    expect("error" in r ? r.error : "").not.toBe("");
  });

  it("allows SQLite REPLACE() string function in SELECT queries", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT REPLACE(title, 'Note', 'Doc') AS renamed FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.columns).toEqual(["renamed"]);
    expect(r.rows[0][0]).toBe("Doc 0000");
  });

  it("allows blocked keywords inside string literals", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs WHERE title <> 'DROP TABLE test' AND 'CREATE' = 'CREATE'",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(1);
  });

  it("allows blocked keywords inside SQL comments", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs /* DROP */ WHERE title <> 'missing' -- CREATE\nORDER BY id",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(1);
  });

  it("reads a WAL-mode vault DB through the readonly child (#254)", async () => {
    // Ingest now switches vault DBs to WAL; the readonly query child and
    // openDb must keep working against that journal mode.
    vaultRoot = await makeVault(3);
    const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
    expect(db.pragma("journal_mode = WAL", { simple: true })).toBe("wal");
    db.close();

    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs ORDER BY id" });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(3);
  });

  it("allows backtick-quoted identifiers named after blocked keywords (#253)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id AS `create`, title AS `delete` FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.columns).toEqual(["create", "delete"]);
    expect(r.rows.length).toBe(1);
  });

  it("allows a backtick-quoted table alias named after a blocked keyword (#253)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT `DROP`.id FROM docs AS `DROP`",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(1);
  });

  it("allows bracket-quoted identifiers named after blocked keywords (#253)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id AS [update] FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.columns).toEqual(["update"]);
    expect(r.rows.length).toBe(1);
  });

  it("still rejects DML keywords outside backtick identifiers (#253)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "WITH x AS (SELECT 1) DELETE FROM `docs`",
    });
    expect(r).toMatchObject({ error: "INVALID_SQL" });
  });

  it("rejects CTE-prefixed REPLACE INTO with a clean INVALID_SQL (#313)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "WITH x AS (SELECT 1) REPLACE INTO docs (id, title, date, body) VALUES ('x', 'y', 'z', 'w')",
    });
    expect(r).toMatchObject({ error: "INVALID_SQL" });
  });

  it("rejects bare REPLACE INTO (#313)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "REPLACE INTO docs (id, title, date, body) VALUES ('x', 'y', 'z', 'w')",
    });
    expect(r).toMatchObject({ error: "INVALID_SQL" });
  });

  it("still allows the scalar REPLACE function (#313)", async () => {
    vaultRoot = await makeVault(1);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT REPLACE(title, 'Note', 'Doc') AS renamed FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows[0][0]).toBe("Doc 0000");
  });
});

// ── cursor decoding ────────────────────────────────────────────────────────

describe("query_graph tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT * FROM docs",
      cursor: "garbage.notreallya.cursor",
    });
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    vaultRoot = await makeVault(5);
    const c = issueCursor({ tool: "search_notes", queryHash: "deadbeef", offset: 5 });
    const r = await query_graph(vaultRoot, { sql: "SELECT * FROM docs", cursor: c });
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("search_notes") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding)", async () => {
    vaultRoot = await makeVault(5);
    const c = issueCursor({ tool: "query_graph", queryHash: "0".repeat(64), offset: 2 });
    const r = await query_graph(vaultRoot, { sql: "SELECT * FROM docs", cursor: c });
    expect(r).toEqual({
      error: "CURSOR_QUERY_MISMATCH",
      message: expect.stringContaining("different query"),
    });
  });
});

// ── identical-query refusal ────────────────────────────────────────────────

describe("query_graph tool — identical-query refusal", () => {
  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    vaultRoot = await makeVault(150);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { sql: "SELECT * FROM docs" };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "query_graph", queryHash: ch.queryHash, owner: "", vaultRoot, verboseEnabled: false });
    const r = await query_graph(vaultRoot, args);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });
});

// ── subquery wrap: BREAKING change ─────────────────────────────────────────

describe("query_graph tool — subquery wrap (breaking change)", () => {
  it("caps `SELECT * FROM docs` at default 100 rows + cursor", async () => {
    vaultRoot = await makeVault(150);
    const r = await query_graph(vaultRoot, { sql: "SELECT * FROM docs" });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(100);
    expect(r.rowCount).toBe(100);
    expect(typeof r.cursor).toBe("string");
  });

  it("returns exactly the caller's inner LIMIT when smaller than the outer cap", async () => {
    vaultRoot = await makeVault(50);
    // Inner LIMIT 5 → 5 rows returned. Outer LIMIT 101 (effectiveLimit+1) sees
    // only 5 rows, so hasMore is false, no cursor.
    const r = await query_graph(vaultRoot, { sql: "SELECT * FROM docs LIMIT 5" });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows.length).toBe(5);
    expect(r.cursor).toBeUndefined();
  });

  it("preserves the caller's ORDER BY across paginated pages", async () => {
    vaultRoot = await makeVault(20);
    // ORDER BY id DESC: ids are notes/0019.md … notes/0000.md
    const r1 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs ORDER BY id DESC",
      limit: 5,
    });
    if (!("rows" in r1)) throw new Error("expected rows");
    expect(r1.rows.length).toBe(5);
    expect(r1.cursor).toBeDefined();
    // Page 1 should be the 5 highest-id notes: 0019 → 0015
    expect(r1.rows.map(r => r[0])).toEqual([
      "notes/0019.md", "notes/0018.md", "notes/0017.md", "notes/0016.md", "notes/0015.md",
    ]);
    const r2 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs ORDER BY id DESC",
      limit: 5,
      cursor: r1.cursor,
    });
    if (!("rows" in r2)) throw new Error("expected rows");
    expect(r2.rows.map(r => r[0])).toEqual([
      "notes/0014.md", "notes/0013.md", "notes/0012.md", "notes/0011.md", "notes/0010.md",
    ]);
  });

  it("WITH (CTE) wraps correctly", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, {
      sql: "WITH ranked AS (SELECT id, date FROM docs ORDER BY date) SELECT * FROM ranked",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(5);
  });

  it("strips a trailing semicolon (ergonomic affordance, not a security risk)", async () => {
    vaultRoot = await makeVault(3);
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs;" });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(3);
  });

  it("cursor advances pagination (page 1 + cursor → page 2, no overlap)", async () => {
    vaultRoot = await makeVault(20);
    const r1 = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 7 });
    if (!("rows" in r1)) throw new Error("expected rows");
    expect(r1.cursor).toBeDefined();
    const r2 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs",
      limit: 7,
      cursor: r1.cursor,
    });
    if (!("rows" in r2)) throw new Error("expected rows");
    expect(r2.rows.length).toBe(7);
    const ids1 = new Set(r1.rows.map(r => r[0] as string));
    for (const row of r2.rows) {
      expect(ids1.has(row[0] as string)).toBe(false);
    }
  });

  it("the last page does NOT return a cursor", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 10 });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows.length).toBe(5);
    expect(r.cursor).toBeUndefined();
  });
});

// ── limit handling ─────────────────────────────────────────────────────────

describe("query_graph tool — limit handling", () => {
  it("clamps limit at 1000 (cap from spec)", async () => {
    vaultRoot = await makeVault(1200);
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 99999 });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows.length).toBeLessThanOrEqual(1000);
  });

  it("collapses limit: 0 to default 100", async () => {
    vaultRoot = await makeVault(150);
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 0 });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows.length).toBe(100);
    expect(r.cursor).toBeDefined();
  });

  it("returns empty rows + columns + no cursor when no rows match", async () => {
    vaultRoot = await makeVault(0);
    const r = await query_graph(vaultRoot, { sql: "SELECT id, title FROM docs" });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows).toEqual([]);
    expect(r.rowCount).toBe(0);
    expect(r.columns).toEqual(["id", "title"]);
    expect(r.cursor).toBeUndefined();
  });
});

// ── adversarial SQL: comment-based wrap escape attempts ───────────────────

describe("query_graph tool — subquery wrap is structurally safe against comment-escape attempts", () => {
  // The wrap embeds caller SQL as `SELECT * FROM (${trimmed}) AS user_query LIMIT ? OFFSET ?`.
  // A caller might try to close the subquery early with `)` and comment out the
  // wrap suffix with `--` or `/* ... */`. None of these should silently bypass
  // the cap — they should either return correct results (cap respected) or
  // surface a clean INVALID_SQL envelope. They MUST NOT silently return
  // unbounded rows.

  it("trailing `--` line comment errors cleanly (wrap parens become unbalanced)", async () => {
    vaultRoot = await makeVault(150);
    // Caller's SQL: `SELECT id FROM docs) AS x --`
    // After wrap:   `SELECT * FROM (SELECT id FROM docs) AS x --) AS user_query LIMIT ? OFFSET ?`
    // The `--` comments out the wrap suffix → outer LIMIT/OFFSET are gone →
    // 2 extra params for 0 placeholders → better-sqlite3 throws.
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs) AS x --" });
    expect(r).toMatchObject({ error: expect.any(String) });
    // Must NOT silently return all 150 rows.
    expect("rows" in r).toBe(false);
  });

  it("unterminated `/*` block comment errors cleanly", async () => {
    vaultRoot = await makeVault(150);
    const r = await query_graph(vaultRoot, { sql: "SELECT id FROM docs) AS x /*" });
    expect(r).toMatchObject({ error: expect.any(String) });
    expect("rows" in r).toBe(false);
  });

  it("legitimate `--` inline comment inside SQL works (must not be falsely rejected)", async () => {
    vaultRoot = await makeVault(5);
    // `--` inside an otherwise normal SELECT is a legitimate SQL comment and
    // must not break the wrap or trigger any spurious error.
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id, title -- pick id and title\nFROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows, got ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(5);
  });

  it("legitimate `/* ... */` block comment inside SQL works", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, {
      sql: "SELECT /* descriptive comment */ id, title FROM docs",
    });
    if (!("rows" in r)) throw new Error("expected rows");
    expect(r.rows.length).toBe(5);
  });

  it("leading `--` line comment must not be falsely rejected (#222)", async () => {
    vaultRoot = await makeVault(5);
    // LLMs routinely prefix a reasoning comment before the SELECT; the masker
    // blanks it to spaces, so without trimStart the ^SELECT anchor fails.
    const r = await query_graph(vaultRoot, {
      sql: "-- most-connected concepts\nSELECT id, title FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows, got ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(5);
  });

  it("leading `/* ... */` block comment must not be falsely rejected (#222)", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, {
      sql: "/* describe the query */ SELECT id, title FROM docs",
    });
    if (!("rows" in r)) throw new Error(`expected rows, got ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(5);
  });

  it("leading comment before WITH (CTE) must not be falsely rejected (#222)", async () => {
    vaultRoot = await makeVault(5);
    const r = await query_graph(vaultRoot, {
      sql: "-- rank notes\nWITH r AS (SELECT id, title FROM docs) SELECT * FROM r",
    });
    if (!("rows" in r)) throw new Error(`expected rows, got ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(5);
  });
});

// ── concurrent-ingest staleness (#90 / #246) ───────────────────────────────

describe("query_graph tool — cursor staleness on rebuild (#246)", () => {
  // PR #241 threaded vaultGeneration through query_graph alongside
  // search_notes; only search_notes had regression coverage. Mirrors
  // tests/search-notes-tool.test.ts's canonical model.
  function gitInitCommit(dir: string, message: string): void {
    execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
    execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
    execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });
    execFileSync("git", ["commit", "--allow-empty", "-m", message], { cwd: dir, stdio: "ignore" });
  }

  it("returns CURSOR_STALE when vault is rebuilt between pages", async () => {
    vaultRoot = await makeVault(10);
    gitInitCommit(vaultRoot, "initial");

    const r1 = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 3 });
    if (!("rows" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");

    // A commit rebuilds the vault index (HEAD moves), reordering OFFSET rows.
    execFileSync("git", ["commit", "--allow-empty", "-m", "concurrent ingest"], {
      cwd: vaultRoot,
      stdio: "ignore",
    });

    const r2 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs",
      limit: 3,
      cursor: r1.cursor,
    });
    expect(r2).toMatchObject({ error: "CURSOR_STALE" });
  });

  it("accepts cursor when HEAD unchanged between pages", async () => {
    vaultRoot = await makeVault(10);
    gitInitCommit(vaultRoot, "initial");

    const r1 = await query_graph(vaultRoot, { sql: "SELECT id FROM docs", limit: 3 });
    if (!("rows" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");

    const r2 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs",
      limit: 3,
      cursor: r1.cursor,
    });
    if (!("rows" in r2)) throw new Error(`expected rows, got ${JSON.stringify(r2)}`);
    expect(r2.rows.length).toBe(3);
    const ids1 = new Set(r1.rows.map((r) => r[0] as string));
    for (const row of r2.rows) expect(ids1.has(row[0] as string)).toBe(false);
  });
});

// ── pagination math: caller LIMIT > effectiveLimit ─────────────────────────

describe("query_graph tool — caller inner LIMIT crosses page boundaries correctly", () => {
  it("caller LIMIT 35 with server effective-limit 30 paginates to 30 + 5 = 35 total", async () => {
    // Pins the math against the adversarial concern that a cursor issued on
    // page 1 could point past the inner LIMIT. effectiveLimit = 30 (passed as
    // args.limit); inner LIMIT = 35. Page 1 returns 30, cursor offset 30.
    // Page 2 returns 5 (inner has 35, outer offset 30 reads positions 30-35).
    vaultRoot = await makeVault(100);
    const r1 = await query_graph(vaultRoot, { sql: "SELECT id FROM docs LIMIT 35", limit: 30 });
    if (!("rows" in r1)) throw new Error(`expected rows on page 1: ${JSON.stringify(r1)}`);
    expect(r1.rows.length).toBe(30);
    expect(r1.cursor).toBeDefined();
    const r2 = await query_graph(vaultRoot, {
      sql: "SELECT id FROM docs LIMIT 35",
      limit: 30,
      cursor: r1.cursor,
    });
    if (!("rows" in r2)) throw new Error("expected rows on page 2");
    expect(r2.rows.length).toBe(5);
    expect(r2.cursor).toBeUndefined();
  });
});

// ── parameter binding ──────────────────────────────────────────────────────

describe("query_graph tool — caller params bound under the wrap", () => {
  it("inner `?` placeholders bind to caller params; outer ?,? bind to limit/offset", async () => {
    vaultRoot = await makeVault(5);
    // Caller's WHERE clause uses `?` — must bind to "notes/0002.md" while the
    // server-appended outer LIMIT/OFFSET use their own positional binds.
    const r = await query_graph(vaultRoot, {
      sql: "SELECT id, title FROM docs WHERE id = ?",
      params: ["notes/0002.md"],
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows.length).toBe(1);
    expect(r.rows[0][0]).toBe("notes/0002.md");
    expect(r.rows[0][1]).toBe("Note 0002");
  });
});

// ── resource hardening ─────────────────────────────────────────────────────

describe("query_graph tool — resource hardening", () => {
  it("rejects responses that exceed the configured byte budget", async () => {
    vaultRoot = await makeVault(1);
    process.env.SCHIST_QUERY_GRAPH_BYTE_BUDGET = "1024";

    const r = await query_graph(vaultRoot, {
      sql: "SELECT randomblob(2048) AS payload FROM docs LIMIT 1",
    });

    expect(r).toMatchObject({
      error: "QUERY_RESPONSE_TOO_LARGE",
      message: expect.stringContaining("byte budget"),
    });
  });

  // ── busy-timeout budget (#311 item 3) ────────────────────────────────────
  // The readonly child derives its SQLite busy_timeout from the outer
  // kill-timeout: max(1000, timeoutMs − 1000). A regression back to a
  // hardcoded 5000 lets a lock-contended query burn its whole budget waiting
  // on the lock (see #254 review). `SELECT timeout FROM pragma_busy_timeout`
  // runs on the child's own connection AFTER it applies the pragma, so the
  // value returned is exactly what the child received — no spawn interception.

  it("readonly child receives busyTimeoutMs = timeoutMs − 1000 (#311)", async () => {
    vaultRoot = await makeVault(1);
    process.env.SCHIST_QUERY_GRAPH_TIMEOUT_MS = "7500";

    const r = await query_graph(vaultRoot, {
      sql: "SELECT timeout FROM pragma_busy_timeout",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows[0][0]).toBe(6500);
  });

  it("readonly child receives the default budget 4000 when no timeout env is set (#311)", async () => {
    vaultRoot = await makeVault(1);
    // beforeEach cleared SCHIST_QUERY_GRAPH_TIMEOUT_MS → default 5000 − 1000.
    const r = await query_graph(vaultRoot, {
      sql: "SELECT timeout FROM pragma_busy_timeout",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows[0][0]).toBe(4000);
  });

  it("busy-timeout budget is floored at 1000ms for very small outer timeouts (#311)", async () => {
    vaultRoot = await makeVault(1);
    // 1500 − 1000 = 500, below the 1s floor → max(1000, 500) = 1000.
    process.env.SCHIST_QUERY_GRAPH_TIMEOUT_MS = "1500";

    const r = await query_graph(vaultRoot, {
      sql: "SELECT timeout FROM pragma_busy_timeout",
    });
    if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
    expect(r.rows[0][0]).toBe(1000);
  });

  it("readonly child reads committed rows through a LIVE, un-checkpointed WAL (#311)", async () => {
    // The existing WAL test (#254) closes the writer before querying, which
    // checkpoints the -wal into the main file. Here the writer connection
    // stays OPEN across the child's read, so the inserted row lives only in
    // the -wal sibling — the readonly child must replay it.
    vaultRoot = await makeVault(3);
    const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
    try {
      expect(db.pragma("journal_mode = WAL", { simple: true })).toBe("wal");
      db.prepare(`INSERT INTO docs (id, title) VALUES (?, ?)`).run(
        "notes/wal-only.md",
        "Wal Only",
      );

      const r = await query_graph(vaultRoot, {
        sql: "SELECT id FROM docs ORDER BY id",
      });
      if (!("rows" in r)) throw new Error(`expected rows: ${JSON.stringify(r)}`);
      expect(r.rows.length).toBe(4);
      expect(r.rows.map((row) => row[0])).toContain("notes/wal-only.md");
    } finally {
      db.close();
    }
  });

  it("interrupts CPU-heavy queries at the configured timeout", async () => {
    vaultRoot = await makeVault(1);
    process.env.SCHIST_QUERY_GRAPH_TIMEOUT_MS = "200";

    const r = await query_graph(vaultRoot, {
      sql: `
        WITH RECURSIVE n(x) AS (
          SELECT 1
          UNION ALL
          SELECT x + 1 FROM n
        )
        SELECT x FROM n LIMIT 1 OFFSET 100000000
      `,
    });

    expect(r).toMatchObject({
      error: "QUERY_TIMEOUT",
      message: expect.stringContaining("timeout"),
    });
  }, 10_000);
});
