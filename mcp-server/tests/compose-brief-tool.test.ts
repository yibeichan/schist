import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { execFileSync } from "child_process";
import Database from "better-sqlite3";
import { compose_brief } from "../src/tools.js";

async function makeVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-compose-brief-test-"));
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
      scope TEXT DEFAULT 'global',
      source TEXT,
      confidence TEXT,
      created_at TEXT DEFAULT (datetime('now')),
      updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE edges (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      target TEXT NOT NULL,
      type TEXT NOT NULL,
      context TEXT
    );
    CREATE VIRTUAL TABLE docs_fts USING fts5(
      title, body, tags, scope UNINDEXED,
      content='docs', content_rowid='rowid'
    );
    CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
      INSERT INTO docs_fts(rowid, title, body, tags, scope)
      VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
    END;
  `);
  const insertDoc = db.prepare(
    `INSERT INTO docs (id, title, date, tags, body, scope) VALUES (?, ?, ?, ?, ?, ?)`,
  );
  insertDoc.run(
    "research/topic.md",
    "Paper Catalog",
    "2026-06-12",
    JSON.stringify(["papers", "catalog"]),
    "Paper catalog metadata extraction should pack useful filing context.",
    "global",
  );
  insertDoc.run(
    "decisions/pinned.md",
    "Pinned Decision",
    "2026-06-11",
    JSON.stringify(["decision"]),
    "Pinned decision explains why brief composition should not file issues directly.",
    "global",
  );
  insertDoc.run(
    "ops/neighbor.md",
    "Neighbor Note",
    "2026-06-10",
    JSON.stringify(["ops"]),
    "Neighbor note is connected through the graph for context.",
    "global",
  );
  db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`)
    .run("research/topic.md", "ops/neighbor.md", "supports");
  db.close();

  execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
  execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
  execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });
  await fs.mkdir(path.join(dir, "research"), { recursive: true });
  await fs.writeFile(path.join(dir, "research", "new-note.md"), "# New note\n");
  execFileSync("git", ["add", "research/new-note.md"], { cwd: dir });
  execFileSync("git", ["commit", "-m", "add recent note"], { cwd: dir, stdio: "ignore" });

  return dir;
}

let vaultRoot: string;

beforeEach(async () => {
  vaultRoot = await makeVault();
});

afterEach(async () => {
  await fs.rm(vaultRoot, { recursive: true, force: true });
});

describe("compose_brief tool", () => {
  it("packs topic results, pinned notes, graph neighbors, external refs, and recent paths", async () => {
    const result = await compose_brief(vaultRoot, {
      topic: "paper catalog metadata",
      related_notes: ["decisions/pinned.md"],
      related_external: ["github:schist#119"],
    });

    expect(result).not.toHaveProperty("error");
    const brief = result as Awaited<ReturnType<typeof compose_brief>>;
    if ("error" in brief) throw new Error(brief.message);

    expect(brief.markdown).toContain("## Related vault notes");
    expect(brief.markdown).toContain("`research/topic.md`");
    expect(brief.markdown).toContain("`decisions/pinned.md`");
    expect(brief.markdown).toContain("`ops/neighbor.md`");
    expect(brief.markdown).toContain("github:schist#119");
    expect(brief.markdown).toContain("research/new-note.md");
    expect(brief.suggested_tags).toEqual(expect.arrayContaining(["papers", "catalog", "decision", "ops"]));
    expect(brief.cross_refs).toEqual(expect.arrayContaining(["github:schist#119", "research/topic.md"]));
    expect(brief.related_notes.map((note) => note.id)).toEqual(expect.arrayContaining([
      "research/topic.md",
      "decisions/pinned.md",
      "ops/neighbor.md",
    ]));
    expect(brief.recent_paths).toEqual([
      expect.objectContaining({ path: "research/new-note.md" }),
    ]);
  });

  it("validates topic and array arguments", async () => {
    await expect(compose_brief(vaultRoot, { topic: "" })).resolves.toMatchObject({
      error: "VALIDATION_ERROR",
    });
    await expect(compose_brief(vaultRoot, {
      topic: "x",
      related_notes: [123],
    } as never)).resolves.toMatchObject({
      error: "VALIDATION_ERROR",
      message: expect.stringContaining("related_notes"),
    });
  });
});
