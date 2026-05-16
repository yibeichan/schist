import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { search_memory } from "../src/tools.js";
import { addMemory } from "../src/sqlite-reader.js";
import { resetCursorForTesting, resetVerboseForTesting, issueCursor } from "../src/protocol/index.js";

let tempDir: string;
const VAULT_ROOT = "/tmp/not-used-by-memory-tools"; // search_memory ignores vaultRoot for memory db

beforeEach(async () => {
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-sm-tool-test-"));
  process.env.SCHIST_MEMORY_DB = path.join(tempDir, "test-memory.db");
  delete process.env.SCHIST_AGENT_ID;
  resetCursorForTesting();
  resetVerboseForTesting();
});

afterEach(async () => {
  delete process.env.SCHIST_MEMORY_DB;
  delete process.env.SCHIST_AGENT_ID;
  await fs.rm(tempDir, { recursive: true, force: true });
});

// Helper: seed N memory entries owned by `owner` so the tool has data to page
function seed(owner: string, n: number, contentPrefix = "entry"): void {
  const prev = process.env.SCHIST_AGENT_ID;
  process.env.SCHIST_AGENT_ID = owner;
  try {
    for (let i = 0; i < n; i++) {
      addMemory({ owner, entry_type: "decision", content: `${contentPrefix}-${i}` });
    }
  } finally {
    if (prev === undefined) delete process.env.SCHIST_AGENT_ID;
    else process.env.SCHIST_AGENT_ID = prev;
  }
}

describe("search_memory tool — verbose input parsing", () => {
  it("returns INVALID_ARG when verbose is a boolean", async () => {
    const r = await search_memory(VAULT_ROOT, { verbose: true } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("verbose") });
  });

  it("returns INVALID_ARG when verbose is a too-short string", async () => {
    const r = await search_memory(VAULT_ROOT, { verbose: "short" } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("code points") });
  });

  it("treats an empty verbose string as not-verbose (no error, no full content)", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, { verbose: "" } as never);
    expect(r).toHaveProperty("entries");
    if ("entries" in r) {
      expect(r.entries.length).toBe(2);
    }
  });

  it("treats omitted verbose as not-verbose", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, {} as never);
    expect(r).toHaveProperty("entries");
    if ("entries" in r) {
      expect(r.entries.length).toBe(2);
    }
  });
});

describe("search_memory tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await search_memory(VAULT_ROOT, { limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});

describe("search_memory tool — cursor decoding", () => {
  it("returns CURSOR_INVALID_SIGNATURE when the cursor signature is malformed", async () => {
    seed("sansan", 5);
    const r = await search_memory(VAULT_ROOT, { cursor: "garbage.notreallya.cursor" } as never);
    expect(r).toEqual({ error: "CURSOR_INVALID_SIGNATURE", message: expect.any(String) });
  });

  it("returns CURSOR_WRONG_TOOL when a cursor for a different tool is presented", async () => {
    seed("sansan", 5);
    // Forge a cursor for search_notes against our HMAC secret
    const c = issueCursor({ tool: "search_notes", queryHash: "deadbeef", offset: 5 });
    const r = await search_memory(VAULT_ROOT, { cursor: c } as never);
    expect(r).toEqual({ error: "CURSOR_WRONG_TOOL", message: expect.stringContaining("search_notes") });
  });

  it("returns CURSOR_INVALID_SIGNATURE when cursor queryHash does NOT match current args (binding policy)", async () => {
    seed("sansan", 5);
    // Issue a cursor with a deliberately wrong queryHash
    const c = issueCursor({ tool: "search_memory", queryHash: "0".repeat(64), offset: 2 });
    // Present that cursor on a search_memory call. The current args compute a
    // different queryHash, so binding must reject.
    const r = await search_memory(VAULT_ROOT, { cursor: c } as never);
    expect(r).toEqual({
      error: "CURSOR_INVALID_SIGNATURE",
      message: expect.stringContaining("different query"),
    });
  });

  it("accepts a cursor whose encoded queryHash matches the current args (round-trip)", async () => {
    seed("sansan", 10);
    // Compute the canonical queryHash for the args we'll pass and forge a
    // cursor matching it; the handler should accept it and proceed.
    const { canonicalizeQueryHash } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    expect(ch.ok).toBe(true);
    if (!ch.ok) return;
    const c = issueCursor({ tool: "search_memory", queryHash: ch.queryHash, offset: 3 });
    const r = await search_memory(VAULT_ROOT, { ...args, cursor: c } as never);
    expect(r).toHaveProperty("entries");
    // The presence of `entries` and absence of an `error` field is sufficient
    // for Task 3.6. Full pagination assertions land in Task 3.8.
    expect(r).not.toHaveProperty("error");
  });
});

describe("search_memory tool — identical-query refusal", () => {
  // Helper pattern: drive the handler into "results were capped" state by
  // calling recordIssued directly. The actual record/issue wiring lands in
  // Task 3.8 — for Task 3.7 we just need to verify checkRefusal is wired
  // correctly when there's a prior LRU entry.

  it("returns CURSOR_REQUIRED on identical (tool, queryHash, activeOwner) within TTL", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: false });
    // Second identical call (no cursor) must be refused
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.stringContaining("Identical query"),
    });
  });

  it("does NOT refuse when activeOwner differs (different owner namespace)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    // Record under owner "yibei"
    const ch = canonicalizeQueryHash(args, "yibei");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "yibei", verboseEnabled: false });
    // Current call's activeOwner is "" (SCHIST_AGENT_ID not set) — different
    // namespace, no refusal expected. Both queryHash AND owner differ in the
    // LRU key, so checkRefusal returns refuse:false.
    delete process.env.SCHIST_AGENT_ID;
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toHaveProperty("entries");
  });

  it("verbose-newly-set bypasses refusal (false -> true)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: false });
    // Now retry the identical query with verbose newly set
    const r = await search_memory(VAULT_ROOT, {
      ...args,
      verbose: "user requested full content for review",
    } as never);
    expect(r).toHaveProperty("entries");
    expect(r).not.toHaveProperty("error");
  });

  it("STILL refuses on verbose true -> true (identical+verbose retry)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    // Record with verboseEnabled=true (prior verbose call)
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: true });
    const r = await search_memory(VAULT_ROOT, {
      ...args,
      verbose: "user requested full content for review",
    } as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.any(String),
    });
  });

  it("STILL refuses on verbose true -> false (downgrade)", async () => {
    seed("sansan", 10);
    const { canonicalizeQueryHash, recordIssued } = await import("../src/protocol/index.js");
    const args = { owner: "sansan", limit: 3 };
    const ch = canonicalizeQueryHash(args, "");
    if (!ch.ok) throw new Error("canonicalize failed in test setup");
    recordIssued({ tool: "search_memory", queryHash: ch.queryHash, owner: "", verboseEnabled: true });
    // Current call has NO verbose — downgrade should still refuse
    const r = await search_memory(VAULT_ROOT, args as never);
    expect(r).toEqual({
      error: "CURSOR_REQUIRED",
      message: expect.any(String),
    });
  });
});
