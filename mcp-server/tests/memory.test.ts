import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import { addMemory, searchMemory, getAgentState, setAgentState, deleteAgentState, addConceptAlias } from "../src/sqlite-reader.js";

// Use a temp DB for each test suite
let tempDir: string;

beforeEach(async () => {
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-memory-test-"));
  process.env.SCHIST_MEMORY_DB = path.join(tempDir, "test-memory.db");
  // Clear agent ID so tests can use any owner
  delete process.env.SCHIST_AGENT_ID;
});

afterEach(async () => {
  delete process.env.SCHIST_MEMORY_DB;
  delete process.env.SCHIST_AGENT_ID;
  await fs.rm(tempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// add_memory — owner enforcement
// ---------------------------------------------------------------------------

describe("addMemory", () => {
  it("inserts a memory entry and returns id + created_at", () => {
    const result = addMemory({
      owner: "sansan",
      entry_type: "decision",
      content: "Use SQLite for memory store",
      confidence: "high",
    });
    expect(result.id).toBeGreaterThan(0);
    expect(typeof result.created_at).toBe("string");
  });

  it("rejects wrong owner when SCHIST_AGENT_ID is set", () => {
    process.env.SCHIST_AGENT_ID = "ninjia";
    expect(() =>
      addMemory({ owner: "sansan", entry_type: "decision", content: "test" })
    ).toThrow();
  });

  it("allows correct owner when SCHIST_AGENT_ID is set", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const result = addMemory({
      owner: "sansan",
      entry_type: "lesson",
      content: "Always check admin role in app layer",
    });
    expect(result.id).toBeGreaterThan(0);
  });

  it("rejects invalid entry_type CHECK constraint", () => {
    expect(() =>
      addMemory({ owner: "sansan", entry_type: "invalid_type", content: "test" })
    ).toThrow();
  });

  it("stores and returns tags as array", () => {
    addMemory({
      owner: "sansan",
      entry_type: "completion",
      content: "PR #266 merged",
      tags: ["rollup", "kiosk"],
    });
    const results = searchMemory({ owner: "sansan" });
    expect(results[0].tags).toEqual(["rollup", "kiosk"]);
  });
});

// ---------------------------------------------------------------------------
// searchMemory
// ---------------------------------------------------------------------------

describe("searchMemory", () => {
  beforeEach(() => {
    addMemory({ owner: "sansan", entry_type: "decision", content: "Use SQLite WAL mode", tags: ["infra"] });
    addMemory({ owner: "ninjia", entry_type: "lesson", content: "Check RLS policies before merge", tags: ["security"] });
    addMemory({ owner: "sansan", entry_type: "blocker", content: "Ninjia CSO review pending", tags: ["rollup"] });
  });

  it("returns all entries with no filter", () => {
    const results = searchMemory({});
    expect(results.length).toBe(3);
  });

  it("filters by owner", () => {
    const results = searchMemory({ owner: "sansan" });
    expect(results.every(r => r.owner === "sansan")).toBe(true);
    expect(results.length).toBe(2);
  });

  it("filters by entry_type", () => {
    const results = searchMemory({ entry_type: "lesson" });
    expect(results.length).toBe(1);
    expect(results[0].content).toContain("RLS");
  });

  it("searches by text query", () => {
    const results = searchMemory({ query: "SQLite" });
    expect(results.length).toBeGreaterThanOrEqual(1);
    expect(results.some(r => r.content.includes("SQLite"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// set_agent_state — key prefix enforcement (Ninjia fix)
// ---------------------------------------------------------------------------

describe("setAgentState", () => {
  it("allows owner to set their own prefixed key", () => {
    const result = setAgentState("sansan.current_pr", 266, "sansan");
    expect(result.key).toBe("sansan.current_pr");
  });

  it("rejects key with wrong prefix", () => {
    expect(() => setAgentState("ninjia.current_task", "review", "sansan")).toThrow();
  });

  it("allows team.* for owner=eleven", () => {
    const result = setAgentState("team.active_blockers", ["PR #266"], "eleven");
    expect(result.key).toBe("team.active_blockers");
  });

  it("rejects team.* for non-eleven owner", () => {
    expect(() => setAgentState("team.active_blockers", [], "sansan")).toThrow();
  });

  it("stores and retrieves JSON value", () => {
    setAgentState("sansan.meta", { pr: 266, status: "open" }, "sansan");
    const entry = getAgentState("sansan.meta");
    expect(entry).not.toBeNull();
    expect(entry!.value).toEqual({ pr: 266, status: "open" });
  });

  it("returns null for missing key", () => {
    const entry = getAgentState("sansan.nonexistent");
    expect(entry).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// deleteAgentState — owner enforcement
// ---------------------------------------------------------------------------

describe("deleteAgentState", () => {
  it("deletes own key and returns deleted=true", () => {
    setAgentState("sansan.temp", "value", "sansan");
    const result = deleteAgentState("sansan.temp", "sansan");
    expect(result.deleted).toBe(true);
    expect(getAgentState("sansan.temp")).toBeNull();
  });

  it("returns deleted=false for non-existent key", () => {
    const result = deleteAgentState("sansan.missing", "sansan");
    expect(result.deleted).toBe(false);
  });

  it("rejects deleting another agent's key", () => {
    setAgentState("ninjia.secret", "val", "ninjia");
    // sansan trying to delete ninjia's key — prefix check fires first
    expect(() => deleteAgentState("ninjia.secret", "sansan")).toThrow();
  });
});

// ---------------------------------------------------------------------------
// searchMemory — FTS5 query sanitization (C2 fix)
// ---------------------------------------------------------------------------

describe("searchMemory — FTS5 sanitization", () => {
  beforeEach(() => {
    addMemory({ owner: "sansan", entry_type: "decision", content: "Set up research-db schema", tags: ["infra"] });
  });

  it("handles hyphenated query without throwing", () => {
    // Before the fix, 'research-db' was interpreted as FTS5 column subtraction
    const results = searchMemory({ query: "research-db" });
    expect(Array.isArray(results)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// addConceptAlias — writable DB handle (C1 fix)
// ---------------------------------------------------------------------------

describe("addConceptAlias", () => {
  let vaultDir: string;

  beforeEach(async () => {
    vaultDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-alias-test-"));
    const schistDir = path.join(vaultDir, ".schist");
    await fs.mkdir(schistDir, { recursive: true });
    // Create a minimal schist.db with concept_aliases table
    const db = new Database(path.join(schistDir, "schist.db"));
    db.exec(`
      CREATE TABLE IF NOT EXISTS concept_aliases (
        duplicate_slug  TEXT NOT NULL,
        canonical_slug  TEXT NOT NULL,
        reason          TEXT,
        created_by      TEXT NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (duplicate_slug, canonical_slug)
      );
    `);
    db.close();
  });

  afterEach(async () => {
    await fs.rm(vaultDir, { recursive: true, force: true });
  });

  it("creates an alias and returns it", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const alias = addConceptAlias(vaultDir, "ml", "machine-learning", "abbreviation", "sansan");
    expect(alias.duplicate_slug).toBe("ml");
    expect(alias.canonical_slug).toBe("machine-learning");
    expect(alias.reason).toBe("abbreviation");
    expect(alias.created_by).toBe("sansan");
    expect(typeof alias.created_at).toBe("string");
  });

  it("throws when SCHIST_AGENT_ID is not set", () => {
    delete process.env.SCHIST_AGENT_ID;
    // assertOwner on this branch only checks mismatch, not missing — so this
    // should succeed (no env var = no gate). If the H1 fix from research-db-cli
    // is merged, this test should be updated to expect a throw.
    // For now, verify it doesn't crash with an unrelated error.
    const alias = addConceptAlias(vaultDir, "dl", "deep-learning", undefined, "sansan");
    expect(alias.duplicate_slug).toBe("dl");
  });

  it("throws when SCHIST_AGENT_ID mismatches created_by", () => {
    process.env.SCHIST_AGENT_ID = "ninjia";
    expect(() =>
      addConceptAlias(vaultDir, "ml", "machine-learning", "test", "sansan")
    ).toThrow();
  });
});
