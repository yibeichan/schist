import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "url";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import { get_context } from "../src/tools.js";
import { addMemory } from "../src/sqlite-reader.js";
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
    `INSERT INTO docs (id, title, date, body, tags, updated_at) VALUES (?, ?, ?, ?, ?, ?)`,
  );
  const tags = opts?.docTagsRow ?? JSON.stringify(["alpha", "beta"]);
  const tiedUpdatedAt = "2026-01-10 00:00:00";
  insertDoc.run("notes/d1.md", "Doc 1", "2026-01-01", "body 1", tags, tiedUpdatedAt);
  insertDoc.run("notes/d2.md", "Doc 2", "2026-01-03", "body 2", tags, tiedUpdatedAt);
  insertDoc.run("notes/d3.md", "Doc 3", "2026-01-02", "body 3", tags, tiedUpdatedAt);
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
let memDir: string;
let memDbPath: string;
let stderrSpy: { write: typeof process.stderr.write; calls: string[] };
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = [
  "SCHIST_AGENT_ID", "SCHIST_AGENT_NAME", "SCHIST_VAULT_PATH",
  "SCHIST_ALLOWED_AGENTS", "SCHIST_MEMORY_DB",
] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  vaultRoot = await makeVault();
  // Point the memory DB at a per-test temp path that does NOT exist yet:
  // recentMemory tests seed it explicitly; every other test exercises the
  // "no memory DB file" degradation path (and never the real ~/.openclaw DB).
  memDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getctx-mem-"));
  memDbPath = path.join(memDir, "agent-state.db");
  process.env.SCHIST_MEMORY_DB = memDbPath;
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
  await fs.rm(memDir, { recursive: true, force: true });
});

