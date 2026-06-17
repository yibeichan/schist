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

  it("keeps body content around Markdown horizontal rules in annotations (#230)", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-compose-brief-hr-"));
    const dbDir = path.join(dir, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const db = new Database(path.join(dbDir, "schist.db"));
    db.exec(`
      CREATE TABLE docs (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, date TEXT,
        status TEXT DEFAULT 'draft', tags TEXT, concepts TEXT,
        body TEXT NOT NULL DEFAULT '', scope TEXT DEFAULT 'global',
        source TEXT, confidence TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
      );
      CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL, context TEXT);
      CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED, content='docs', content_rowid='rowid');
      CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
        INSERT INTO docs_fts(rowid, title, body, tags, scope) VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
      END;
    `);
    // Body uses Markdown horizontal rules. The old /m frontmatter strip would
    // eat everything between the first and second `---`, dropping the rationale.
    const body = [
      "## Context",
      "Background about transformer scaling laws.",
      "",
      "---",
      "",
      "Critical rationale that must reach the brief annotation.",
      "",
      "---",
      "",
      "Further detail.",
    ].join("\n");
    db.prepare(`INSERT INTO docs (id, title, date, tags, body, scope) VALUES (?, ?, ?, ?, ?, ?)`)
      .run("research/hr.md", "Horizontal Rule Note", "2026-06-16", JSON.stringify(["scaling"]), body, "global");
    db.close();
    execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
    execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
    execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });

    // Pin the note so its annotation is built from the full body (oneLine(body)),
    // which is where the HR-strip bug deletes content; search results annotate
    // from a truncated FTS snippet instead.
    const result = await compose_brief(dir, {
      topic: "transformer scaling rationale",
      related_notes: ["research/hr.md"],
    });
    if ("error" in result) throw new Error(result.message);
    expect(result.related_notes.map((n) => n.id)).toContain("research/hr.md");
    // The annotation is rendered into the markdown brief. With the old /m
    // frontmatter strip, the "Critical rationale" line (between two `---`
    // rules) was silently dropped from the annotation.
    expect(result.markdown).toContain("Critical rationale");

    await fs.rm(dir, { recursive: true, force: true });
  });

  it("returns scope-matching topic results even when out-of-scope notes rank higher (#232)", async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-compose-brief-scope-"));
    const dbDir = path.join(dir, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const db = new Database(path.join(dbDir, "schist.db"));
    db.exec(`
      CREATE TABLE docs (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, date TEXT,
        status TEXT DEFAULT 'draft', tags TEXT, concepts TEXT,
        body TEXT NOT NULL DEFAULT '', scope TEXT DEFAULT 'global',
        source TEXT, confidence TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
      );
      CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL, context TEXT);
      CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED, content='docs', content_rowid='rowid');
      CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
        INSERT INTO docs_fts(rowid, title, body, tags, scope) VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
      END;
    `);
    const insertDoc = db.prepare(`INSERT INTO docs (id, title, date, tags, body, scope) VALUES (?, ?, ?, ?, ?, ?)`);
    // 30 highly-relevant research/ notes outrank a handful of ops/ notes.
    for (let i = 0; i < 30; i++) {
      insertDoc.run(
        `research/nn-${i}.md`, `Research ${i}`, "2026-06-16",
        JSON.stringify(["research"]),
        "neural network training neural network training neural network training",
        "global",
      );
    }
    insertDoc.run(
      "ops/runbook.md", "Ops Runbook", "2026-06-16",
      JSON.stringify(["ops"]),
      "neural network training runbook for the ops cluster",
      "global",
    );
    db.close();
    execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
    execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
    execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });

    const result = await compose_brief(dir, { topic: "neural network training", scope: ["ops"] });
    if ("error" in result) throw new Error(result.message);
    expect(result.related_notes.map((n) => n.id)).toContain("ops/runbook.md");

    await fs.rm(dir, { recursive: true, force: true });
  });

  it("does not overflow SQLite variable limit with a large related_notes list (#231)", async () => {
    // Real pinned notes all land in byId and become graph seeds. The graph
    // query binds 3 × seeds + 2 parameters; once that exceeds SQLite's
    // variable limit it crashes with INGEST_ERROR "too many SQL variables".
    // The bundled SQLite uses the modern 32766 default (older builds use 999),
    // so we seed >10922 notes to cross it without the cap. The fix slices seeds
    // to 200 regardless of build, so this stays well clear on any deployment.
    const COUNT = 11_000;
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-compose-brief-overflow-"));
    const dbDir = path.join(dir, ".schist");
    await fs.mkdir(dbDir, { recursive: true });
    const db = new Database(path.join(dbDir, "schist.db"));
    db.exec(`
      CREATE TABLE docs (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, date TEXT,
        status TEXT DEFAULT 'draft', tags TEXT, concepts TEXT,
        body TEXT NOT NULL DEFAULT '', scope TEXT DEFAULT 'global',
        source TEXT, confidence TEXT,
        created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
      );
      CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL, context TEXT);
      CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED, content='docs', content_rowid='rowid');
      CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
        INSERT INTO docs_fts(rowid, title, body, tags, scope) VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
      END;
    `);
    const insertDoc = db.prepare(`INSERT INTO docs (id, title, date, tags, body, scope) VALUES (?, ?, ?, ?, ?, ?)`);
    const pinned: string[] = [];
    const insertMany = db.transaction(() => {
      for (let i = 0; i < COUNT; i++) {
        const id = `decisions/pinned-${i}.md`;
        insertDoc.run(id, `Decision ${i}`, "2026-06-16", JSON.stringify(["decision"]), `Pinned body ${i}.`, "global");
        pinned.push(id);
      }
    });
    insertMany();
    db.close();
    execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
    execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
    execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });

    const result = await compose_brief(dir, { topic: "decision", related_notes: pinned });
    expect(result).not.toHaveProperty("error");
    const brief = result as Awaited<ReturnType<typeof compose_brief>>;
    if ("error" in brief) throw new Error(brief.message);
    // The brief is capped at 10 notes regardless, and pinned notes still appear.
    expect(brief.related_notes.length).toBeGreaterThan(0);
    expect(brief.related_notes.every((n) => n.id.startsWith("decisions/pinned-"))).toBe(true);

    await fs.rm(dir, { recursive: true, force: true });
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
