import { queryGraph } from "../src/sqlite-reader.js";
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
