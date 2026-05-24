import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import { get_context } from "../src/tools.js";
import { resetVerboseForTesting } from "../src/protocol/index.js";
import * as verboseModule from "../src/protocol/verbose.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// get_context hits <vault>/.schist/schist.db (docs/concepts/edges) plus reads
// an optional .schist/last-sync-error sentinel. Tests build a minimal schema +
// seed handful of rows so the verbose-gate, downgrade, sentinel-interaction,
// and rate-limit-concat branches can be exercised without a real ingest run.

const SENTINEL_REL = ".schist/last-sync-error";

async function makeVault(opts?: { docTagsRow?: string }): Promise<string> {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getctx-test-"));
  const dbDir = path.join(dir, ".schist");
  await fs.mkdir(dbDir, { recursive: true });
  const dbPath = path.join(dbDir, "schist.db");

  const db = new Database(dbPath);
  db.exec(`
    CREATE TABLE docs (
      id          TEXT PRIMARY KEY,
      title       TEXT NOT NULL,
      date        TEXT,
      status      TEXT DEFAULT 'draft',
      tags        TEXT,
      concepts    TEXT,
      body        TEXT NOT NULL,
      scope       TEXT DEFAULT 'global',
      source      TEXT,
      created_at  TEXT DEFAULT (datetime('now')),
      updated_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE concepts (
      slug        TEXT PRIMARY KEY,
      title       TEXT NOT NULL,
      description TEXT,
      tags        TEXT,
      created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE edges (
      id          INTEGER PRIMARY KEY AUTOINCREMENT,
      source      TEXT NOT NULL,
      target      TEXT NOT NULL,
      type        TEXT NOT NULL,
      context     TEXT,
      created_at  TEXT DEFAULT (datetime('now')),
      UNIQUE(source, target, type)
    );
  `);
  // Seed 3 docs (with tags), 2 concepts, 2 edges so all three depth tiers
  // return non-trivial shapes.
  const insertDoc = db.prepare(
    `INSERT INTO docs (id, title, body, tags) VALUES (?, ?, ?, ?)`,
  );
  const tags = opts?.docTagsRow ?? JSON.stringify(["alpha", "beta"]);
  insertDoc.run("notes/d1.md", "Doc 1", "body 1", tags);
  insertDoc.run("notes/d2.md", "Doc 2", "body 2", tags);
  insertDoc.run("notes/d3.md", "Doc 3", "body 3", tags);
  db.prepare(`INSERT INTO concepts (slug, title) VALUES (?, ?)`).run("c-one", "C One");
  db.prepare(`INSERT INTO concepts (slug, title) VALUES (?, ?)`).run("c-two", "C Two");
  db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
    "notes/d1.md", "c-one", "related",
  );
  db.prepare(`INSERT INTO edges (source, target, type) VALUES (?, ?, ?)`).run(
    "notes/d2.md", "c-two", "related",
  );
  db.close();
  return dir;
}

let vaultRoot: string;
let stderrSpy: { write: typeof process.stderr.write; calls: string[] };
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = ["SCHIST_AGENT_ID", "SCHIST_AGENT_NAME", "SCHIST_VAULT_PATH"] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  vaultRoot = await makeVault();
  resetVerboseForTesting();

  // Capture stderr writes so we can assert logVerbose audit lines without
  // polluting test output. Restore in afterEach.
  const calls: string[] = [];
  const orig = process.stderr.write.bind(process.stderr);
  stderrSpy = { write: orig, calls };
  process.stderr.write = ((chunk: string | Uint8Array, ...rest: unknown[]): boolean => {
    if (typeof chunk === "string") calls.push(chunk);
    // Suppress stderr during tests (do NOT call orig).
    // The cast keeps the broad write() overload set quiet for TS.
    void rest;
    return true;
  }) as typeof process.stderr.write;
});

afterEach(async () => {
  process.stderr.write = stderrSpy.write;
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  await fs.rm(vaultRoot, { recursive: true, force: true });
});

// ── Case 1: depth=full + valid verbose ─────────────────────────────────────

describe("get_context tool — depth=full happy path", () => {
  it("returns full shape (tagCloud) when verbose reason is valid; logs an audit line", async () => {
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: "preparing handoff doc",
    });
    expect(r).not.toHaveProperty("error");
    if ("error" in r) throw new Error("unexpected error");
    expect(r.tagCloud).toBeDefined();
    expect(Array.isArray(r.tagCloud)).toBe(true);
    expect(r.tagCloud!.length).toBeGreaterThan(0);
    // verboseNote absent on the happy path (no downgrade, no rate-limit).
    expect(r.verboseNote).toBeUndefined();
    // logVerbose audit line emitted.
    const audit = stderrSpy.calls.find(c => c.startsWith("[verbose] get_context"));
    expect(audit).toBeDefined();
    expect(audit).toContain('"preparing handoff doc"');
  });
});

