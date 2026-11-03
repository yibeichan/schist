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
import { connectionBody, insertConnectionLine, parseConnections, parseNote, scanNoteConnections } from "../src/markdown-parser.js";
import { bodyConnectionEdgeLines, stripConnectionsTo } from "../src/tools.js";

const M = 1_000_000;
const n = (x: number) => Math.floor(x);
// Each case builds a whole note at size m, so a step can be timed at two sizes.
const CASES: Record<string, (m: number) => string> = {
  "chained processing instructions": (m) => "## Connections\n" + "<? ?>".repeat(n(m / 5)) + "\n- extends: a\n",
  "chained CDATA": (m) => "## Connections\n" + "<![CDATA[]]>".repeat(n(m / 12)) + "\n",
  "edge line with a long space run": (m) => "## Connections\n- extends: a" + " ".repeat(m) + "x\n",
  "edge line with a colon chain": (m) => "## Connections\n- a:" + "b:".repeat(n(m / 2)) + "\n",
  "edge line with an unclosed quote": (m) => '## Connections\n- a: b "' + "x".repeat(m) + "\n",
  "edge line with many quote pairs": (m) => "## Connections\n- a: b" + ' "'.repeat(n(m / 2)) + "\n",
  "bracket line with escapes": (m) => "[" + "\\x".repeat(n(m / 2)) + "\n## Connections\n- extends: a\n",
  "rule-like line ending in a letter": (m) => "## Connections\n" + "-".repeat(m) + "x\n",
  "fence opener with a long info word": (m) => "```" + "a".repeat(m) + "!\n## Connections\n- extends: a\n",
  "long run of backticks": (m) => "`".repeat(m) + "x!\n## Connections\n",
  "long indentation before a fence": (m) => " ".repeat(m) + "```x\n## Connections\n",
  "many edges": (m) => "## Connections\n" + "- extends: a\n".repeat(n(m / 13)),
  "unclosed fence over many lines": (m) => "```\n" + "x\n".repeat(n(m / 2)),
};
// The frontmatter hashtag patch (parseNote) on one long flow-collection line.
const HASHTAGS = (m: number) => "---\ntags: [" + "#,".repeat(n(m / 2)) + "]\n---\nx\n";

function time(fn: () => unknown): number {
  const t0 = performance.now();
  try {
    fn();
  } catch {
    // A refused write is fine; only its cost is measured.
  }
  return performance.now() - t0;
}

/**
 * Fastest of five runs of each step, after one warm-up run, at a quarter of
 * the size and at full size. A note four times larger takes about four times
 * as long for linear work and sixteen times for quadratic work, so 8x
 * separates them with room for a loaded runner. A step is judged on growth
 * only when its quarter-size time is at least 20 ms: shorter timings are
 * mostly noise (a 10 ms step read as 8x on a CI runner), and the garbage
 * collector cannot be paused here as it is in test_ingest.py. Every step is
 * held to the 5 s backstop. A fixed one-second ceiling alone failed on
 * runner speed.
 */
type Steps = Record<string, () => unknown>;
function expectLinear(stepsAt: (m: number) => Steps): void {
  const bestOf5 = (steps: Steps) =>
    Object.fromEntries(
      Object.entries(steps).map(([k, fn]) => {
        time(fn);
        return [k, Math.min(time(fn), time(fn), time(fn), time(fn), time(fn))];
      }),
    );
  const quarter = bestOf5(stepsAt(n(M / 4)));
  const full = bestOf5(stepsAt(M));
  const slow = Object.fromEntries(Object.entries(full).filter(([, ms]) => ms >= 5000));
  expect(slow).toEqual({});
  const superlinear = Object.fromEntries(
    Object.entries(full)
      .filter(([k]) => quarter[k] >= 20)
      .map(([k, ms]) => [k, Math.round((ms / quarter[k]) * 10) / 10])
      .filter(([, g]) => (g as number) >= 8),
  );
  expect(superlinear).toEqual({});
}

test("the frontmatter hashtag patch is linear on a 1 MB flow list", () => {
  expectLinear((m) => {
    const text = HASHTAGS(m);
    return { parseNote: () => parseNote(text) };
  });
}, 30_000);

describe("the connection rule is linear on 1 MB adversarial notes", () => {
  for (const [name, build] of Object.entries(CASES)) {
    test(name, () => {
      expectLinear((m) => {
        const text = "---\ntitle: t\n---\n" + build(m);
        return {
          parse: () => parseConnections(connectionBody(text)),
          scan: () => scanNoteConnections(text),
          insert: () => insertConnectionLine(text, "- extends: new"),
          strip: () => stripConnectionsTo(text, ["a"]),
          validator: () => [...bodyConnectionEdgeLines(connectionBody(text))],
          parseNote: () => parseNote(text),
        };
      });
    }, 30_000);
  }
});
