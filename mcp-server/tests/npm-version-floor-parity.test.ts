// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * mcp-server/package.json `devEngines.packageManager.version` sets the npm
 * floor for every npm command run in mcp-server/ (npm before 11.11 rewrites
 * package-lock.json without its `libc` fields). Node 22 bundles npm 10, so the
 * floor has to be written out wherever a reader is told to build or test the
 * server, and in `schist doctor`'s rebuild advice. This test makes devEngines
 * the authority and those documents assertions about it, as
 * node-version-floor-parity.test.ts does for engines.node.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REPO = path.resolve(__dirname, "..", "..");

/** The authority, as "major.minor". */
function declaredFloor(): string {
  const pkg = JSON.parse(
    readFileSync(path.join(REPO, "mcp-server", "package.json"), "utf-8"),
  ) as { devEngines?: { packageManager?: { name?: string; version?: string } } };
  const pm = pkg.devEngines?.packageManager;
  if (pm?.name !== "npm" || !pm.version) {
    throw new Error("mcp-server/package.json has no devEngines.packageManager for npm");
  }
  const m = /^>=\s*(\d+)\.(\d+)/.exec(pm.version);
  if (!m) throw new Error(`cannot read a floor from devEngines version = ${pm.version}`);
  return `${m[1]}.${m[2]}`;
}

// CHANGELOG.md is absent on purpose: it records history and may name
// superseded floors.
const DOCS = [
  "README.md",
  "CONTRIBUTING.md",
  "AGENTS.md",
  "docs/getting-started.md",
  "docs/mcp-setup.md",
  "docs/cross-project-memory.md",
  "docs/RELEASE.md",
  "cli/schist/doctor.py",
];

const SHAPES: { label: string; re: RegExp }[] = [
  { label: "prose floor (npm >= N.M / ≥ N.M)", re: /\bnpm\s*(?:>=|≥)\s*(\d+\.\d+)/g },
  { label: "requirements table row", re: /\|\s*npm\s*\|\s*>=\s*(\d+\.\d+)/g },
  { label: "doctor MIN_NPM", re: /MIN_NPM\s*=\s*\((\d+),\s*(\d+)\)/g },
];

describe("npm version floor parity", () => {
  const floor = declaredFloor();

  test("devEngines npm floor is readable and sane", () => {
    expect(Number(floor.split(".")[0])).toBeGreaterThanOrEqual(11);
  });

  for (const rel of DOCS) {
    test(`${rel} states only the declared npm floor`, () => {
      const text = readFileSync(path.join(REPO, rel), "utf-8");
      const wrong: string[] = [];
      let matched = 0;
      for (const { label, re } of SHAPES) {
        for (const m of text.matchAll(new RegExp(re))) {
          matched += 1;
          const stated = m[2] === undefined ? m[1] : `${m[1]}.${m[2]}`;
          if (stated !== floor) {
            const line = text.slice(0, m.index).split("\n").length;
            wrong.push(`${rel}:${line} [${label}] says ${stated}, devEngines says ${floor}: ${m[0]}`);
          }
        }
      }
      expect(wrong).toEqual([]);
      // Each file must state the floor at least once; an empty match set
      // would pass the assertion above vacuously.
      expect(matched).toBeGreaterThanOrEqual(1);
    });
  }
});
