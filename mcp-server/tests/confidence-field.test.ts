/**
 * #69 — `confidence` field on docs.
 *
 * Round-trip coverage:
 *   - create_note accepts low|medium|high, omits the field when undeclared
 *   - create_note rejects invalid values with VALIDATION_ERROR
 *   - search_notes filter selects matching confidence and excludes NULL
 *
 * Ingestion-path coverage (frontmatter → SQLite) lives in cli/tests; here we
 * verify the MCP write + read surface area.
 */
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import Database from "better-sqlite3";
import { loadVaultConfig, create_note, search_notes } from "../src/tools.js";

const execFile = promisify(execFileCb);

async function makeTempVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-confidence-"));
  await execFile("git", ["init"], { cwd: dir });
  await execFile("git", ["config", "user.email", "test@test.com"], { cwd: dir });
  await execFile("git", ["config", "user.name", "Test"], { cwd: dir });
  await fs.writeFile(
    path.join(dir, "schist.yaml"),
    [
      "name: Test Vault",
      "write_branch: drafts",
      "directories:",
      "  - notes",
      "statuses:",
      "  - draft",
      "connection_types:",
      "  - related",
      "",
    ].join("\n"),
  );
  await execFile("git", ["add", "."], { cwd: dir });
  await execFile("git", ["commit", "-m", "init"], { cwd: dir });
  return dir;
}

const TEST_AGENT = "test-agent";

beforeAll(() => {
  process.env.SCHIST_AGENT_ID = TEST_AGENT;
});
afterAll(() => {
  delete process.env.SCHIST_AGENT_ID;
});

describe("create_note confidence frontmatter", () => {
  test.each(["low", "medium", "high"] as const)(
    "writes `confidence: %s` to frontmatter",
    async (level) => {
      const vault = await makeTempVault();
      const config = await loadVaultConfig(vault);
      const result = (await create_note(
        vault,
        { owner: TEST_AGENT, title: `Conf ${level}`, body: "body", confidence: level },
        config,
      )) as { path: string };
      const content = await fs.readFile(path.join(vault, result.path), "utf-8");
      expect(content).toMatch(new RegExp(`^confidence:\\s*${level}$`, "m"));
    },
  );

  test("omits `confidence` from frontmatter when not declared", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    const result = (await create_note(
      vault,
      { owner: TEST_AGENT, title: "No confidence", body: "body" },
      config,
    )) as { path: string };
    const content = await fs.readFile(path.join(vault, result.path), "utf-8");
    // NULL state — the field MUST NOT be silently defaulted to 'medium'.
    expect(content).not.toMatch(/^confidence:/m);
  });

  test("rejects invalid confidence with VALIDATION_ERROR", async () => {
    const vault = await makeTempVault();
    const config = await loadVaultConfig(vault);
    const result = (await create_note(
      vault,
      // @ts-expect-error — testing the runtime guard against off-enum strings
      { owner: TEST_AGENT, title: "Bad", body: "body", confidence: "very-high" },
      config,
    )) as { error: string; message: string };
    expect(result.error).toBe("VALIDATION_ERROR");
    expect(result.message).toMatch(/confidence/);
  });
});

describe("search_notes confidence filter", () => {
  // Build a SQLite DB directly (no schist init / ingest) so the test stays
  // unit-scoped: it exercises the SQL filter, not the ingestion pipeline.
  async function seedVault(): Promise<string> {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-conf-search-"));
    const dbDir = path.join(dir, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const db = new Database(path.join(dbDir, "schist.db"));
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
        confidence TEXT
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
    const insert = db.prepare(
      "INSERT INTO docs (id, title, body, scope, confidence) VALUES (?, ?, 'haystack body', 'global', ?)",
    );
    insert.run("notes/lo.md", "Low Note", "low");
    insert.run("notes/me.md", "Med Note", "medium");
    insert.run("notes/hi.md", "High Note", "high");
    insert.run("notes/null.md", "Null Note", null);
    db.close();
    return dir;
  }

  test("filter='high' returns only the high doc", async () => {
    const vault = await seedVault();
    const res = (await search_notes(vault, { query: "haystack", confidence: "high" })) as {
      results: Array<{ id: string; confidence?: string }>;
    };
    expect(res.results.map((r) => r.id)).toEqual(["notes/hi.md"]);
    expect(res.results[0].confidence).toBe("high");
  });

  test("filter excludes NULL-confidence notes", async () => {
    const vault = await seedVault();
    const res = (await search_notes(vault, { query: "haystack", confidence: "low" })) as {
      results: Array<{ id: string }>;
    };
    // 'null' note is NULL in SQLite; AND-equality with 'low' excludes it.
    expect(res.results.map((r) => r.id)).toEqual(["notes/lo.md"]);
  });

  test("no filter returns all notes including NULL-confidence", async () => {
    const vault = await seedVault();
    const res = (await search_notes(vault, { query: "haystack" })) as {
      results: Array<{ id: string; confidence?: string }>;
    };
    expect(res.results.map((r) => r.id).sort()).toEqual(
      ["notes/hi.md", "notes/lo.md", "notes/me.md", "notes/null.md"],
    );
    // Round-trip: SearchResult exposes the field when set, omits when NULL.
    const byId = new Map(res.results.map((r) => [r.id, r.confidence]));
    expect(byId.get("notes/null.md")).toBeUndefined();
  });

  test("invalid filter value returns VALIDATION_ERROR", async () => {
    const vault = await seedVault();
    const res = (await search_notes(vault, {
      query: "haystack",
      // @ts-expect-error — runtime guard against off-enum strings
      confidence: "very-high",
    })) as { error: string; message: string };
    expect(res.error).toBe("VALIDATION_ERROR");
    expect(res.message).toMatch(/confidence/);
  });
});