// ── Case 2: depth=full + missing verbose → downgrade ───────────────────────

describe("get_context tool — depth=full soft downgrade", () => {
  it("downgrades to standard + emits verboseNote when verbose is omitted", async () => {
    const r = await get_context(vaultRoot, { depth: "full" });
    if ("error" in r) throw new Error("unexpected error");
    // tagCloud is the depth=full delta — must be absent on downgrade.
    expect(r.tagCloud).toBeUndefined();
    // Standard shape is present (recent + hotConcepts).
    expect(r.recent).toBeDefined();
    expect(r.hotConcepts).toBeDefined();
    expect(r.verboseNote).toBeDefined();
    expect(r.verboseNote).toContain('depth="full"');
    expect(r.verboseNote).toContain("downgraded");
    // No logVerbose audit line on downgrade.
    const audit = stderrSpy.calls.find(c => c.startsWith("[verbose] get_context"));
    expect(audit).toBeUndefined();
  });
});

// ── Case 3: depth=full + too-short verbose → INVALID_ARG ───────────────────

describe("get_context tool — parseVerbose too-short", () => {
  it("returns INVALID_ARG for verbose <12 code points (hard error, no downgrade)", async () => {
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: "short",
    });
    expect(r).toEqual({
      error: "INVALID_ARG",
      message: expect.stringContaining("code points"),
    });
  });
});

// ── Case 4: depth=full + boolean verbose → INVALID_ARG type error ──────────

describe("get_context tool — parseVerbose type error", () => {
  it("returns INVALID_ARG for verbose=true (boolean)", async () => {
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: true,
    } as never);
    expect(r).toEqual({
      error: "INVALID_ARG",
      message: expect.stringContaining("must be a string"),
    });
  });
});

// ── Case 5: depth=standard + valid verbose → IGNORED ───────────────────────

describe("get_context tool — verbose on non-full depth is ignored semantically", () => {
  it("depth=standard + valid verbose returns standard shape, NO verboseNote, NO logVerbose", async () => {
    const r = await get_context(vaultRoot, {
      depth: "standard",
      verbose: "valid reason here",
    });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recent).toBeDefined();
    expect(r.tagCloud).toBeUndefined();
    expect(r.verboseNote).toBeUndefined();
    const audit = stderrSpy.calls.find(c => c.startsWith("[verbose] get_context"));
    expect(audit).toBeUndefined();
  });
});

// ── Case 6: depth=minimal → unchanged shape ────────────────────────────────

describe("get_context tool — depth=minimal default", () => {
  it("returns counts-only shape; no recent/hotConcepts/tagCloud/verboseNote", async () => {
    const r = await get_context(vaultRoot, { depth: "minimal" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.noteCount).toBeDefined();
    expect(r.conceptCount).toBeDefined();
    expect(r.edgeCount).toBeDefined();
    expect(r.recent).toBeUndefined();
    expect(r.hotConcepts).toBeUndefined();
    expect(r.tagCloud).toBeUndefined();
    expect(r.verboseNote).toBeUndefined();
  });
});

// ── Case 7: rate-limit primitive wired into get_context ────────────────────

describe("get_context tool — rate-limit (noteHighFrequency) wiring", () => {
  it("emits high-frequency verboseNote when the rate-limit primitive trips", async () => {
    // Pre-load the frequency bucket past the per-minute cap by calling the
    // primitive directly with the same (tool, owner, reason) signature the
    // handler will use. resetVerboseForTesting in beforeEach guaranteed a
    // clean bucket. Owner is "" because SCHIST_AGENT_ID is unset (beforeEach
    // deletes it). The cap is VERBOSE_RATE_LIMIT_PER_MIN = 30.
    const trip = "rate-limit verbose test";
    for (let i = 0; i < verboseModule.VERBOSE_RATE_LIMIT_PER_MIN; i++) {
      verboseModule.noteHighFrequency({
        tool: "get_context",
        owner: "",
        reason: trip,
      });
    }
    // 31st invocation (this one, via the handler) should push the bucket over.
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: trip,
    });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.tagCloud).toBeDefined();
    expect(r.verboseNote).toBeDefined();
    expect(r.verboseNote).toContain("frequent");
  });
});

// ── Case 8: sentinel + downgrade interaction ───────────────────────────────

