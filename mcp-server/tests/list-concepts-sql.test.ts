import { getContext, listConcepts } from "../src/sqlite-reader.js";
import * as fs from "fs/promises";
import * as path from "path";
import * as os from "os";
import Database from "better-sqlite3";

const createdDirs = new Set<string>();

async function makeConceptVault(
  concepts: Array<{ slug: string; title: string; edgeCount?: number }>
): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-concepts-test-"));
  createdDirs.add(dir);
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
  `);
  db.prepare(
    `INSERT INTO docs (id, title, body, tags) VALUES (?, ?, ?, ?)`,
  ).run("notes/seed.md", "Seed", "body", "[]");

  const insertConcept = db.prepare(
    `INSERT INTO concepts (slug, title) VALUES (?, ?)`
  );
  const insertEdge = db.prepare(
    `INSERT INTO edges (source, target, type) VALUES (?, ?, 'related')`
  );

  let edgeSeq = 0;
  for (const c of concepts) {
    insertConcept.run(c.slug, c.title);
    // Create the desired number of edges for this concept by pairing with
    // a synthetic "dummy-<n>" target that we don't need to register as a concept.
    const count = c.edgeCount ?? 0;
    for (let i = 0; i < count; i++) {
      edgeSeq++;
      insertEdge.run(c.slug, `dummy-${edgeSeq}`);
    }
  }
  db.close();

  return dir;
}

describe("listConcepts — cursor pagination", () => {
  afterAll(async () => {
    for (const dir of createdDirs) {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });

  // Case 1: offset 0 default returns results without error
  it("offset 0 default: no opts yields first page", async () => {
    const vault = await makeConceptVault([
      { slug: "alpha", title: "Alpha" },
      { slug: "beta", title: "Beta" },
    ]);
    const results = listConcepts(vault);
    expect(results.length).toBe(2);
    const slugs = results.map((r) => r.slug);
    expect(slugs).toContain("alpha");
    expect(slugs).toContain("beta");
  });

  // Case 2: offset N pagination — union of two pages = full set, no overlap
  it("offset pagination: two pages cover all 10 concepts without overlap", async () => {
    const concepts = Array.from({ length: 10 }, (_, i) => ({
      slug: `concept-${String(i).padStart(2, "0")}`,
      title: `Concept ${i}`,
    }));
    const vault = await makeConceptVault(concepts);

    const page1 = listConcepts(vault, { limit: 5, offset: 0 });
    const page2 = listConcepts(vault, { limit: 5, offset: 5 });

    expect(page1.length).toBe(5);
    expect(page2.length).toBe(5);

    const slugs1 = new Set(page1.map((r) => r.slug));
    const slugs2 = new Set(page2.map((r) => r.slug));

    // No overlap
    for (const s of slugs1) {
      expect(slugs2.has(s)).toBe(false);
    }

    // Union covers all 10
    const allSlugs = new Set([...slugs1, ...slugs2]);
    expect(allSlugs.size).toBe(10);
    for (const c of concepts) {
      expect(allSlugs.has(c.slug)).toBe(true);
    }
  });

  // Case 3: tiebreaker stability — edgeCount ties broken by c.slug ASC
  it("tiebreaker stability: slug ASC tiebreaker within edgeCount groups", async () => {
    // 3 concepts with edgeCount=1, slugs: "b-high", "c-high", "a-high"
    // 3 concepts with edgeCount=0, slugs: "z-low", "m-low", "a-low"
    const vault = await makeConceptVault([
      { slug: "b-high", title: "B High", edgeCount: 1 },
      { slug: "c-high", title: "C High", edgeCount: 1 },
      { slug: "a-high", title: "A High", edgeCount: 1 },
      { slug: "z-low", title: "Z Low", edgeCount: 0 },
      { slug: "m-low", title: "M Low", edgeCount: 0 },
      { slug: "a-low", title: "A Low", edgeCount: 0 },
    ]);

    const page1 = listConcepts(vault, { limit: 3, offset: 0 });
    const page2 = listConcepts(vault, { limit: 3, offset: 3 });

    // Page 1: the three edgeCount=1 concepts in slug ASC order
    expect(page1.map((r) => r.slug)).toEqual(["a-high", "b-high", "c-high"]);

    // Page 2: the three edgeCount=0 concepts in slug ASC order
    expect(page2.map((r) => r.slug)).toEqual(["a-low", "m-low", "z-low"]);

    // No overlap, full coverage
    const allSlugs = [...page1, ...page2].map((r) => r.slug);
    expect(new Set(allSlugs).size).toBe(6);
  });

  // Case 4: limit precedence preserved
  it("limit precedence: opts.limit=2 caps to 2 rows", async () => {
    const concepts = Array.from({ length: 8 }, (_, i) => ({
      slug: `item-${i}`,
      title: `Item ${i}`,
    }));
    const vault = await makeConceptVault(concepts);

    const results = listConcepts(vault, { limit: 2 });
    expect(results.length).toBe(2);
  });

  it("counts both slug and concepts/<slug>.md edge endpoints", async () => {
    const vault = await makeConceptVault([
      { slug: "backpropagation", title: "Backpropagation" },
    ]);
    const db = new Database(path.join(vault, ".schist", "schist.db"));
    db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
      "notes/seed.md", "backpropagation", "references",
    );
    db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
      "notes/seed.md", "concepts/backpropagation.md", "extends",
    );
    db.close();

    const [concept] = listConcepts(vault);

    expect(concept.slug).toBe("backpropagation");
    expect(concept.edgeCount).toBe(2);
  });

  it("uses the same path-aware edge count for getContext hotConcepts", async () => {
    const vault = await makeConceptVault([
      { slug: "backpropagation", title: "Backpropagation" },
    ]);
    const db = new Database(path.join(vault, ".schist", "schist.db"));
    db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
      "notes/seed.md", "backpropagation", "references",
    );
    db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
      "notes/seed.md", "concepts/backpropagation.md", "extends",
    );
    db.close();

    const context = getContext(vault) as { hotConcepts: Array<{ slug: string; edgeCount: number }> };

    expect(context.hotConcepts[0]).toMatchObject({
      slug: "backpropagation",
      edgeCount: 2,
    });
  });
});
