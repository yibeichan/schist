// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * hasNonYamlOpener (markdown-parser.ts) mirrors the hub's `_bad_opener`
 * (cli/schist/pre_receive.py). A writer that keeps a note's first line uses
 * it to avoid committing a note the hub refuses, which would block every
 * later push from the spoke. schema/frontmatter-opener-parity.json is the
 * single source of truth, consumed here and by cli/tests/test_pre_receive.py.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { hasNonYamlOpener } from "../src/markdown-parser.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

type OpenerCase = { name: string; text: string; refused: boolean };

const cases = (JSON.parse(
  readFileSync(path.resolve(__dirname, "..", "..", "schema", "frontmatter-opener-parity.json"), "utf-8"),
) as { cases: OpenerCase[] }).cases;

describe("frontmatter opener parity with the hub", () => {
  test("the corpus covers both verdicts", () => {
    expect(cases.filter((c) => c.refused).length).toBeGreaterThanOrEqual(10);
    expect(cases.filter((c) => !c.refused).length).toBeGreaterThanOrEqual(10);
  });

  test.each(cases.map((c) => [c.name, c] as const))("%s", (_name, c) => {
    expect(hasNonYamlOpener(c.text)).toBe(c.refused);
  });
});
