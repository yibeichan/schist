// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Adversarial note bodies of about 1 MB, most of them one line: the
 * connection rule must read each in linear time. The MCP server runs these
 * paths synchronously inside get_note, add_connection, update_note and the
 * delete cascade, so a slow one stalls the event loop. cli/tests/test_ingest.py
 * runs the same set against ingest.py.
 */
import { connectionBody, insertConnectionLine, parseConnections, scanNoteConnections } from "../src/markdown-parser.js";
import { bodyConnectionEdgeLines, stripConnectionsTo } from "../src/tools.js";

const M = 1_000_000;
const CASES: Record<string, string> = {
  "chained processing instructions": "## Connections\n" + "<? ?>".repeat(M / 5) + "\n- extends: a\n",
  "chained CDATA": "## Connections\n" + "<![CDATA[]]>".repeat(Math.floor(M / 12)) + "\n",
  "edge line with a long space run": "## Connections\n- extends: a" + " ".repeat(M) + "x\n",
  "edge line with a colon chain": "## Connections\n- a:" + "b:".repeat(M / 2) + "\n",
  "edge line with an unclosed quote": '## Connections\n- a: b "' + "x".repeat(M) + "\n",
  "edge line with many quote pairs": "## Connections\n- a: b" + ' "'.repeat(M / 2) + "\n",
  "bracket line with escapes": "[" + "\\x".repeat(M / 2) + "\n## Connections\n- extends: a\n",
  "rule-like line ending in a letter": "## Connections\n" + "-".repeat(M) + "x\n",
  "fence opener with a long info word": "```" + "a".repeat(M) + "!\n## Connections\n- extends: a\n",
  "long run of backticks": "`".repeat(M) + "x!\n## Connections\n",
  "long indentation before a fence": " ".repeat(M) + "```x\n## Connections\n",
  "many edges": "## Connections\n" + "- extends: a\n".repeat(Math.floor(M / 13)),
  "unclosed fence over many lines": "```\n" + "x\n".repeat(M / 2),
};

function time(fn: () => unknown): number {
  const t0 = performance.now();
  try {
    fn();
  } catch {
    // A refused write is fine; only its cost is measured.
  }
  return performance.now() - t0;
}

describe("the connection rule is linear on 1 MB adversarial notes", () => {
  for (const [name, body] of Object.entries(CASES)) {
    test(name, () => {
      const text = "---\ntitle: t\n---\n" + body;
      const steps: Record<string, number> = {
        parse: time(() => parseConnections(connectionBody(text))),
        scan: time(() => scanNoteConnections(text)),
        insert: time(() => insertConnectionLine(text, "- extends: new")),
        strip: time(() => stripConnectionsTo(text, ["a"])),
        validator: time(() => [...bodyConnectionEdgeLines(connectionBody(text))]),
      };
      const slow = Object.fromEntries(Object.entries(steps).filter(([, ms]) => ms >= 1000));
      expect(slow).toEqual({});
    });
  }
});
