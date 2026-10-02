import { describe, expect, it, beforeEach, afterEach } from "@jest/globals";
import Ajv from "ajv";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { get_note, loadVaultConfig } from "../src/tools.js";
import { formatToolResult, OUTPUT_SCHEMAS } from "../src/output-contract.js";

// The get_note tool is file-first: it builds its result from the note's
// frontmatter, NOT from the SQLite `getNote()` reader whose `Note` type also
// carries `scope` and `source`. The advertised outputSchema must describe what
// this handler actually sends. Because the schema allows additionalProperties,
// an undeclared field never fails validation, so a field added to the handler
// without a schema entry would be invisible to every CI check unless something
// compares the two directly. This is that comparison.

let vault: string;
let config: Awaited<ReturnType<typeof loadVaultConfig>>;

beforeEach(async () => {
  vault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getnote-contract-"));
  await fs.mkdir(path.join(vault, "notes"), { recursive: true });
  // Every frontmatter key the SQLite-side Note type knows about, so a handler
  // that started copying any of them through would surface here.
  await fs.writeFile(
    path.join(vault, "notes", "full.md"),
    [
      "---",
      "title: Full",
      "date: 2026-09-24",
      "status: draft",
      "tags: [a, b]",
      "concepts: [ml]",
      "scope: research/ai",
      "source: agent",
      "confidence: high",
      "file_ref: data/results.csv",
      "---",
      "",
      "Body.",
      "",
      "## Connections",
      "",
      "- extends: notes/other.md",
      "",
    ].join("\n"),
    "utf-8",
  );
  await fs.writeFile(
    path.join(vault, "schist.yaml"),
    ["directories:", "  notes: notes/", "  concepts: concepts/", "connection_types:", "  - extends", ""].join("\n"),
    "utf-8",
  );
  config = await loadVaultConfig(vault);
});

afterEach(async () => {
  await fs.rm(vault, { recursive: true, force: true });
});

describe("get_note output contract", () => {
  it("every field the handler returns is declared in the output schema", async () => {
    const res = (await get_note(vault, { id: "notes/full.md" }, config)) as Record<string, unknown>;
    expect(res.error).toBeUndefined();
    // The fixture really exercises the optional fields, so the check below is
    // not vacuous.
    expect(res.confidence).toBe("high");
    expect(res.file_ref).toBe("data/results.csv");

    const declared = Object.keys(OUTPUT_SCHEMAS.get_note.properties as Record<string, unknown>);
    const undeclared = Object.keys(res).filter((key) => !declared.includes(key));
    expect(undeclared).toEqual([]);
  });

  it("an unquoted date-only YAML value is returned as the date-only string search_notes and the index use", async () => {
    // YAML reads `date: 2026-09-24` as a Date. Cast `as string` and passed
    // through, it reaches the client as "2026-09-24T00:00:00.000Z" - a
    // different string for the same note than search_notes returns - and is a
    // non-string in-process. The write paths already coerce this; the read did not.
    const res = (await get_note(vault, { id: "notes/full.md" }, config)) as Record<string, unknown>;
    expect(typeof res.date).toBe("string");
    expect(JSON.parse(JSON.stringify(res)).date).toBe("2026-09-24");
  });

  it("a date with a time component keeps its full timestamp rather than losing the time", async () => {
    await fs.writeFile(
      path.join(vault, "notes", "stamped.md"),
      "---\ntitle: Stamped\ndate: 2026-09-24T10:30:00Z\n---\n\nBody.\n",
      "utf-8",
    );
    const res = (await get_note(vault, { id: "notes/stamped.md" }, config)) as Record<string, unknown>;
    expect(res.date).toBe("2026-09-24T10:30:00.000Z");
  });

  it("a numeric title is returned as a string, as the schema declares", async () => {
    await fs.writeFile(
      path.join(vault, "notes", "numeric.md"),
      "---\ntitle: 2026\ndate: 2026-09-24\n---\n\nBody.\n",
      "utf-8",
    );
    const res = (await get_note(vault, { id: "notes/numeric.md" }, config)) as Record<string, unknown>;
    expect(res.title).toBe("2026");
  });

  it("non-string status, tags and concepts entries cannot break the schema, as ingest guards them (#278)", async () => {
    // YAML types its scalars: `status: 42`, `tags: [2026, a]`, `concepts: [42, ml]`
    // are all valid frontmatter. Passed through, a client validating the
    // advertised outputSchema rejects the whole get_note call for such a note.
    await fs.writeFile(
      path.join(vault, "notes", "typed.md"),
      "---\ntitle: Typed\ndate: 2026-09-24\nstatus: 42\ntags: [2026, a]\nconcepts: [42, ml]\n---\n\nBody.\n",
      "utf-8",
    );
    const res = await get_note(vault, { id: "notes/typed.md" }, config);
    const wire = JSON.parse(JSON.stringify(formatToolResult("get_note", res).structuredContent));
    expect(wire.status).toBeNull();
    expect(wire.tags).toEqual(["a"]);
    expect(wire.concepts).toEqual(["ml"]);
    const validate = new Ajv().compile(OUTPUT_SCHEMAS.get_note);
    expect({ ok: validate(wire), errors: validate.errors }).toEqual({ ok: true, errors: null });
  });

  it("a list-valued status is dropped rather than returned", async () => {
    await fs.writeFile(
      path.join(vault, "notes", "listy.md"),
      "---\ntitle: Listy\ndate: 2026-09-24\nstatus: [draft]\n---\n\nBody.\n",
      "utf-8",
    );
    const res = (await get_note(vault, { id: "notes/listy.md" }, config)) as Record<string, unknown>;
    expect(res.status).toBeNull();
  });

  it("the returned note validates against the advertised schema", async () => {
    const res = await get_note(vault, { id: "notes/full.md" }, config);
    const response = formatToolResult("get_note", res);
    const validate = new Ajv().compile(OUTPUT_SCHEMAS.get_note);
    const ok = validate("structuredContent" in response ? response.structuredContent : undefined);
    expect({ ok, errors: validate.errors }).toEqual({ ok: true, errors: null });
  });
});
