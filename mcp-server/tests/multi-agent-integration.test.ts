/**
 * Multi-agent integration test — regression guard for the original gap that
 * let PR #61 ship as a no-op.
 *
 * The bug was: PR added SCHIST_ALLOWED_AGENTS enforcement to tools.ts but left
 * sqlite-reader.ts's identity guard hard-coded to SCHIST_AGENT_ID. The MCP tool
 * passed allowlist mode, the SQLite layer then threw CONFIG_ERROR or
 * VALIDATION_ERROR before any data was written. End-to-end, the multi-agent
 * use case was still broken.
 *
 * These tests exercise BOTH layers under allowlist mode to ensure that
 * `octopus` (and friends) can actually write through the shared MCP subprocess
 * config that PR #61 documented.
 */
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import Database from "better-sqlite3";
import {
  addMemory,
  setAgentState,
  deleteAgentState,
  searchMemory,
  getAgentState,
} from "../src/sqlite-reader.js";
import { add_memory, set_agent_state, delete_agent_state, add_concept_alias } from "../src/tools.js";

let tempDir: string;

beforeEach(async () => {
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-multi-agent-test-"));
  process.env.SCHIST_MEMORY_DB = path.join(tempDir, "test-memory.db");
  delete process.env.SCHIST_AGENT_ID;
  delete process.env.SCHIST_ALLOWED_AGENTS;
});

afterEach(async () => {
  delete process.env.SCHIST_MEMORY_DB;
  delete process.env.SCHIST_AGENT_ID;
  delete process.env.SCHIST_ALLOWED_AGENTS;
  await fs.rm(tempDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// SQLite-reader layer (the layer that was missing the fix in the original PR)
// ---------------------------------------------------------------------------

describe("sqlite-reader honors SCHIST_ALLOWED_AGENTS", () => {
  it("addMemory accepts an allowlisted owner with SCHIST_AGENT_ID unset", () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus,sansan";
    // Critically: SCHIST_AGENT_ID is unset. Before the fix this threw CONFIG_ERROR.
    expect(() =>
      addMemory({
        owner: "octopus",
        entry_type: "decision",
        content: "test from octopus",
      })
    ).not.toThrow();
  });

  it("addMemory accepts any allowlisted owner from a single shared subprocess", () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus,sansan";
    // Several agents writing through one process — the OpenClaw deployment shape
    const a = addMemory({ owner: "octopus", entry_type: "lesson", content: "a" });
    const b = addMemory({ owner: "sansan", entry_type: "lesson", content: "b" });
    const c = addMemory({ owner: "eleven", entry_type: "lesson", content: "c" });
    expect(a.id).toBeGreaterThan(0);
    expect(b.id).toBeGreaterThan(0);
    expect(c.id).toBeGreaterThan(0);

    const results = searchMemory({});
    const owners = new Set(results.map((r) => r.owner));
    expect(owners).toEqual(new Set(["octopus", "sansan", "eleven"]));
  });

  it("addMemory rejects an owner outside the allowlist", () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    expect(() =>
      addMemory({ owner: "ninjia", entry_type: "decision", content: "blocked" })
    ).toThrow(/not in SCHIST_ALLOWED_AGENTS/);
  });

  it("setAgentState + deleteAgentState honor the allowlist", () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    setAgentState("octopus.current_pr", "61", "octopus");
    // Round-trip: state should exist after set
    expect(getAgentState("octopus.current_pr")).not.toBeNull();
    deleteAgentState("octopus.current_pr", "octopus");
    // ...and be gone after delete
    expect(getAgentState("octopus.current_pr")).toBeNull();
  });

  it("falls back to SCHIST_AGENT_ID when SCHIST_ALLOWED_AGENTS is unset", () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    expect(() =>
      addMemory({ owner: "sansan", entry_type: "decision", content: "ok" })
    ).not.toThrow();
    expect(() =>
      addMemory({ owner: "octopus", entry_type: "decision", content: "no" })
    ).toThrow(/does not match SCHIST_AGENT_ID/);
  });

  it("throws CONFIG_ERROR when neither env var is set", () => {
    expect(() =>
      addMemory({ owner: "anyone", entry_type: "decision", content: "x" })
    ).toThrow(/SCHIST_AGENT_ID or SCHIST_ALLOWED_AGENTS/);
  });
});

// ---------------------------------------------------------------------------
// MCP tool layer — exercise the public entry points end-to-end
// ---------------------------------------------------------------------------

