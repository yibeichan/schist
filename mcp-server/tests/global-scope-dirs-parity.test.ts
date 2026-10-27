// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The MCP server's scopeDirsFromConfig (sync_status's ignore probe) and the
 * CLI's git_ops.scope_dirs_from_config (`schist sync push` staging) must
 * read a vault's schist.yaml `directories` the same way, or sync_status
 * reports a clean tree for a directory the push stages and then refuses
 * (#734). schema/global-scope-dirs-parity.json is the single source of
 * truth, consumed here and by cli/tests/test_git_ops.py.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { scopeDirsFromConfig } from "../src/tools.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

type ScopeDirsCase = { name: string; config: unknown; dirs: string[] | null };

const cases = JSON.parse(
  readFileSync(
    path.resolve(__dirname, "..", "..", "schema", "global-scope-dirs-parity.json"),
    "utf-8",
  ),
) as ScopeDirsCase[];

describe("global scope directories parity (#734)", () => {
  test("fixture is present and non-trivial", () => {
    expect(cases.length).toBeGreaterThanOrEqual(10);
  });

  for (const c of cases) {
    test(c.name, () => {
      expect(scopeDirsFromConfig(c.config)).toEqual(c.dirs);
    });
  }
});