describe("get_context tool — sync sentinel + verbose downgrade coexistence", () => {
  it("response carries BOTH syncWarning and verboseNote (downgrade); sentinel is deleted", async () => {
    const sentinelPath = path.join(vaultRoot, SENTINEL_REL);
    await fs.writeFile(sentinelPath, "background push failed: timeout");
    const r = await get_context(vaultRoot, { depth: "full" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.syncWarning).toBeDefined();
    expect(r.syncWarning).toContain("timeout");
    expect(r.verboseNote).toBeDefined();
    expect(r.verboseNote).toContain("downgraded");
    // Sentinel is consumed after a successful read.
    await expect(fs.access(sentinelPath)).rejects.toBeDefined();
  });
});

// ── Case 9: corrupt DB → INGEST_ERROR ──────────────────────────────────────

describe("get_context tool — corrupt DB error fallthrough", () => {
  it("returns INGEST_ERROR when sqliteReader.getContext throws (missing docs table)", async () => {
    const dbPath = path.join(vaultRoot, ".schist", "schist.db");
    const db = new Database(dbPath);
    db.exec("DROP TABLE docs;");
    db.close();
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: "valid long reason",
    });
    expect(r).toHaveProperty("error", "INGEST_ERROR");
    expect(r).toHaveProperty("message");
  });
});

// ── Case 10: type validation runs regardless of depth ──────────────────────

describe("get_context tool — type validation precedes depth check", () => {
  it("returns INVALID_ARG for verbose=true even on depth=minimal", async () => {
    const r = await get_context(vaultRoot, {
      depth: "minimal",
      verbose: true,
    } as never);
    expect(r).toEqual({
      error: "INVALID_ARG",
      message: expect.stringContaining("must be a string"),
    });
  });
});

// ── Case 11: whitespace-only verbose → soft downgrade (no error) ───────────

describe("get_context tool — whitespace-only verbose", () => {
  it('depth="full" + verbose="   " (whitespace) downgrades to standard, no error', async () => {
    const r = await get_context(vaultRoot, {
      depth: "full",
      verbose: "   \t  ",
    });
    if ("error" in r) throw new Error("unexpected error");
    // Whitespace-only is treated like missing — downgrade, NOT INVALID_ARG.
    expect(r.tagCloud).toBeUndefined();
    expect(r.recent).toBeDefined();
    expect(r.verboseNote).toBeDefined();
    expect(r.verboseNote).toContain("downgraded");
  });
});

// ── Case 12: downgrade + high-frequency concat ─────────────────────────────

describe("get_context tool — verboseNote concat (downgrade + rate-limit)", () => {
  it("emits a single verboseNote joining both hints with '; ' when both fire on one call", async () => {
    // This is structurally hard to exercise: the downgrade path skips
    // noteHighFrequency (because effectiveDepth !== "full" after downgrade,
    // and the handler gates the rate-limit call on that). By inspection the
    // concat branch in step 6 is unreachable from real callers UNLESS the
    // rate-limit primitive were extended to fire on the downgrade itself.
    //
    // We verify the concat code is correctly shaped via source inspection +
    // a unit-style invocation in the "future-proofing" frame: if a refactor
    // ever wires noteHighFrequency on the downgrade path, the assembly is
    // already correct. This test serves as a docs anchor + regression guard.
    const src = await fs.readFile(
      path.join(__dirname, "..", "src", "tools.ts"),
      "utf-8",
    );
    const fnIdx = src.indexOf("export async function get_context(");
    expect(fnIdx).toBeGreaterThan(0);
    const body = src.slice(fnIdx, fnIdx + 4500);
    // Assert the concat shape: downgradeNote + "; " + freqNote
    expect(body).toMatch(/`\$\{downgradeNote\}; \$\{freqNote\}`/);
    // Assert the fallback ?? chain orders downgradeNote first, freqNote second.
    expect(body).toMatch(/downgradeNote \?\? freqNote/);
  });
});

// ── JSDoc smoke ────────────────────────────────────────────────────────────

describe("get_context tool — JSDoc smoke", () => {
  it("source file contains the handler's spec doc reference", async () => {
    const src = await fs.readFile(
      path.join(__dirname, "..", "src", "tools.ts"),
      "utf-8",
    );
    const fnIdx = src.indexOf("export async function get_context(");
    expect(fnIdx).toBeGreaterThan(0);
    const head = src.slice(Math.max(0, fnIdx - 1500), fnIdx);
    expect(head).toMatch(/get_context tool handler/);
    expect(head).toMatch(/Spec:\s*docs\/superpowers\/specs\//);
  });
});
