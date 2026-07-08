/**
 * TS CONNECTION_RE (markdown-parser.ts) and Python CONNECTION_RE (ingest.py)
 * must agree on every line — Python's ingest builds the index from its parse
 * while TS's parse drives delete_note's cascade and write-time validation, so
 * any skew silently diverges the graph from what the tools believe is in it
 * (#338). schema/connection-line-parity.json is the single source of truth,
 * consumed here and by cli/tests/test_ingest.py. Modeled on the #318
 * concept-slug parity contract.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CONNECTION_RE } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type ConnectionParityCase = {
  name: string;
  line: string;
  expected: { type: string; target: string; context: string | null } | null;
};

function loadConnectionParityCases(): ConnectionParityCase[] {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "connection-line-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as ConnectionParityCase[];
}

describe("connection-line parity (#338)", () => {
  const cases = loadConnectionParityCases();

  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(15);
  });

  for (const c of cases) {
    test(c.name, () => {
      const m = c.line.match(CONNECTION_RE);
      if (c.expected === null) {
        expect(m).toBeNull();
      } else {
        expect(m).not.toBeNull();
        expect(m![1]).toBe(c.expected.type);
        expect(m![2]).toBe(c.expected.target);
        // Same quoted-context || em-dash-context || nothing combination
        // parseConnections uses (and Python's `or` chain mirrors).
        expect(m![3] || m![4] || null).toBe(c.expected.context);
      }
    });
  }
});
