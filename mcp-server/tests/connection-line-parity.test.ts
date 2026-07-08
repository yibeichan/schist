/**
 * TS CONNECTION_RE + parseConnections (markdown-parser.ts) and Python
 * CONNECTION_RE + parse_connections (ingest.py) must agree at BOTH levels:
 *   - `lines`  — per-line regex parity (#338)
 *   - `bodies` — whole-body line-SPLITTER parity (#359): Python ingest
 *                iterates body.splitlines() (breaks on CR/VT/FF/FS/GS/RS/NEL/
 *                LS/PS, not just \n), so a TS reader that split only on \n let
 *                a bogus type smuggled across such a separator bypass the #317
 *                vocabulary check while ingest still indexed the edge.
 * Python's parse builds the index; TS's parse drives get_note/query_graph,
 * delete_note's cascade, and write-time validation — any skew silently
 * diverges the graph from what the tools believe is in it.
 * schema/connection-line-parity.json is the single source of truth, consumed
 * here and by cli/tests/test_ingest.py. Modeled on the #318 concept-slug contract.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CONNECTION_RE, parseConnections } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type LineCase = {
  name: string;
  line: string;
  expected: { type: string; target: string; context: string | null } | null;
};
type BodyEdge = { type: string; target: string; context: string | null };
type BodyCase = { name: string; body: string; edges: BodyEdge[] };
type ParityFixture = { lines: LineCase[]; bodies: BodyCase[] };

function loadFixture(): ParityFixture {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "connection-line-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as ParityFixture;
}

const fixture = loadFixture();

describe("connection-line regex parity (#338)", () => {
  test("fixture is present and non-trivial", () => {
    expect(fixture.lines.length).toBeGreaterThanOrEqual(15);
  });

  for (const c of fixture.lines) {
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

describe("connection-body splitter parity (#359)", () => {
  test("fixture is present and non-trivial", () => {
    expect(fixture.bodies.length).toBeGreaterThanOrEqual(15);
  });

  for (const c of fixture.bodies) {
    test(c.name, () => {
      const got = parseConnections(c.body).map((e) => ({
        type: e.type,
        target: e.target,
        context: e.context ?? null,
      }));
      expect(got).toEqual(c.edges);
    });
  }

  test("splitLinesLikePython is linear on a 1M-char body", async () => {
    const { splitLinesLikePython } = await import("../src/markdown-parser.js");
    const big = "x".repeat(1_000_000);
    const t0 = performance.now();
    expect(splitLinesLikePython(big)).toEqual([big]); // no boundaries → one line
    expect(performance.now() - t0).toBeLessThan(2000); // linear is a few ms
  });
});
