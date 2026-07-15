import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import * as fs from "fs/promises";
import { execFileSync } from "child_process";
import Database from "better-sqlite3";
import { list_concepts } from "../src/tools.js";
import { resetCursorForTesting, issueCursor } from "../src/protocol/index.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// list_concepts hits <vault>/.schist/schist.db (concepts + edges tables).
// Tests build a minimal schema and seed N rows so the cursor-pipeline
// behaviours can be exercised without a real ingest run.

async function makeVault(): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-lc-tool-test-"));
  const dbDir = path.join(dir, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");

  const db = new Database(dbPath);
  db.exec(`
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
  db.close();
  return dir;
}

// Seed N concepts with no edges. Slugs are zero-padded so c.slug ASC
// tiebreaker yields a deterministic, lexicographic pagination order.
async function seed(vaultRoot: string, n: number, opts?: { tags?: string[]; description?: string }): Promise<void> {
  const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
  try {
    const stmt = db.prepare(
      `INSERT INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, ?)`,
    );
    const tagsJson = opts?.tags ? JSON.stringify(opts.tags) : null;
    for (let i = 0; i < n; i++) {
      const idx = String(i).padStart(3, "0");
      stmt.run(
        `concept-${idx}`,
        `Concept ${idx}`,
        opts?.description ?? `description ${idx}`,
        tagsJson,
      );
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
});

afterEach(async () => {
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  await fs.rm(vaultRoot, { recursive: true, force: true });
});

// ── canonicalize errors ────────────────────────────────────────────────────

describe("list_concepts tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await list_concepts(vaultRoot, { limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});

// ── cursor decoding ────────────────────────────────────────────────────────

describe("list_concepts tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    await seed(vaultRoot, 5);
    const r = await list_concepts(vaultRoot, { cursor: "garbage.notreallya.cursor" });
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "search_notes", queryHash: "deadbeef", offset: 5 });
    const r = await list_concepts(vaultRoot, { cursor: c });
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("search_notes") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding policy)", async () => {
    await seed(vaultRoot, 5);
    const c = issueCursor({ tool: "list_concepts", queryHash: "0".repeat(64), offset: 2 });
    const r = await list_concepts(vaultRoot, { cursor: c });
    expect(r).toEqual({
      error: "CURSOR_QUERY_MISMATCH",
      message: expect.stringContaining("different query"),
    });
  });

  it("accepts a cursor whose encoded queryHash matches the current args (round-trip)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash } = await import("../src/protocol/index.js");
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    expect(ch.ok).toBe(true);
    if (!ch.ok) return;
    const c = issueCursor({ tool: "list_concepts", queryHash: ch.queryHash, offset: 3 });
    const r = await list_concepts(vaultRoot, { ...args, cursor: c });
    expect(r).toHaveProperty("concepts");
    expect(r).not.toHaveProperty("error");
    if (!("concepts" in r)) throw new Error("expected concepts");
    // Page 2 starting at offset 3 in a 10-concept vault should return up to
    // limit (3) rows, beginning with the 4th concept lexicographically.
    expect(r.concepts.length).toBe(3);
    expect(r.concepts[0].slug).toBe("concept-003");
  });
});

// ── identical-query refusal ────────────────────────────────────────────────

describe("list_concepts tool — identical-query refusal", () => {
  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "list_concepts", queryHash: ch.queryHash, owner: "", vaultRoot, verboseEnabled: false });
    const r = await list_concepts(vaultRoot, args);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });

  it("does NOT refuse when activeOwner differs (per-call owner namespace)", async () => {
    await seed(vaultRoot, 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    // Record under owner "yibei". Current call's activeOwner resolves from
    // SCHIST_AGENT_NAME → SCHIST_AGENT_ID → "", both env vars are unset in
    // beforeEach, so it becomes "" — DIFFERENT from the recorded "yibei".
    const args = { limit: 3 };
    const ch = canonicalizeQueryHash(args, "yibei");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "list_concepts", queryHash: ch.queryHash, owner: "yibei", vaultRoot, verboseEnabled: false });
    const r = await list_concepts(vaultRoot, args);
    expect(r).toHaveProperty("concepts");
    expect(r).not.toHaveProperty("error");
  });

  it("refuses naturally on second identical call after first issued a cursor (round-trip)", async () => {
    // Black-box regression: the prior tests prime the LRU manually with
    // recordIssued. If recordIssued were ever moved out of the `if (hasMore)`
    // block in the handler, those tests would still pass but production
    // behaviour would silently change. This test exercises the full pipeline:
    // call A returns a cursor → recordIssued fires inside the handler → call B
    // with identical args (no cursor) must be refused.
    await seed(vaultRoot, 15);
    const r1 = await list_concepts(vaultRoot, { limit: 5 });
    if (!("concepts" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await list_concepts(vaultRoot, { limit: 5 });
    expect(r2).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });
});

// ── pagination + cursor issuance ───────────────────────────────────────────

describe("list_concepts tool — pagination + cursor issuance", () => {
  it("returns a cursor when results are capped and concepts.length === limit", async () => {
    await seed(vaultRoot, 15);
    const r = await list_concepts(vaultRoot, { limit: 5 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(5);
    expect(typeof r.cursor).toBe("string");
  });

  it("does NOT return a cursor when results fit (concepts.length < limit)", async () => {
    await seed(vaultRoot, 2);
    const r = await list_concepts(vaultRoot, { limit: 50 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(2);
    expect(r.cursor).toBeUndefined();
  });

  it("cursor advances pagination — page 1 + cursor → page 2 (no overlap, deterministic order)", async () => {
    await seed(vaultRoot, 15);
    const r1 = await list_concepts(vaultRoot, { limit: 5 });
    if (!("concepts" in r1)) throw new Error("expected concepts");
    expect(r1.cursor).toBeDefined();
    const r2 = await list_concepts(vaultRoot, { limit: 5, cursor: r1.cursor });
    if (!("concepts" in r2)) throw new Error("expected concepts");
    expect(r2.concepts.length).toBe(5);
    const slugs1 = new Set(r1.concepts.map(c => c.slug));
    for (const c of r2.concepts) {
      expect(slugs1.has(c.slug)).toBe(false);
    }
  });

  it("the last page does NOT return a cursor", async () => {
    await seed(vaultRoot, 8);
    const r1 = await list_concepts(vaultRoot, { limit: 5 });
    if (!("concepts" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");
    const r2 = await list_concepts(vaultRoot, { limit: 5, cursor: r1.cursor });
    if (!("concepts" in r2)) throw new Error("expected concepts");
    expect(r2.concepts.length).toBe(3);
    expect(r2.cursor).toBeUndefined();
  });

  it("clamps limit at 200 (cap from spec)", async () => {
    await seed(vaultRoot, 250);
    const r = await list_concepts(vaultRoot, { limit: 9999 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBeLessThanOrEqual(200);
    // Explicit timeout: this is the largest seed in the file (250 rows) and
    // flaked against jest's 5s default on the loaded CI runner.
  }, 30000);

  it("collapses limit: 0 to default 50", async () => {
    await seed(vaultRoot, 60);
    const r = await list_concepts(vaultRoot, { limit: 0 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(50);
    expect(r.cursor).toBeDefined();
  });

  it("collapses negative limit to default 50", async () => {
    await seed(vaultRoot, 60);
    const r = await list_concepts(vaultRoot, { limit: -1 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(50);
    expect(r.cursor).toBeDefined();
  });

  it("rejects non-numeric limit (string) — validateLimit defense (#108)", async () => {
    await seed(vaultRoot, 60);
    // Caller bypassed the JSON-schema layer and passed a string. Pre-#108
    // this flowed into Math.min with implicit coercion and corrupted the
    // offset math; post-#108 validateLimit treats non-number as not-a-number
    // and falls back to default 50.
    const r = await list_concepts(vaultRoot, { limit: "50" as unknown as number });
    if (!("concepts" in r)) throw new Error(`unexpected error: ${JSON.stringify(r)}`);
    expect(r.concepts.length).toBe(50);
  });

  it("truncates fractional limit (Math.trunc) — validateLimit (#108)", async () => {
    await seed(vaultRoot, 60);
    const r = await list_concepts(vaultRoot, { limit: 12.7 });
    if (!("concepts" in r)) throw new Error("expected concepts");
    // 12.7 → truncated to 12 (NOT rounded to 13)
    expect(r.concepts.length).toBe(12);
  });

  it("rejects NaN at canonicalize (before validateLimit) — INVALID_ARG (#108)", async () => {
    await seed(vaultRoot, 60);
    // NaN is caught by canonicalizeQueryHash BEFORE reaching validateLimit;
    // the handler returns INVALID_ARG, not a fallback default. validateLimit
    // is defense-in-depth — the canonicalize gate is the first line.
    const r = await list_concepts(vaultRoot, { limit: NaN });
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });

  it("returns empty concepts + no cursor when no rows match", async () => {
    // Empty vault — no seed call.
    const r = await list_concepts(vaultRoot, {});
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts).toEqual([]);
    expect(r.cursor).toBeUndefined();
  });
});

// ── tiebreaker stability ───────────────────────────────────────────────────

describe("list_concepts tool — slug-ASC tiebreaker stability", () => {
  // All seeded concepts have edgeCount=0, so the ORDER BY edgeCount DESC, slug ASC
  // ordering relies entirely on the slug-ASC tiebreaker to produce stable pagination.
  it("paginates 6 equal-edgeCount concepts exactly once across pages (no duplicates, no skips), in slug ASC order", async () => {
    await seed(vaultRoot, 6);
    const seen: string[] = [];
    let cursor: string | undefined;
    let page = 0;
    while (page < 10) {
      const r = await list_concepts(vaultRoot, { limit: 3, cursor });
      if (!("concepts" in r)) throw new Error(`unexpected error on page ${page}: ${JSON.stringify(r)}`);
      for (const c of r.concepts) {
        expect(seen.includes(c.slug)).toBe(false);
        seen.push(c.slug);
      }
      cursor = r.cursor;
      page++;
      if (cursor === undefined) break;
    }
    expect(seen.length).toBe(6);
    // Tiebreaker order is c.slug ASC — slugs are concept-000..concept-005.
    expect(seen).toEqual([
      "concept-000", "concept-001", "concept-002",
      "concept-003", "concept-004", "concept-005",
    ]);
  });
});

// ── concurrent-ingest staleness (#90 / #246) ───────────────────────────────

describe("list_concepts tool — cursor staleness on rebuild (#246)", () => {
  // PR #241 threaded vaultGeneration through list_concepts alongside
  // search_notes; only search_notes had regression coverage. Mirrors
  // tests/search-notes-tool.test.ts's canonical model.
  function gitInitCommit(dir: string, message: string): void {
    execFileSync("git", ["init"], { cwd: dir, stdio: "ignore" });
    execFileSync("git", ["config", "user.email", "test@example.com"], { cwd: dir });
    execFileSync("git", ["config", "user.name", "Test User"], { cwd: dir });
    execFileSync("git", ["commit", "--allow-empty", "-m", message], { cwd: dir, stdio: "ignore" });
  }

  it("returns CURSOR_STALE when vault is rebuilt between pages", async () => {
    await seed(vaultRoot, 10);
    gitInitCommit(vaultRoot, "initial");

    const r1 = await list_concepts(vaultRoot, { limit: 3 });
    if (!("concepts" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");

    // A commit rebuilds the vault index (HEAD moves), reordering OFFSET rows.
    execFileSync("git", ["commit", "--allow-empty", "-m", "concurrent ingest"], {
      cwd: vaultRoot,
      stdio: "ignore",
    });

    const r2 = await list_concepts(vaultRoot, { limit: 3, cursor: r1.cursor });
    expect(r2).toMatchObject({ error: "CURSOR_STALE" });
  });

  it("accepts cursor when HEAD unchanged between pages", async () => {
    await seed(vaultRoot, 10);
    gitInitCommit(vaultRoot, "initial");

    const r1 = await list_concepts(vaultRoot, { limit: 3 });
    if (!("concepts" in r1) || !r1.cursor) throw new Error("expected page-1 cursor");

    const r2 = await list_concepts(vaultRoot, { limit: 3, cursor: r1.cursor });
    if (!("concepts" in r2)) throw new Error(`expected concepts, got ${JSON.stringify(r2)}`);
    expect(r2.concepts.length).toBe(3);
    const slugs1 = new Set(r1.concepts.map((c) => c.slug));
    for (const c of r2.concepts) expect(slugs1.has(c.slug)).toBe(false);
  });
});

// ── filters: tags, search ──────────────────────────────────────────────────

describe("list_concepts tool — filters", () => {
  it("filters by tags — only concepts whose tags JSON contains the tag are returned", async () => {
    // Seed 3 concepts tagged ["neural"], plus 2 untagged.
    await seed(vaultRoot, 3, { tags: ["neural"] });
    const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
    try {
      const stmt = db.prepare(
        `INSERT INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, NULL)`,
      );
      stmt.run("other-001", "Other 001", "no tags");
      stmt.run("other-002", "Other 002", "no tags");
    } finally {
      db.close();
    }

    const r = await list_concepts(vaultRoot, { tags: ["neural"] });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(3);
    for (const c of r.concepts) {
      expect(c.slug.startsWith("concept-")).toBe(true);
    }
  });

  it("filters by search string against title/description LIKE", async () => {
    // Seed 3 concepts with description "foo description ..." plus 2 with default body.
    await seed(vaultRoot, 3, { description: "foo bar baz" });
    const db = new Database(path.join(vaultRoot, ".schist", "schist.db"));
    try {
      const stmt = db.prepare(
        `INSERT INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, NULL)`,
      );
      stmt.run("zzz-001", "Other 001", "unrelated content here");
      stmt.run("zzz-002", "Other 002", "more unrelated content");
    } finally {
      db.close();
    }

    const r = await list_concepts(vaultRoot, { search: "foo" });
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(3);
    for (const c of r.concepts) {
      expect(c.slug.startsWith("concept-")).toBe(true);
    }
  });
});

// ── empty owner ────────────────────────────────────────────────────────────

describe("list_concepts tool — empty owner", () => {
  it("returns a valid response when both SCHIST_AGENT_NAME and SCHIST_AGENT_ID are unset (owner='')", async () => {
    // beforeEach already deletes both env vars. Reassert here for clarity.
    delete process.env.SCHIST_AGENT_NAME;
    delete process.env.SCHIST_AGENT_ID;
    await seed(vaultRoot, 3);
    const r = await list_concepts(vaultRoot, {});
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(r.concepts.length).toBe(3);
    expect(r.cursor).toBeUndefined();
  });
});

// ── normalizeError fallthrough ─────────────────────────────────────────────

describe("list_concepts tool — normalizeError fallthrough", () => {
  it("returns INGEST_ERROR when sqliteReader.listConcepts throws (corrupt DB)", async () => {
    // Corrupt the SQLite file: drop the concepts table so listConcepts'
    // SELECT against c.slug throws a SqliteError. The handler should catch
    // and normalize this into a ToolError with error: "INGEST_ERROR".
    const dbPath = path.join(vaultRoot, ".schist", "schist.db");
    const db = new Database(dbPath);
    db.exec("DROP TABLE concepts;");
    db.close();

    const r = await list_concepts(vaultRoot, {});
    expect(r).toHaveProperty("error", "INGEST_ERROR");
    expect(r).toHaveProperty("message");
    if ("message" in r) {
      expect(typeof r.message).toBe("string");
      expect(r.message.length).toBeGreaterThan(0);
    }
  });
});

// ── response shape ─────────────────────────────────────────────────────────

describe("list_concepts tool — response shape", () => {
  it("returns { concepts: Concept[] } (not a bare array)", async () => {
    await seed(vaultRoot, 2);
    const r = await list_concepts(vaultRoot, {});
    expect(Array.isArray(r)).toBe(false);
    if (!("concepts" in r)) throw new Error("expected concepts");
    expect(Array.isArray(r.concepts)).toBe(true);
    // Concept shape: slug, title, description, tags, edgeCount.
    for (const c of r.concepts) {
      expect(typeof c.slug).toBe("string");
      expect(typeof c.title).toBe("string");
      expect(typeof c.description).toBe("string");
      expect(Array.isArray(c.tags)).toBe(true);
      expect(typeof c.edgeCount).toBe("number");
    }
  });
});

// ── JSDoc smoke ────────────────────────────────────────────────────────────

describe("list_concepts tool — JSDoc smoke", () => {
  it("source file contains the handler's spec doc reference", async () => {
    const src = await fs.readFile(
      path.join(__dirname, "..", "src", "tools.ts"),
      "utf-8",
    );
    // Find the list_concepts function definition. The doc block lives in
    // the preceding lines — assert both the function header and the Spec:
    // reference appear nearby.
    const fnIdx = src.indexOf("export async function list_concepts(");
    expect(fnIdx).toBeGreaterThan(0);
    // Look backwards ~1500 chars for the JSDoc.
    const head = src.slice(Math.max(0, fnIdx - 1500), fnIdx);
    expect(head).toMatch(/list_concepts tool handler/);
    expect(head).toMatch(/Spec:\s*docs\/superpowers\/specs\//);
  });
});
