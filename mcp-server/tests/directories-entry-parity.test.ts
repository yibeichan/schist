// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * loadVaultConfig and the CLI's commands._directories must resolve a list-form
 * `directories:` in schist.yaml to the same names, or `schist add-concept`
 * accepts a directory that MCP create_concept rejects (#469).
 * schema/directories-entry-parity.json is the single source of truth, consumed
 * here and by cli/tests/test_commands.py.
 */
import * as fs from "fs/promises";
import { readFileSync } from "node:fs";
import * as os from "os";
import * as path from "path";
import { fileURLToPath } from "node:url";
import { loadVaultConfig } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type Case = { name: string; entries: string[]; dirs: string[] };

const cases = JSON.parse(
  readFileSync(
    path.resolve(__dirname, "..", "..", "schema", "directories-entry-parity.json"),
    "utf-8",
  ),
) as Case[];

describe("directories entry parity (#469)", () => {
  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(8);
  });

  for (const c of cases) {
    test(c.name, async () => {
      const vault = await fs.mkdtemp(path.join(os.tmpdir(), "schist-dirs-parity-"));
      try {
        // JSON is valid YAML flow syntax.
        await fs.writeFile(
          path.join(vault, "schist.yaml"),
          `directories: ${JSON.stringify(c.entries)}\n`,
        );
        expect((await loadVaultConfig(vault)).directories).toEqual(c.dirs);
      } finally {
        await fs.rm(vault, { recursive: true, force: true });
      }
    });
  }
});
