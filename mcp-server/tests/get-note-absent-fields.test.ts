// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import { afterEach, beforeEach, describe, expect, it } from "@jest/globals";
import Ajv from "ajv";
import * as os from "os";
import * as path from "path";
import * as fs from "fs/promises";
import { get_note, loadVaultConfig } from "../src/tools.js";
import { formatToolResult, OUTPUT_SCHEMAS } from "../src/output-contract.js";

// What get_note returns today for frontmatter fields a note does not set:
// title and date come back as "" and status as null, and the three differ in
// the schema (string, string, string-or-null). These tests pin that shape for
// notes without a date, a title or a status (a concept note typically has no
// date), and check the result against the advertised schema. They describe
// current behavior; they are not a statement that the two conventions are right.

let vault: string;
let config: Awaited<ReturnType<typeof loadVaultConfig>>;

async function note(rel: string, text: string): Promise<void> {
  await fs.mkdir(path.dirname(path.join(vault, rel)), { recursive: true });
  await fs.writeFile(path.join(vault, rel), text, "utf-8");
}

async function wireOf(id: string): Promise<Record<string, unknown>> {
  const res = await get_note(vault, { id }, config);
  expect((res as { error?: string }).error).toBeUndefined();
  const wire = JSON.parse(JSON.stringify(formatToolResult("get_note", res).structuredContent));
  const validate = new Ajv().compile(OUTPUT_SCHEMAS.get_note);
  expect({ ok: validate(wire), errors: validate.errors }).toEqual({ ok: true, errors: null });
  return wire;
}

beforeEach(async () => {
  vault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-getnote-absent-"));
  await fs.writeFile(
    path.join(vault, "schist.yaml"),
    ["directories:", "  notes: notes/", "  concepts: concepts/", "connection_types:", "  - extends", ""].join("\n"),
    "utf-8",
  );
  await note("concepts/ml.md", "---\ntitle: ML\nconcept: ml\n---\n\nStable reference.\n");
  await note("notes/untitled.md", "---\ndate: 2026-09-24\nstatus: draft\n---\n\nBody.\n");
  await note("notes/bare.md", "---\ntags: [a]\n---\n\nBody.\n");
  await note("notes/empty-date.md", "---\ntitle: Empty\ndate: \"\"\n---\n\nBody.\n");
  config = await loadVaultConfig(vault);
});

afterEach(async () => {
  await fs.rm(vault, { recursive: true, force: true });
});

describe("get_note output for absent optional frontmatter (#685)", () => {
  it("a concept note with no date returns date as an empty string, status as null, and validates", async () => {
    const wire = await wireOf("concepts/ml.md");
    expect(wire.title).toBe("ML");
    expect(wire.date).toBe("");
    expect(wire.status).toBeNull();
    expect(wire.tags).toEqual([]);
    expect(wire.concepts).toEqual([]);
    expect(wire.connections).toEqual([]);
  });

  it("a note with no title returns title as an empty string and validates", async () => {
    const wire = await wireOf("notes/untitled.md");
    expect(wire.title).toBe("");
    expect(wire.date).toBe("2026-09-24");
    expect(wire.status).toBe("draft");
  });

  it("a note with neither title, date nor status returns \"\", \"\" and null, and validates", async () => {
    const wire = await wireOf("notes/bare.md");
    expect([wire.title, wire.date, wire.status]).toEqual(["", "", null]);
    expect(wire.tags).toEqual(["a"]);
  });

  it("an absent date and an explicitly empty date are not distinguishable in the result", async () => {
    // Pins the ambiguity the issue describes: a client sees "" for both.
    const absent = await wireOf("concepts/ml.md");
    const explicit = await wireOf("notes/empty-date.md");
    expect(absent.date).toBe("");
    expect(explicit.date).toBe("");
  });
});
