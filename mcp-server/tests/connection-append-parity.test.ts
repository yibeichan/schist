/**
 * TS insertConnectionLine (markdown-parser.ts) and Python
 * markdown_io.insert_connection_line must produce byte-identical output —
 * both writers feed the same ## Connections sections that BOTH readers
 * (parseConnections / ingest.py parse_connections) index, so any skew means
 * an edge written by one language silently vanishes for the other (#365 the
 * Python split('\n') write path, #366 the TS CRLF-blind insert regex).
 * schema/connection-append-parity.json is the single source of truth,
 * consumed here and by cli/tests/test_markdown_io.py. Modeled on the
 * #318/#338 slug parity contracts.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { insertConnectionLine } from "../src/markdown-parser.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type AppendParityCase = { name: string; content: string; line: string; expected: string };

function loadAppendParityCases(): AppendParityCase[] {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "connection-append-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as AppendParityCase[];
}

describe("connection-append parity (#365/#366)", () => {
  const cases = loadAppendParityCases();

  test("fixture is present and non-trivial", () => {
    // An emptied/mangled fixture must fail loudly, not zero-iterate green.
    expect(cases.length).toBeGreaterThanOrEqual(12);
  });

  for (const c of cases) {
    test(c.name, () => {
      expect(insertConnectionLine(c.content, c.line)).toBe(c.expected);
    });
  }
});
