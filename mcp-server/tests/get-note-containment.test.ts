import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { get_note, add_connection, loadVaultConfig } from "../src/tools.js";

// #480: get_note checked containment with path.resolve, which resolves ".."
// lexically but does NOT follow symlinks — while the fs.readFile that follows
// DOES. A symlink planted inside the vault therefore passed the guard and
// disclosed its target. The write tools (create_note / update_note /
// delete_note / add_connection) already used realpath-based containment, so
// this was a read-side parity gap, and get_note is the one read tool with no
// owner parameter.
//
// These tests plant a real symlink rather than asserting on the guard's shape,
// so they fail if the containment regresses to any string-only check.

let vault: string;
let outside: string;

beforeEach(async () => {
  vault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getnote-vault-"));
  outside = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getnote-outside-"));
  await fs.mkdir(path.join(vault, "notes"), { recursive: true });
  await fs.writeFile(
    path.join(vault, "notes", "real.md"),
    "---\ntitle: Real\n---\n\nIn-vault body.\n",
    "utf-8",
  );
  // `concepts` is listed lowercase, as every real vault has it — that is
  // precisely what makes validateNoteId reject a capitalized id (#475).
  await fs.writeFile(
    path.join(vault, "schist.yaml"),
    [
      "directories:",
      "  notes: notes/",
      "  concepts: concepts/",
      "connection_types:",
      "  - extends",
      "  - related",
      "",
    ].join("\n"),
    "utf-8",
  );
});

afterEach(async () => {
  await fs.rm(vault, { recursive: true, force: true });
  await fs.rm(outside, { recursive: true, force: true });
});

describe("get_note vault containment", () => {
  it("reads a genuine in-vault note", async () => {
    const res = (await get_note(vault, { id: "notes/real.md" })) as Record<string, unknown>;
    expect(res.error).toBeUndefined();
    expect(res.title).toBe("Real");
    expect(res.body).toContain("In-vault body");
  });

  it("refuses a symlinked FILE whose target is outside the vault", async () => {
    const secret = path.join(outside, "secret.md");
    await fs.writeFile(secret, "---\ntitle: Secret\n---\n\nTOP SECRET PAYLOAD\n", "utf-8");
    await fs.symlink(secret, path.join(vault, "notes", "link.md"));

    const res = (await get_note(vault, { id: "notes/link.md" })) as Record<string, unknown>;

    expect(res.error).toBe("PATH_TRAVERSAL");
    // The disclosure itself, not just the error code: no field may carry the
    // target's contents or title.
    expect(JSON.stringify(res)).not.toContain("TOP SECRET PAYLOAD");
    expect(JSON.stringify(res)).not.toContain("Secret");
  });

  it("refuses a note reached THROUGH a symlinked directory", async () => {
    // The escape need not be the leaf: a symlinked parent is equally effective
    // and equally invisible to a lexical check.
    const outDir = path.join(outside, "elsewhere");
    await fs.mkdir(outDir, { recursive: true });
    await fs.writeFile(
      path.join(outDir, "leaf.md"),
      "---\ntitle: Leaked\n---\n\nDIRECTORY ESCAPE PAYLOAD\n",
      "utf-8",
    );
    await fs.symlink(outDir, path.join(vault, "notes", "hop"));

    const res = (await get_note(vault, { id: "notes/hop/leaf.md" })) as Record<string, unknown>;

    expect(res.error).toBe("PATH_TRAVERSAL");
    expect(JSON.stringify(res)).not.toContain("DIRECTORY ESCAPE PAYLOAD");
  });

  it("still refuses a lexical .. escape", async () => {
    const res = (await get_note(vault, { id: "../escape.md" })) as Record<string, unknown>;
    expect(res.error).toBe("PATH_TRAVERSAL");
  });

  it("reports a missing note as NOT_FOUND, not PATH_TRAVERSAL", async () => {
    // resolvesInsideVault returns false on ENOENT too, so the containment check
    // must not swallow the ordinary missing-file case into a scary error code.
    const res = (await get_note(vault, { id: "notes/absent.md" })) as Record<string, unknown>;
    expect(res.error).toBe("NOT_FOUND");
  });

  it("follows an in-vault symlink that stays inside the vault", async () => {
    // Containment is about the RESOLVED location, not about symlinks per se —
    // shared/symlinked content inside the vault must keep working.
    await fs.symlink(
      path.join(vault, "notes", "real.md"),
      path.join(vault, "notes", "alias.md"),
    );
    const res = (await get_note(vault, { id: "notes/alias.md" })) as Record<string, unknown>;
    expect(res.error).toBeUndefined();
    expect(res.title).toBe("Real");
  });
});

describe("add_connection concept-source guard: the real gate is validateNoteId (#475)", () => {
  // #475 claims `Concepts/foo.md` bypasses the concept-source prohibition
  // because isConceptNoteId compares case-sensitively. It does compare
  // case-sensitively — but two earlier gates make it unreachable: the identity
  // check, then validateNoteId, which tests the id's top segment against the
  // configured directory list (lowercase "concepts"). These tests pin the real
  // ordering so a refactor that reorders or drops either gate fails loudly
  // rather than quietly opening the hole the issue describes.
  const AGENT = "test-agent";

  beforeEach(() => {
    process.env.SCHIST_AGENT_ID = AGENT;
  });
  afterEach(() => {
    delete process.env.SCHIST_AGENT_ID;
  });

  it("rejects a capitalized concept id before the concept guard is reached", async () => {
    await fs.mkdir(path.join(vault, "Concepts"), { recursive: true });
    await fs.writeFile(
      path.join(vault, "Concepts", "foo.md"),
      "---\ntitle: Foo\n---\n\nDefinition.\n\n## Connections\n",
      "utf-8",
    );
    const config = await loadVaultConfig(vault);

    const res = (await add_connection(vault, {
      owner: AGENT,
      source: "Concepts/foo.md",
      target: "notes/real.md",
      type: "extends",
    } as Parameters<typeof add_connection>[1], config)) as Record<string, unknown>;

    expect(res.error).toBe("VALIDATION_ERROR");
    expect(String(res.message)).toContain("configured directory");
  });

  it("still rejects the lowercase concept id via the concept guard itself", async () => {
    await fs.mkdir(path.join(vault, "concepts"), { recursive: true });
    await fs.writeFile(
      path.join(vault, "concepts", "foo.md"),
      "---\ntitle: Foo\n---\n\nDefinition.\n",
      "utf-8",
    );
    const config = await loadVaultConfig(vault);

    const res = (await add_connection(vault, {
      owner: AGENT,
      source: "concepts/foo.md",
      target: "notes/real.md",
      type: "extends",
    } as Parameters<typeof add_connection>[1], config)) as Record<string, unknown>;

    expect(res.error).toBe("VALIDATION_ERROR");
    expect(String(res.message)).toContain("cannot be connection sources");
  });
});
