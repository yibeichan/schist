// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * markdown-parser.ts frontmatterLength and cli/schist/ingest.py
 * note_body_offset must agree on where a note's body starts, so the MCP
 * server reads, writes and repairs `## Connections` sections on exactly the
 * lines ingest indexes. schema/frontmatter-boundary-parity.json is the single
 * source of truth, consumed here and by cli/tests/test_ingest.py.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PY_WHITESPACE, connectionBody, frontmatterLength } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type BoundaryCase = { name: string; text: string; body: string };
const fixture = JSON.parse(
  readFileSync(path.resolve(__dirname, "..", "..", "schema", "frontmatter-boundary-parity.json"), "utf-8"),
) as { python_whitespace: string; cases: BoundaryCase[] };

describe("frontmatter boundary parity", () => {
  test("fixture is present and non-trivial", () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(15);
  });

  test("PY_WHITESPACE is Python's str.isspace() set", () => {
    expect(PY_WHITESPACE).toBe(fixture.python_whitespace);
  });

  for (const c of fixture.cases) {
    test(c.name, () => {
      expect(c.text.slice(frontmatterLength(c.text))).toBe(c.body);
      expect(connectionBody(c.text)).toBe(c.body);
    });
  }
});
