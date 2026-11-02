// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/** The MCP server's connection rule version equals the CLI's, so `schist
 * doctor` can compare a built server with the installed CLI. */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { CONNECTIONS_RULE_VERSION } from "../src/markdown-parser.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

test("CONNECTIONS_RULE_VERSION matches cli/schist/ingest.py", () => {
  const ingest = readFileSync(path.resolve(__dirname, "..", "..", "cli", "schist", "ingest.py"), "utf-8");
  const m = ingest.match(/^CONNECTIONS_RULE_VERSION = (\d+)$/m);
  expect(m).not.toBeNull();
  expect(CONNECTIONS_RULE_VERSION).toBe(Number(m![1]));
});