describe("MCP tool layer (add_memory / set_agent_state / delete_agent_state)", () => {
  // tools.ts wraps thrown errors with normalizeError and returns them rather
  // than throwing. Result objects with an `error` field signal failure.
  function isErrorResult(r: unknown): r is { error: string; message: string } {
    return typeof r === "object" && r !== null && "error" in r;
  }

  it("add_memory(owner=octopus) succeeds when octopus is allowlisted", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus,sansan";
    const result = await add_memory("/tmp/unused-vault", {
      owner: "octopus",
      entry_type: "decision",
      content: "multi-agent write via tool layer",
    });
    expect(isErrorResult(result)).toBe(false);
    expect(result).toMatchObject({ id: expect.any(Number) });
  });

  it("add_memory(owner=ninjia) returns VALIDATION_ERROR when ninjia is not allowlisted", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    const result = await add_memory("/tmp/unused-vault", {
      owner: "ninjia",
      entry_type: "decision",
      content: "should be blocked",
    });
    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toMatch(/not in SCHIST_ALLOWED_AGENTS/);
    }
  });

  it("set_agent_state + delete_agent_state work via tool layer under allowlist mode", async () => {
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    const setRes = await set_agent_state("/tmp/unused-vault", {
      key: "octopus.session",
      value: { active: true },
      owner: "octopus",
    });
    expect(isErrorResult(setRes)).toBe(false);

    const delRes = await delete_agent_state("/tmp/unused-vault", {
      key: "octopus.session",
      owner: "octopus",
    });
    expect(isErrorResult(delRes)).toBe(false);
  });

  it("add_concept_alias rejects non-allowlisted created_by via tool layer", async () => {
    // validateOwner runs before any DB touch, so the negative path doesn't
    // need a real vault — /tmp/unused-vault is never opened.
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    const result = await add_concept_alias("/tmp/unused-vault", {
      duplicate_slug: "foo",
      canonical_slug: "bar",
      created_by: "ninjia",
    });
    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toMatch(/not in SCHIST_ALLOWED_AGENTS/);
    }
  });
});

// ---------------------------------------------------------------------------
// add_concept_alias slug normalization (#338/#317): create/update/delete all
// normalize concept slugs before they hit the index (#302/#303), so an alias
// stored raw ("Neural Networks") could never match a concepts row and the next
// ingest would garbage-collect it silently.
// ---------------------------------------------------------------------------

describe("add_concept_alias normalizes slugs before storage (#338/#317)", () => {
  function isErrorResult(r: unknown): r is { error: string; message: string } {
    return typeof r === "object" && r !== null && "error" in r;
  }

  async function seedAliasDb(vault: string): Promise<string> {
    const schistDir = path.join(vault, ".schist");
    await fs.mkdir(schistDir, { recursive: true });
    const dbPath = path.join(schistDir, "schist.db");
    const db = new Database(dbPath);
    db.exec(`
      CREATE TABLE concepts (slug TEXT PRIMARY KEY, name TEXT NOT NULL);
      INSERT INTO concepts (slug, name) VALUES
        ('neural-networks', 'Neural Networks'),
        ('machine-learning', 'Machine Learning');
      CREATE TABLE concept_aliases (
        duplicate_slug  TEXT NOT NULL REFERENCES concepts(slug),
        canonical_slug  TEXT NOT NULL REFERENCES concepts(slug),
        reason          TEXT,
        created_by      TEXT NOT NULL,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (duplicate_slug, canonical_slug)
      );
    `);
    db.close();
    return dbPath;
  }

  it("stores display-form input normalized so index lookups (and ingest GC) match", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    const dbPath = await seedAliasDb(tempDir);

    // Display form + edge/multiple whitespace: exactly what create_note's
    // `concepts` normalization would have collapsed before indexing.
    const result = await add_concept_alias(tempDir, {
      duplicate_slug: "Neural Networks",
      canonical_slug: "  Machine   Learning ",
      created_by: "sansan",
    });

    expect(isErrorResult(result)).toBe(false);
    const alias = result as { duplicate_slug: string; canonical_slug: string };
    expect(alias.duplicate_slug).toBe("neural-networks");
    expect(alias.canonical_slug).toBe("machine-learning");

    // Findable via the normalized slug — the form every other tool queries by.
    const db = new Database(dbPath, { readonly: true });
    const row = db
      .prepare("SELECT canonical_slug FROM concept_aliases WHERE duplicate_slug = ?")
      .get("neural-networks") as { canonical_slug: string } | undefined;
    db.close();
    expect(row).toBeDefined();
    expect(row!.canonical_slug).toBe("machine-learning");
  });

  it("rejects a slug that normalizes to empty", async () => {
    process.env.SCHIST_AGENT_ID = "sansan";
    await seedAliasDb(tempDir);

    const result = await add_concept_alias(tempDir, {
      duplicate_slug: "   ",
      canonical_slug: "machine-learning",
      created_by: "sansan",
    });

    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toMatch(/non-empty after normalization/);
    }
  });
});
