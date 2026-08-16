// Alias resolution on the read paths (#489): listConcepts aliasOf/aliases,
// getContext hotConcepts aliasOf, getNote alias_of/aliases on concept notes.
// Fixtures build the index tables by hand (same style as
// list-concepts-sql.test.ts) — one WITH concept_aliases, and one without to
// pin the degrade-gracefully path for pre-alias-era indexes.

import { getContext, getNote, listConcepts } from "../src/sqlite-reader.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";

const createdDirs = new Set<string>();

afterAll(async () => {
  for (const dir of createdDirs) {
    await fs.rm(dir, { recursive: true, force: true });
  }
});

async function makeVault(opts: { withAliasTable: boolean }): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-alias-res-test-"));
  createdDirs.add(dir);
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
      body TEXT NOT NULL,
      scope TEXT DEFAULT 'global',
      source TEXT,
      confidence TEXT,
      file_ref TEXT,
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

    INSERT INTO concepts (slug, title) VALUES
      ('machine-learning', 'Machine Learning'),
      ('ml', 'ML'),
      ('sl', 'SL'),
      ('graphs', 'Graphs');

    INSERT INTO docs (id, title, body, tags) VALUES
      ('notes/seed.md', 'Seed', 'body', '[]'),
      ('concepts/ml.md', 'ML', 'concept body', '[]'),
      ('concepts/machine-learning.md', 'Machine Learning', 'concept body', '[]'),
      ('concepts/graphs.md', 'Graphs', 'concept body', '[]');

    -- ml outranks the canonical on edges so hotConcepts surfaces the
    -- duplicate first — exactly the situation the annotation exists for.
    INSERT INTO edges (source, target, type) VALUES
      ('notes/seed.md', 'ml', 'related'),
      ('concepts/ml.md', 'graphs', 'related'),
      ('notes/seed.md', 'machine-learning', 'mentions');
  `);
  if (opts.withAliasTable) {
    db.exec(`
      CREATE TABLE concept_aliases (
        duplicate_slug  TEXT NOT NULL REFERENCES concepts(slug),
        canonical_slug  TEXT NOT NULL REFERENCES concepts(slug),
        reason          TEXT,
        created_by      TEXT NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (duplicate_slug, canonical_slug)
      );
      INSERT INTO concept_aliases (duplicate_slug, canonical_slug, created_by) VALUES
        ('ml', 'machine-learning', 'tester'),
        ('sl', 'machine-learning', 'tester');
    `);
  }
  db.close();
  return dir;
}

describe("listConcepts alias annotation (#489)", () => {
  it("marks duplicates with aliasOf and canonicals with aliases", async () => {
    const dir = await makeVault({ withAliasTable: true });
    const concepts = listConcepts(dir, { limit: 50 });
    const bySlug = Object.fromEntries(concepts.map((c) => [c.slug, c]));

    expect(bySlug["ml"].aliasOf).toBe("machine-learning");
    expect(bySlug["sl"].aliasOf).toBe("machine-learning");
    expect(bySlug["machine-learning"].aliasOf).toBeUndefined();
    expect(bySlug["machine-learning"].aliases).toEqual(["ml", "sl"]);
    // Uninvolved concepts carry neither field (additive shape — absent, not null).
    expect("aliasOf" in bySlug["graphs"]).toBe(false);
    expect("aliases" in bySlug["graphs"]).toBe(false);
  });

  it("degrades to no annotation when concept_aliases is absent", async () => {
    const dir = await makeVault({ withAliasTable: false });
    const concepts = listConcepts(dir, { limit: 50 });
    expect(concepts.length).toBeGreaterThan(0);
    for (const c of concepts) {
      expect("aliasOf" in c).toBe(false);
      expect("aliases" in c).toBe(false);
    }
  });
});

describe("getContext hotConcepts alias annotation (#489)", () => {
  it("flags hot duplicates with aliasOf", async () => {
    const dir = await makeVault({ withAliasTable: true });
    const ctx = getContext(dir, "standard") as {
      hotConcepts: Array<{ slug: string; aliasOf?: string }>;
    };
    const hot = Object.fromEntries(ctx.hotConcepts.map((c) => [c.slug, c]));
    expect(hot["ml"].aliasOf).toBe("machine-learning");
    expect("aliasOf" in hot["machine-learning"]).toBe(false);
  });

  it("degrades gracefully without the table", async () => {
    const dir = await makeVault({ withAliasTable: false });
    const ctx = getContext(dir, "standard") as {
      hotConcepts: Array<{ slug: string; aliasOf?: string }>;
    };
    for (const c of ctx.hotConcepts) expect("aliasOf" in c).toBe(false);
  });
});

describe("getNote alias annotation on concept notes (#489)", () => {
  it("reports alias_of on a duplicate's note and aliases on the canonical's", async () => {
    const dir = await makeVault({ withAliasTable: true });

    const dup = getNote(dir, "concepts/ml.md");
    expect(dup?.alias_of).toBe("machine-learning");
    expect(dup?.aliases).toBeUndefined();

    const canonical = getNote(dir, "concepts/machine-learning.md");
    expect(canonical?.alias_of).toBeUndefined();
    expect(canonical?.aliases).toEqual(["ml", "sl"]);
  });

  it("leaves non-concept notes and unaliased concepts unannotated", async () => {
    const dir = await makeVault({ withAliasTable: true });
    const note = getNote(dir, "notes/seed.md");
    expect(note?.alias_of).toBeUndefined();
    expect(note?.aliases).toBeUndefined();
    const graphs = getNote(dir, "concepts/graphs.md");
    expect(graphs?.alias_of).toBeUndefined();
    expect(graphs?.aliases).toBeUndefined();
  });

  it("degrades gracefully without the table", async () => {
    const dir = await makeVault({ withAliasTable: false });
    const dup = getNote(dir, "concepts/ml.md");
    expect(dup).not.toBeNull();
    expect(dup?.alias_of).toBeUndefined();
  });
});
