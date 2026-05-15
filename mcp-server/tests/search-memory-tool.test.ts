import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { search_memory } from "../src/tools.js";
import { addMemory } from "../src/sqlite-reader.js";
import { resetCursorForTesting, resetVerboseForTesting } from "../src/protocol/index.js";

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
  });

  it("treats omitted verbose as not-verbose", async () => {
    seed("sansan", 2);
    const r = await search_memory(VAULT_ROOT, {} as never);
    expect(r).toHaveProperty("entries");
  });
});

describe("search_memory tool — canonicalize errors", () => {
  it("returns INVALID_ARG when an arg is unhashable (NaN)", async () => {
    const r = await search_memory(VAULT_ROOT, { limit: NaN } as never);
    expect(r).toEqual({ error: "INVALID_ARG", message: expect.stringContaining("non-finite") });
  });
});
