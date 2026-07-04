/**
 * TS normalizeConceptSlug and Python _normalize_concept_slug must produce
 * byte-identical slugs — the index stores Python's output, delete_note's
 * cascade compares against TS's, and any skew silently leaves dangling refs
 * (#303). schema/concept-slug-parity.json is the single source of truth,
 * consumed here and by cli/tests/test_ingest.py (#318).
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { normalizeConceptSlug } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type SlugParityCase = { name: string; input: string; slug: string };

function loadSlugParityCases(): SlugParityCase[] {
  const fixturePath = path.resolve(__dirname, "..", "..", "schema", "concept-slug-parity.json");
  return JSON.parse(readFileSync(fixturePath, "utf-8")) as SlugParityCase[];
}

describe("concept-slug parity (#318)", () => {
  const cases = loadSlugParityCases();

  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(20);
  });

  for (const c of cases) {
    test(c.name, () => {
      expect(normalizeConceptSlug(c.input)).toBe(c.slug);
    });
  }
});
