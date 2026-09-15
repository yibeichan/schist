/**
 * The declared Node floor lives in mcp-server/package.json `engines.node`, but
 * it is also WRITTEN OUT in prose, in a requirements table, in two NodeSource
 * `setup_NN.x` lines, in three `nvm install NN` lines, and in a Singularity
 * definition's `From: node:NN-bookworm-slim` base image. Sixteen places, none
 * of them enforced.
 *
 * They drifted exactly the way that invites: better-sqlite3 v13 raised
 * `engines.node` from `>=20` to `>=22`, `package.json` was updated with the
 * dependency bump, and all sixteen documents kept saying 20 — including the
 * container base image an operator provisions against, which would have built
 * a Node 20 image against a dependency that no longer supports it. CI could
 * not see it: CI runs Node 22, so the install it performs is the supported one.
 *
 * This test makes `engines.node` the single authority and the documents
 * assertions about it, so the next floor change either updates them or fails
 * here. It deliberately reads the major from package.json rather than hardcoding
 * 22 — a test that hardcodes the number is a seventeenth place to drift.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO = path.resolve(__dirname, "..", "..");

/** The authority. */
function declaredMajor(): number {
  const pkg = JSON.parse(
    readFileSync(path.join(REPO, "mcp-server", "package.json"), "utf-8"),
  ) as { engines?: { node?: string } };
  const spec = pkg.engines?.node;
  if (!spec) throw new Error("mcp-server/package.json has no engines.node");
  const m = /(\d+)/.exec(spec);
  if (!m) throw new Error(`cannot read a major from engines.node = ${spec}`);
  return Number(m[1]);
}

// Every file that states the floor for a human or a provisioning script.
// CHANGELOG.md is deliberately absent: it records the floor's HISTORY, so it
// legitimately contains superseded numbers (the >=22 -> >=20 relaxation, and
// the raise back) and must never be swept.
const DOCS = [
  "README.md",
  "CONTRIBUTING.md",
  "AGENTS.md",
  "docs/getting-started.md",
  "docs/hub-spoke-pi-orcd-dragonfly.md",
];

// Each shape the number is written in. Kept as named patterns so a failure
// says WHICH form drifted, not just that some digit somewhere is wrong.
const SHAPES: { label: string; re: RegExp }[] = [
  { label: "prose floor (Node.js >= N / ≥ N)", re: /Node\.js\s*(?:>=|≥)\s*(\d+)/g },
  { label: "prose floor (Node N+)", re: /\bNode\s+(\d+)\+/g },
  { label: "requirements table row", re: /\|\s*Node\.js\s*\|\s*>=\s*(\d+)\s*\|/g },
  { label: "NodeSource setup script", re: /setup_(\d+)\.x/g },
  { label: "nvm install", re: /nvm install\s+(\d+)/g },
  { label: "container base image", re: /node:(\d+)[-.]/g },
];

describe("Node version floor parity (#498 aftermath)", () => {
  const major = declaredMajor();

  test("engines.node is readable and sane", () => {
    expect(Number.isInteger(major)).toBe(true);
    // A floor below 18 means the regex grabbed the wrong digits (a patch
    // level, say) rather than that the project supports Node 12.
    expect(major).toBeGreaterThanOrEqual(18);
  });

  test("the corpus is non-trivial", () => {
    // Guards the vacuous pass: if the doc list or the patterns stop matching
    // anything, every assertion below succeeds over an empty set and the
    // drift this test exists to catch sails through (#600's shape).
    const found = DOCS.flatMap((rel) => {
      const text = readFileSync(path.join(REPO, rel), "utf-8");
      return SHAPES.flatMap(({ re }) => [...text.matchAll(new RegExp(re))]);
    });
    expect(found.length).toBeGreaterThanOrEqual(12);
  });

  for (const rel of DOCS) {
    test(`${rel} states only the declared floor`, () => {
      const text = readFileSync(path.join(REPO, rel), "utf-8");
      const wrong: string[] = [];
      for (const { label, re } of SHAPES) {
        for (const m of text.matchAll(new RegExp(re))) {
          if (Number(m[1]) !== major) {
            const line = text.slice(0, m.index).split("\n").length;
            wrong.push(`${rel}:${line} [${label}] says ${m[1]}, engines.node says ${major}: ${m[0]}`);
          }
        }
      }
      expect(wrong).toEqual([]);
    });
  }
});