// Seed agent_memory rows for `owner` through the real write path (addMemory
// creates the DB + schema). Env juggling because addMemory validates owner
// against SCHIST_AGENT_ID.
function seedMemory(
  owner: string,
  entries: Array<{ content: string; entry_type?: string; related_doc?: string; date?: string }>,
): void {
  const prevId = process.env.SCHIST_AGENT_ID;
  const prevAllowed = process.env.SCHIST_ALLOWED_AGENTS;
  delete process.env.SCHIST_ALLOWED_AGENTS;
  process.env.SCHIST_AGENT_ID = owner;
  try {
    for (const e of entries) {
      addMemory({
        owner,
        entry_type: e.entry_type ?? "decision",
        content: e.content,
        related_doc: e.related_doc,
        date: e.date,
      });
    }
  } finally {
    if (prevId === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prevId;
    if (prevAllowed === undefined) delete process.env.SCHIST_ALLOWED_AGENTS;
    else process.env.SCHIST_ALLOWED_AGENTS = prevAllowed;
  }
}

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

describe("get_context tool — recent docs ordering", () => {
  it("orders recent docs by frontmatter date when updated_at ties", async () => {
    const r = await get_context(vaultRoot, { depth: "standard" });
    expect(r).not.toHaveProperty("error");
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recent?.map((doc) => doc.id)).toEqual([
      "notes/d2.md",
      "notes/d3.md",
      "notes/d1.md",
    ]);
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
  it("response carries BOTH syncWarning and verboseNote (downgrade); sentinel remains", async () => {
    const sentinelPath = path.join(vaultRoot, SENTINEL_REL);
    await fs.writeFile(sentinelPath, "background push failed: timeout");
    const r = await get_context(vaultRoot, { depth: "full" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.syncWarning).toBeDefined();
    expect(r.syncWarning).toContain("timeout");
    expect(r.verboseNote).toBeDefined();
    expect(r.verboseNote).toContain("downgraded");
    // A context read is not proof that local commits reached the hub.
    await expect(fs.access(sentinelPath)).resolves.toBeUndefined();
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
    // Slice to the next top-level export so the window tracks the handler's
    // actual extent instead of a fixed offset that needs manual re-widening
    // every time the function grows.
    const nextExportIdx = src.indexOf("\nexport ", fnIdx + 1);
    const body = src.slice(fnIdx, nextExportIdx === -1 ? undefined : nextExportIdx);
    // Assert the concat shape: downgradeNote + "; " + freqNote
    expect(body).toMatch(/`\$\{downgradeNote\}; \$\{freqNote\}`/);
    // Assert the fallback ?? chain orders downgradeNote first, freqNote second.
    expect(body).toMatch(/downgradeNote \?\? freqNote/);
  });
});

// ── recentMemory block (slice C, docs/data-model.md D4) ────────────────────

describe("get_context tool — recentMemory happy path", () => {
  it("appends the owner's 5 most recent entries at depth=standard, newest first, owner-scoped", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    seedMemory("sansan", [
      { content: "entry one" }, { content: "entry two" }, { content: "entry three" },
      { content: "entry four" }, { content: "entry five" }, { content: "entry six" },
      { content: "entry seven" },
    ]);
    seedMemory("ninjia", [{ content: "foreign entry must not leak" }]);

    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeDefined();
    expect(r.recentMemory!.owner).toBe("sansan");
    const entries = r.recentMemory!.entries;
    expect(entries.length).toBe(5);
    // Newest first (all rows share a created_at second → id DESC tiebreak).
    expect(entries.map(e => e.content)).toEqual([
      "entry seven", "entry six", "entry five", "entry four", "entry three",
    ]);
    // Owner-scoped: ninjia's row never appears.
    expect(entries.some(e => e.content.includes("foreign"))).toBe(false);
    // Row shape is the stable /pickup contract: id, date, entry_type, content.
    for (const e of entries) {
      expect(typeof e.id).toBe("number");
      expect(typeof e.date).toBe("string");
      expect(e.entry_type).toBe("decision");
    }
  });

  it("truncates content to 100 code points with ellipsis and carries related_doc", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    seedMemory("sansan", [
      { content: "x".repeat(150), related_doc: "notes/fuel-station.md" },
      { content: "short one, no back-reference" },
    ]);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    const entries = r.recentMemory!.entries;
    expect(entries.length).toBe(2);
    const [short, long] = entries; // newest first
    // Exact key sets (toEqual fails on extra keys): widening the row shape —
    // e.g. an accidental SELECT * leaking owner/created_at/tags — must be a
    // conscious decision that updates this pin, not a silent drive-by.
    expect(short).toEqual({
      id: expect.any(Number),
      date: expect.any(String),
      entry_type: "decision",
      content: "short one, no back-reference",
    });
    expect(long).toEqual({
      id: expect.any(Number),
      date: expect.any(String),
      entry_type: "decision",
      content: "x".repeat(100) + "…",
      related_doc: "notes/fuel-station.md",
    });
  });

  it("keeps an oversized content row process-safe: SQL-side bound + 100-cp snippet", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    // Large enough to prove the row is bounded before snippetContent spreads
    // it into a code-point array, small enough to keep the test fast. The
    // SQL-side substr() in getRecentMemory is what keeps a hostile
    // multi-hundred-MB row from ever being loaded into process memory.
    seedMemory("sansan", [{ content: "y".repeat(500_000) }]);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    const entries = r.recentMemory!.entries;
    expect(entries.length).toBe(1);
    expect(entries[0].content).toBe("y".repeat(100) + "…");
  });

  it("omits related_doc values that fail the note-id shape rule at read time (untrusted rows)", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    // Seed via the sqlite-reader layer, which (deliberately) does not shape-
    // validate — mimicking legacy rows written before add_memory's validation
    // and rows planted by other software in the shared per-machine DB.
    seedMemory("sansan", [
      { content: "junk empty", related_doc: "" },
      { content: "junk dotfile", related_doc: ".git/config" },
      { content: "junk traversal", related_doc: "../x.md" },
      { content: "good ref", related_doc: "notes/legit.md" },
    ]);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    const entries = r.recentMemory!.entries;
    expect(entries.length).toBe(4);
    for (const entry of entries) {
      if (entry.content === "good ref") {
        expect(entry.related_doc).toBe("notes/legit.md");
      } else {
        // Key omitted entirely — a present-but-junk key would still read as
        // "this entry has a back-reference" to consumers.
        expect(entry).not.toHaveProperty("related_doc");
      }
    }
  });

  it("appears at depth=full too, and stays absent at depth=minimal", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    seedMemory("sansan", [{ content: "full-depth entry" }]);
    const full = await get_context(vaultRoot, { depth: "full", verbose: "checking full depth" });
    if ("error" in full) throw new Error("unexpected error");
    expect(full.recentMemory).toBeDefined();
    expect(full.tagCloud).toBeDefined();

    const minimal = await get_context(vaultRoot, { depth: "minimal" });
    if ("error" in minimal) throw new Error("unexpected error");
    expect(minimal.recentMemory).toBeUndefined();
    expect(minimal.noteCount).toBeDefined();
  });

  it("returns a present block with entries: [] when memory is reachable but empty for the owner", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    // Table exists (another owner's write created it) but sansan has no rows:
    // "memory works, nothing recorded" must be distinct from "unavailable".
    seedMemory("ninjia", [{ content: "someone else's entry" }]);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toEqual({ owner: "sansan", entries: [] });
  });
});

