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
  // Clear agent ID — individual tests set it as needed
  delete process.env.SCHIST_AGENT_ID;
});

afterEach(async () => {
  delete process.env.SCHIST_MEMORY_DB;
  delete process.env.SCHIST_AGENT_ID;
  await fs.rm(tempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// Helper: add memory with env var set
// ---------------------------------------------------------------------------
function addMemoryAs(owner: string, entry: Omit<Parameters<typeof addMemory>[0], "owner">) {
  const prev = process.env.SCHIST_AGENT_ID;
  process.env.SCHIST_AGENT_ID = owner;
  try {
    return addMemory({ owner, ...entry });
  } finally {
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
  }
}

function setStateAs(owner: string, key: string, value: unknown, ttl_hours?: number) {
  const prev = process.env.SCHIST_AGENT_ID;
  process.env.SCHIST_AGENT_ID = owner;
  try {
    return setAgentState(key, value, owner, ttl_hours);
  } finally {
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
  }
}

// ---------------------------------------------------------------------------
// add_memory — owner enforcement
// ---------------------------------------------------------------------------

describe("addMemory", () => {
  it("inserts a memory entry and returns id + created_at", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
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
    process.env.SCHIST_AGENT_ID = "sansan";
    expect(() =>
      addMemory({ owner: "sansan", entry_type: "invalid_type", content: "test" })
    ).toThrow();
  });

  it("stores and returns tags as array", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    addMemory({
      owner: "sansan",
      entry_type: "completion",
      content: "PR #266 merged",
      tags: ["rollup", "kiosk"],
    });
    const results = searchMemory({ owner: "sansan" });
    expect(results[0].tags).toEqual(["rollup", "kiosk"]);
  });

  it("throws CONFIG_ERROR when SCHIST_AGENT_ID is not set", () => {
    delete process.env.SCHIST_AGENT_ID;
    try {
      addMemory({ owner: "sansan", entry_type: "decision", content: "test" });
      fail("Expected error to be thrown");
    } catch (e: unknown) {
      expect(e).toBeInstanceOf(Error);
      expect((e as Error).message).toMatch(/SCHIST_AGENT_ID/);
      expect((e as Record<string, unknown>).error).toBe("CONFIG_ERROR");
    }
  });
});

// ---------------------------------------------------------------------------
// searchMemory
// ---------------------------------------------------------------------------

describe("searchMemory", () => {
  beforeEach(() => {
    addMemoryAs("sansan", { entry_type: "decision", content: "Use SQLite WAL mode", tags: ["infra"] });
    addMemoryAs("ninjia", { entry_type: "lesson", content: "Check RLS policies before merge", tags: ["security"] });
    addMemoryAs("sansan", { entry_type: "blocker", content: "Ninjia CSO review pending", tags: ["rollup"] });
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
    process.env.SCHIST_AGENT_ID = "sansan";
    const result = setAgentState("sansan.current_pr", 266, "sansan");
    expect(result.key).toBe("sansan.current_pr");
  });

  it("rejects key with wrong prefix", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    expect(() => setAgentState("ninjia.current_task", "review", "sansan")).toThrow();
  });

  it("allows team.* for owner=eleven", () => {
    process.env.SCHIST_AGENT_ID = "eleven";
    const result = setAgentState("team.active_blockers", ["PR #266"], "eleven");
    expect(result.key).toBe("team.active_blockers");
  });

  it("rejects team.* for non-eleven owner", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    expect(() => setAgentState("team.active_blockers", [], "sansan")).toThrow();
  });

  it("stores and retrieves JSON value", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    setAgentState("sansan.meta", { pr: 266, status: "open" }, "sansan");
    const entry = getAgentState("sansan.meta");
    expect(entry).not.toBeNull();
    expect(entry!.value).toEqual({ pr: 266, status: "open" });
  });

  it("returns null for missing key", () => {
    const entry = getAgentState("sansan.nonexistent");
    expect(entry).toBeNull();
  });

  it("throws OWNERSHIP_ERROR when agent B tries to overwrite agent A's key", () => {
    // Agent A sets the key
    setStateAs("ninjia", "ninjia.secret_key", "original_value");

    // Agent B (with matching prefix via team scenario isn't possible, so we
    // test via direct DB setup): insert a key with owner=ninjia, then agent
    // sansan tries to overwrite with a key that has ninjia prefix.
    // Since prefix check fires first for sansan->ninjia prefix, we need a
    // scenario where prefix passes but owner differs.
    // Use team.* key: eleven sets it, then another eleven-impersonating agent
    // can't hijack. Actually the simplest: ninjia sets ninjia.x, then
    // another caller claiming to be ninjia but actually being someone else
    // via raw DB manipulation. But with assertOwner, the env var must match.
    //
    // Real scenario: Agent A (ninjia) creates ninjia.task, then the DB has
    // owner=ninjia. Now if SCHIST_AGENT_ID changes to a different value but
    // someone calls setAgentState with owner matching the new SCHIST_AGENT_ID
    // and a key that already exists with a different owner.
    //
    // Simplest: use team.* keys — eleven creates team.x, then we change
    // owner in DB to simulate another agent, and eleven tries to overwrite.
    // Actually even simpler: directly test the ownership check.

    // eleven sets team.shared
    setStateAs("eleven", "team.shared", "eleven_data");

    // Manually change the owner in DB to simulate a different agent owning it
    const db = new Database(process.env.SCHIST_MEMORY_DB!);
    db.prepare("UPDATE agent_state SET owner = 'ninjia' WHERE key = 'team.shared'").run();
    db.close();

    // Now eleven tries to overwrite — should fail with OWNERSHIP_ERROR
    process.env.SCHIST_AGENT_ID = "eleven";
    try {
      setAgentState("team.shared", "hijack", "eleven");
      fail("Expected error to be thrown");
    } catch (e: unknown) {
      expect(e).toBeInstanceOf(Error);
      expect((e as Error).message).toMatch(/owned by another agent/);
      expect((e as Record<string, unknown>).error).toBe("OWNERSHIP_ERROR");
    }
  });
});

