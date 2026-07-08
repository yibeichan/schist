/**
 * TS titleSlug (tools.ts slugify/rawSlug core) and Python markdown_io.slugify
 * must produce byte-identical slugs — the slug is embedded in the note id
 * (filename), so any skew means the two languages mint different ids for the
 * same title (#338). schema/title-slug-parity.json is the single source of
 * truth, consumed here and by cli/tests/test_markdown_io.py. Modeled on the
 * #318 concept-slug parity contract.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { titleSlug } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type SlugParityCase = { name: string; input: string; slug: string };

function loadSlugParityCases(): SlugParityCase[] {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "title-slug-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as SlugParityCase[];
}

describe("title-slug parity (#338)", () => {
  const cases = loadSlugParityCases();

  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(20);
  });

  for (const c of cases) {
    test(c.name, () => {
      expect(titleSlug(c.input)).toBe(c.slug);
    });
  }

  test("slugging is linear, not quadratic, on huge whitespace runs", () => {
    // Whitespace collapse is a single [class]+ pass and the edge-dash strip
    // is an index scan — never a `^[ws]+|[ws]+$` alternated anchored regex,
    // which backtracks quadratically over interior runs (minutes at this
    // size). `title` reaches this with no length validation on a
    // single-threaded server.
    const big = "a" + " ".repeat(1_000_000) + "b";
    const t0 = performance.now();
    expect(titleSlug(big)).toBe("a-b");
    expect(performance.now() - t0).toBeLessThan(5000); // linear is ~10 ms
  });
});
