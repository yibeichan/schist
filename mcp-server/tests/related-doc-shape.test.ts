import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { noteIdShapeError } from "../src/note-id.js";
import { add_memory } from "../src/tools.js";
import { searchMemory } from "../src/sqlite-reader.js";

// Slice C (docs/data-model.md D4): agent_memory.related_doc is defined as "a
// vault note id (notes/….md)". add_memory validates the SHAPE only — never
// existence — via the shared rule in note-id.ts, so memory stays writable
// when the vault is unavailable. These tests exercise the shared shape core
// and the tool-layer wiring, including the no-vault property: every
// add_memory call below uses a vault root that does not exist.

let tempDir: string;
// Snapshot/restore (not delete) — jest workers reuse one process across test
// files, so blindly deleting a shell-inherited var here would silently wipe
// it for suites that run later in the same worker.
const envSnapshot: Record<string, string | undefined> = {};
const envKeys = ["SCHIST_MEMORY_DB", "SCHIST_AGENT_ID", "SCHIST_ALLOWED_AGENTS"] as const;

beforeEach(async () => {
  for (const k of envKeys) {
    envSnapshot[k] = process.env[k];
    delete process.env[k];
  }
  tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "schist-relateddoc-test-"));
  process.env.SCHIST_MEMORY_DB = path.join(tempDir, "test-memory.db");
  process.env.SCHIST_AGENT_ID = "sansan";
});

afterEach(async () => {
  for (const k of envKeys) {
    if (envSnapshot[k] === undefined) delete process.env[k];
    else process.env[k] = envSnapshot[k];
  }
  await fs.rm(tempDir, { recursive: true, force: true });
});

function isErrorResult(r: unknown): r is { error: string; message: string } {
  return typeof r === "object" && r !== null && "error" in r;
}

// A vault root that does not exist — proves the shape check needs no vault.
const NO_VAULT = "/tmp/schist-no-such-vault";

// ---------------------------------------------------------------------------
// noteIdShapeError — the single shared shape rule
// ---------------------------------------------------------------------------

describe("noteIdShapeError (shared note-id shape core)", () => {
  it("accepts well-formed vault note ids", () => {
    expect(noteIdShapeError("notes/topic.md")).toBeNull();
    expect(noteIdShapeError("research/ai/agents.md")).toBeNull();
    expect(noteIdShapeError("papers/smith-2026.md")).toBeNull();
  });

  it("rejects traversal and absolute paths", () => {
    expect(noteIdShapeError("../etc/passwd.md")).toMatch(/relative path without '\.\.'/);
    expect(noteIdShapeError("notes/../.git/config.md")).toMatch(/relative path without '\.\.'/);
    expect(noteIdShapeError("/abs/notes/x.md")).toMatch(/relative path without '\.\.'/);
  });

  it("rejects non-.md targets", () => {
    expect(noteIdShapeError("notes/topic.txt")).toMatch(/must be a \.md file/);
    expect(noteIdShapeError("notes/topic")).toMatch(/must be a \.md file/);
  });

  it("rejects ids without a top-level directory segment", () => {
    expect(noteIdShapeError("topic.md")).toMatch(/top-level directory/);
  });

  it("rejects dot-prefixed segments (.git, .schist, dotfiles)", () => {
    expect(noteIdShapeError(".git/hooks/post-commit.md")).toMatch(/must not start with '\.'/);
    expect(noteIdShapeError("notes/.hidden.md")).toMatch(/must not start with '\.'/);
    expect(noteIdShapeError(".schist/schist.md")).toMatch(/must not start with '\.'/);
  });
});

// ---------------------------------------------------------------------------
// add_memory tool layer — related_doc shape validation
// ---------------------------------------------------------------------------

describe("add_memory — related_doc shape validation (D4)", () => {
  it("accepts a valid vault note id and stores it verbatim", async () => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "decision",
      content: "graduated the FTS5 lesson",
      related_doc: "notes/fts5-sanitization.md",
    });
    expect(isErrorResult(result)).toBe(false);
    const rows = searchMemory({ owner: "sansan" });
    expect(rows.length).toBe(1);
    expect(rows[0].related_doc).toBe("notes/fts5-sanitization.md");
  });

  it("accepts an id for a note that does not exist (existence is deliberately unchecked)", async () => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "lesson",
      content: "vault is offline right now",
      related_doc: "notes/definitely-not-created-yet.md",
    });
    expect(isErrorResult(result)).toBe(false);
  });

  it("still accepts entries without related_doc", async () => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "observation",
      content: "no back-reference here",
    });
    expect(isErrorResult(result)).toBe(false);
    expect(searchMemory({ owner: "sansan" })[0].related_doc).toBeUndefined();
  });

  it.each([
    ["free text", "see my notes from tuesday"],
    ["traversal", "../secrets/creds.md"],
    ["absolute path", "/etc/notes/x.md"],
    ["non-.md", "notes/topic.txt"],
    ["no directory segment", "topic.md"],
    ["dot-prefixed segment", "notes/.hidden.md"],
    ["empty string", ""],
  ])("rejects %s with VALIDATION_ERROR naming related_doc and writes nothing", async (_label, relatedDoc) => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "decision",
      content: "should not land",
      related_doc: relatedDoc,
    });
    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toContain("related_doc");
    }
    expect(searchMemory({ owner: "sansan" })).toEqual([]);
  });

  it("rejects a non-string related_doc", async () => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "decision",
      content: "should not land",
      related_doc: 42 as unknown as string,
    });
    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toContain("related_doc");
    }
    expect(searchMemory({ owner: "sansan" })).toEqual([]);
  });

  it("treats an explicit null like an omission (stored as no back-reference)", async () => {
    const result = await add_memory(NO_VAULT, {
      owner: "sansan",
      entry_type: "decision",
      content: "null back-reference",
      related_doc: null as unknown as undefined,
    });
    expect(isErrorResult(result)).toBe(false);
    expect(searchMemory({ owner: "sansan" })[0].related_doc).toBeUndefined();
  });

  it("validates shape under allowlist mode too", async () => {
    delete process.env.SCHIST_AGENT_ID;
    process.env.SCHIST_ALLOWED_AGENTS = "eleven,octopus";
    const result = await add_memory(NO_VAULT, {
      owner: "octopus",
      entry_type: "decision",
      content: "should not land",
      related_doc: "not a note id",
    });
    expect(isErrorResult(result)).toBe(true);
    if (isErrorResult(result)) {
      expect(result.error).toBe("VALIDATION_ERROR");
      expect(result.message).toContain("related_doc");
    }
  });
});