// ---------------------------------------------------------------------------
// deleteAgentState — owner enforcement
// ---------------------------------------------------------------------------

describe("deleteAgentState", () => {
  it("deletes own key and returns deleted=true", () => {
    setStateAs("sansan", "sansan.temp", "value");
    process.env.SCHIST_AGENT_ID = "sansan";
    const result = deleteAgentState("sansan.temp", "sansan");
    expect(result.deleted).toBe(true);
    expect(getAgentState("sansan.temp")).toBeNull();
  });

  it("returns deleted=false for non-existent key", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const result = deleteAgentState("sansan.missing", "sansan");
    expect(result.deleted).toBe(false);
  });

  it("rejects deleting another agent's key", () => {
    setStateAs("ninjia", "ninjia.secret", "val");
    // sansan trying to delete ninjia's key — prefix check fires first
    process.env.SCHIST_AGENT_ID = "sansan";
    expect(() => deleteAgentState("ninjia.secret", "sansan")).toThrow();
  });
});

// ---------------------------------------------------------------------------
// searchMemory — FTS5 query sanitization (C2 fix)
// ---------------------------------------------------------------------------

describe("searchMemory — FTS5 sanitization", () => {
  beforeEach(() => {
    addMemoryAs("sansan", { entry_type: "decision", content: "Set up research-db schema", tags: ["infra"] });
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
    expect(() =>
      addConceptAlias(vaultDir, "dl", "deep-learning", undefined, "sansan")
    ).toThrow(/SCHIST_AGENT_ID/);
  });

  it("throws when SCHIST_AGENT_ID mismatches created_by", () => {
    process.env.SCHIST_AGENT_ID = "ninjia";
    expect(() =>
      addConceptAlias(vaultDir, "ml", "machine-learning", "test", "sansan")
    ).toThrow();
  });
});
