import { queryGraph, searchNotes, resetAgentScopeMap } from "../src/sqlite-reader.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";

async function makeTempDb(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-db-test-"));
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
      body TEXT NOT NULL DEFAULT '',
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE concepts (
      slug TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      description TEXT,
      tags TEXT,
      created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      target TEXT NOT NULL,
      type TEXT NOT NULL,
      context TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      UNIQUE(source, target, type)
    );
    INSERT INTO docs (id, title, body) VALUES ('notes/test.md', 'Test Note', 'Test body');
  `);
  db.close();

  return dir;
}

describe("sqlite-reader", () => {
  test("queryGraph: DROP TABLE is rejected with INVALID_SQL", async () => {
    const vaultRoot = await makeTempDb();
    expect(() => queryGraph(vaultRoot, "DROP TABLE docs")).toThrow(
      expect.objectContaining({ error: "INVALID_SQL" })
    );
  });

  test("queryGraph: CTE with DELETE is rejected with INVALID_SQL", async () => {
    const vaultRoot = await makeTempDb();
    expect(() =>
      queryGraph(vaultRoot, "WITH x AS (DELETE FROM docs RETURNING *) SELECT * FROM x")
    ).toThrow(expect.objectContaining({ error: "INVALID_SQL" }));
  });

  test("queryGraph: valid SELECT returns results", async () => {
    const vaultRoot = await makeTempDb();
    const result = queryGraph(vaultRoot, "SELECT id, title FROM docs LIMIT 10");
    expect(result.columns).toEqual(["id", "title"]);
    expect(result.rows.length).toBeGreaterThan(0);
    expect(result.rows[0][1]).toBe("Test Note");
    expect(result.rowCount).toBeGreaterThan(0);
  });
});

// ── searchNotes scope=inherit (#62) ───────────────────────────────────────

async function makeScopedVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-scope-test-"));
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
    INSERT INTO docs (id, title, body, scope) VALUES
      ('notes/global.md',   'global haystack',   'shared knowledge', 'global'),
      ('notes/octopus.md',  'octopus haystack',  'shared knowledge', 'octopus'),
      ('notes/sansan.md',   'sansan haystack',   'shared knowledge', 'sansan');
  `);
  db.close();

  await fs.writeFile(
    path.join(dir, "vault.yaml"),
    [
      "name: scope-test",
      "participants:",
      "  - { name: octopus, default_scope: octopus }",
      "  - { name: sansan,  default_scope: sansan }",
      "",
    ].join("\n")
  );

  return dir;
}

describe("searchNotes scope='inherit' identity resolution (#62)", () => {
  const envKeys = ["SCHIST_AGENT_ID", "SCHIST_AGENT_NAME", "SCHIST_ALLOWED_AGENTS"] as const;
  const saved: Record<string, string | undefined> = {};

  beforeEach(() => {
    for (const k of envKeys) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
    resetAgentScopeMap();
  });

  afterEach(() => {
    for (const k of envKeys) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
    resetAgentScopeMap();
  });

  it("per-call owner resolves to that agent's default scope (allowlist-only deployment)", async () => {
    const vault = await makeScopedVault();
    process.env.SCHIST_ALLOWED_AGENTS = "octopus,sansan";

    const r = searchNotes(vault, "haystack", { scope: "inherit", owner: "octopus" });
    const ids = r.map((x) => x.id).sort();
    expect(ids).toEqual(["notes/global.md", "notes/octopus.md"]);
    expect(ids).not.toContain("notes/sansan.md");
  });

  it("per-call owner wins over SCHIST_AGENT_ID env", async () => {
    const vault = await makeScopedVault();
    process.env.SCHIST_AGENT_ID = "octopus";

    const r = searchNotes(vault, "haystack", { scope: "inherit", owner: "sansan" });
    const ids = r.map((x) => x.id).sort();
    expect(ids).toEqual(["notes/global.md", "notes/sansan.md"]);
  });

  it("falls back to SCHIST_AGENT_NAME when owner not passed", async () => {
    const vault = await makeScopedVault();
    process.env.SCHIST_AGENT_NAME = "sansan";

    const r = searchNotes(vault, "haystack", { scope: "inherit" });
    const ids = r.map((x) => x.id).sort();
    expect(ids).toEqual(["notes/global.md", "notes/sansan.md"]);
  });

  it("falls back to SCHIST_AGENT_ID when neither owner nor SCHIST_AGENT_NAME set", async () => {
    const vault = await makeScopedVault();
    process.env.SCHIST_AGENT_ID = "octopus";

    const r = searchNotes(vault, "haystack", { scope: "inherit" });
    const ids = r.map((x) => x.id).sort();
    expect(ids).toEqual(["notes/global.md", "notes/octopus.md"]);
  });

  it("documents the #62 regression: scope-inherit with no signals at all collapses to global", async () => {
    // Allowlist-only deployment (no per-process AGENT_ID) calling search_notes
    // without `owner` is the pre-fix bug shape. We don't *fix* that case here —
    // the agent is the only one who knows its identity. Just document that the
    // resolution silently returns global rather than throwing, so callers can
    // detect the gap and start passing `owner`.
    const vault = await makeScopedVault();
    process.env.SCHIST_ALLOWED_AGENTS = "octopus,sansan";

    const r = searchNotes(vault, "haystack", { scope: "inherit" });
    const ids = r.map((x) => x.id).sort();
    expect(ids).toEqual(["notes/global.md"]);
  });
});