describe("get_context tool — recentMemory degradation (absent block, never an error)", () => {
  it("no memory DB file → block absent, vault context intact, and the file is NOT created", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
    expect(r.hotConcepts).toBeDefined();
    // A context read must not scaffold the memory DB (readonly open).
    await expect(fs.access(memDbPath)).rejects.toThrow();
  });

  // chmod 000 only blocks non-root users — root (e.g. a Docker CI runner)
  // reads it anyway and the test would FAIL rather than exercise the
  // degradation path, so skip under uid 0.
  const itUnlessRoot = process.getuid?.() === 0 ? it.skip : it;
  itUnlessRoot("unreadable memory DB file → block absent, no error", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    seedMemory("sansan", [{ content: "soon unreadable" }]);
    await fs.chmod(memDbPath, 0o000);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
  });

  it("memory DB path holds a non-SQLite file → block absent, no error", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    await fs.writeFile(memDbPath, "this is not a sqlite database at all");
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
  });

  it("memory DB exists but lacks the agent_memory table → block absent, no error", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const db = new Database(memDbPath);
    db.exec("CREATE TABLE unrelated (id INTEGER PRIMARY KEY);");
    db.close();
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
  });

  it("no resolvable owner (no arg, no SCHIST_AGENT_ID) → block absent even with memory seeded", async () => {
    seedMemory("sansan", [{ content: "orphaned by missing identity" }]);
    // beforeEach cleared SCHIST_AGENT_ID / SCHIST_AGENT_NAME.
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
  });

  it("env fallback identity outside SCHIST_ALLOWED_AGENTS → block absent, no error (no allowlist bypass)", async () => {
    // Asymmetric-gating guard: with SCHIST_ALLOWED_AGENTS=alpha,beta and
    // SCHIST_AGENT_ID=gamma, add_memory refuses to write as gamma and an
    // explicit owner:"gamma" arg is rejected — so the implicit env fallback
    // must not quietly serve gamma's memory either. It degrades to an absent
    // block (never an error: it is ambient config, not a caller assertion).
    seedMemory("gamma", [{ content: "written before the allowlist was tightened" }]);
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    process.env.SCHIST_AGENT_ID = "gamma";
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
    expect(r.recent).toBeDefined();
    expect(r.hotConcepts).toBeDefined();
  });
});

describe("get_context tool — recentMemory owner resolution (multi-owner servers)", () => {
  it("accepts an allowlisted owner arg and scopes the block to it", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    seedMemory("alpha", [{ content: "alpha entry" }]);
    seedMemory("beta", [{ content: "beta entry" }]);
    const r = await get_context(vaultRoot, { depth: "standard", owner: "beta" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory!.owner).toBe("beta");
    expect(r.recentMemory!.entries.map(e => e.content)).toEqual(["beta entry"]);
  });

  it("rejects an owner arg not in SCHIST_ALLOWED_AGENTS with VALIDATION_ERROR", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    const r = await get_context(vaultRoot, { depth: "standard", owner: "mallory" });
    expect(r).toMatchObject({
      error: "VALIDATION_ERROR",
      message: expect.stringContaining("SCHIST_ALLOWED_AGENTS"),
    });
  });

  it("rejects an owner arg that mismatches SCHIST_AGENT_ID in single-agent mode", async () => {
    process.env.SCHIST_AGENT_ID = "alpha";
    const r = await get_context(vaultRoot, { depth: "standard", owner: "beta" });
    expect(r).toMatchObject({
      error: "VALIDATION_ERROR",
      message: expect.stringContaining("SCHIST_AGENT_ID"),
    });
  });

  it("validates an explicit owner even at depth=minimal (validate-first parity)", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    const r = await get_context(vaultRoot, { depth: "minimal", owner: "mallory" });
    expect(r).toMatchObject({ error: "VALIDATION_ERROR" });
  });

  it("allowlist-only deployment with owner omitted → block absent (no env fallback identity)", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    seedMemory("alpha", [{ content: "needs an explicit owner to surface" }]);
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory).toBeUndefined();
  });

  it("env fallback identity INSIDE the allowlist still surfaces the block (validation must not over-degrade)", async () => {
    seedMemory("alpha", [{ content: "allowlisted env identity" }]);
    process.env.SCHIST_ALLOWED_AGENTS = "alpha,beta";
    process.env.SCHIST_AGENT_ID = "alpha";
    const r = await get_context(vaultRoot, { depth: "standard" });
    if ("error" in r) throw new Error("unexpected error");
    expect(r.recentMemory!.owner).toBe("alpha");
    expect(r.recentMemory!.entries.map(e => e.content)).toEqual(["allowlisted env identity"]);
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
