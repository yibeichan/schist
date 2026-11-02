// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Each reading of the connection rule (CommonMark, marked, marked with any
 * HTML interrupting a paragraph) must skip the same lines as
 * cli/schist/ingest.py connection_line_readings.
 * schema/connection-reading-parity.json is the single source of truth.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { connectionLineReadings, splitConnectionLines } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type ReadingCase = {
  name: string;
  body: string;
  start: number;
  commonmark: number[];
  marked: number[];
  marked_any_html: number[];
};
const cases = (JSON.parse(
  readFileSync(path.resolve(__dirname, "..", "..", "schema", "connection-reading-parity.json"), "utf-8"),
) as { cases: ReadingCase[] }).cases;

describe("connection rule readings parity", () => {
  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(10);
  });

  for (const c of cases) {
    test(c.name, () => {
      const got = connectionLineReadings(splitConnectionLines(c.body), c.start).map((r) =>
        r.flatMap((skipped, i) => (skipped ? [i] : [])),
      );
      expect(got).toEqual([c.commonmark, c.marked, c.marked_any_html]);
    });
  }
});
