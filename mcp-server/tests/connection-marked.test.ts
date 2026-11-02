// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The connection rule against the viewer's renderer. The viewer
 * (cli/schist/viewer/index.html) renders note bodies with marked 12.0.2,
 * pinned here as a devDependency at the same version. Every edge the
 * connection rule reads must be, in marked's tokens, a top-level list item
 * under a level-2 heading whose text is "Connections" (no other heading in
 * between), whose first token is text or a paragraph whose first line is
 * the edge. ingest.py and markdown-parser.ts read the same edges (the parity
 * fixtures pin that), so checking markdown-parser.ts covers both readers.
 *
 * Bodies: every body and file case in schema/connection-line-parity.json,
 * plus a seeded random generator built from the constructs that change how
 * marked groups lines.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { marked, type Token, type Tokens } from "marked";
import {
  CONNECTION_RE,
  connectionBody,
  parseConnections,
  trimConnectionWs,
} from "../src/markdown-parser.js";
import type { Connection } from "../src/types.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(path.resolve(__dirname, "..", "..", "schema", "connection-line-parity.json"), "utf-8"),
) as { bodies: { name: string; body: string }[]; files: { name: string; text: string }[] };

const viewerPage = readFileSync(
  path.resolve(__dirname, "..", "..", "cli", "schist", "viewer", "index.html"),
  "utf-8",
);

function key(c: Connection): string {
  return `${c.type}\u0000${c.target}\u0000${c.context ?? ""}`;
}

/** The edges marked shows as top-level list items under "## Connections". */
function markedEdges(body: string): Map<string, number> {
  const out = new Map<string, number>();
  const tokens = marked.lexer(body, { gfm: true, breaks: false });
  let underConnections = false;
  for (const t of tokens as Token[]) {
    if (t.type === "heading") {
      const h = t as Tokens.Heading;
      underConnections = h.depth === 2 && h.text === "Connections";
      continue;
    }
    if (t.type !== "list" || !underConnections) continue;
    for (const item of (t as Tokens.List).items) {
      const first = item.tokens[0];
      if (first === undefined || (first.type !== "text" && first.type !== "paragraph")) continue;
      if (item.task) continue;
      const line = (first as Tokens.Text).text.split("\n")[0];
      const m = trimConnectionWs(`- ${line}`).match(CONNECTION_RE);
      if (!m) continue;
      const k = key({ type: m[1], target: m[2], context: m[3] || m[4] || undefined });
      out.set(k, (out.get(k) ?? 0) + 1);
    }
  }
  return out;
}

/** Edges the rule reads that marked does not show (empty when they agree). */
function unseen(body: string): string[] {
  const shown = markedEdges(body);
  const missing: string[] = [];
  for (const c of parseConnections(body)) {
    const k = key(c);
    const n = shown.get(k) ?? 0;
    if (n === 0) missing.push(`${c.type}: ${c.target}`);
    else shown.set(k, n - 1);
  }
  return missing;
}

test("marked here is the version the viewer loads", () => {
  const pkg = JSON.parse(
    readFileSync(path.resolve(__dirname, "..", "node_modules", "marked", "package.json"), "utf-8"),
  ) as { version: string };
  expect(pkg.version).toBe("12.0.2");
  expect(viewerPage).toContain("npm/marked@12.0.2/");
});

describe("every parity body: each edge read is a list item marked shows under Connections", () => {
  for (const c of fixture.bodies) {
    test(c.name, () => {
      expect(unseen(c.body)).toEqual([]);
    });
  }
  for (const c of fixture.files) {
    test(`file: ${c.name}`, () => {
      expect(unseen(connectionBody(c.text))).toEqual([]);
    });
  }
});

// Lines that change how marked groups the lines around them, and ordinary
// note lines between them.
const POOL = [
  "## Connections", "## Connections", "## Connections  ", " ## Connections", "## Connections\t",
  "- extends: a", "- supports: b \"ctx\"", "- extends: c — dash", "-  extends: d", "-   extends: e",
  "-    extends: f", "-     extends: g", "-\textends: h", "- \textends: i", " - extends: j", "  - extends: k",
  "   - extends: l", "* extends: m", "+ extends: n", "1. extends: o", "- [x]: tgt", "- [ ] extends: p",
  "- <x: y", "- >q: r", "- ```: s", "- extends: [ref]", "- #x: y",
  "```", "```js", "``` ", "~~~", "~~~~", "````", "  ```", "    ```", "```~~~", "~~~`", "``` x y", "\t```",
  "<div>", "<div", "</div>", "<!--", "-->", "<!-- x -->- a", "<?", "?>", "<?>", "<? ?>```", "<!X >```",
  "<![CDATA[ ]]>```", "<meta x", "<pre", "</pre>", "<pre x", "<a_b>", "</b>", "<x-y>", "<style",
  "[x]: /u \"", "[x]: /u", "\"", "'", "(", ")", "[x](y)", "[x] y", "[a\\]", "[[w]]", "[open",
  "> quote", ">", "> ```", "> - extends: q",
  "---", "--", "-", "===", "=", "***", "* * *", "- - -", "___", "|-|-|", ":-:", "-|-", "a | b", "| a |",
  "text", "more text", "", "", "", "  ", "\t", "    code", "      deep", "\tcode",
  "## Other", "#\tOther", "##", "## ", "# H1", "### Sub", "#tag", "####### seven", "  ## Indented",
  "#\u00a0nbsp", "Other", "1. item", "1.     x", "2) y", "* item", "+ x", "1.",
  "\u0085```", "a\u2028b", "\u000c", "\u00a0", "\u3000", "\u00a0```", "\ufeff## Connections",
];

/** mulberry32: a small seeded generator, so a failure reproduces. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Ordinary lines, drawn more often so that many bodies keep their edges and
// the risky lines meet them.
const PLAIN = [
  "- extends: a", "- supports: b \"ctx\"", "- refines: c", "text", "more text", "", "", "- item",
  "  continued", "1. item", "* item", "[x](y) text", "> quote", "---", "```", "```py", "x = 1",
];

function randomBody(next: () => number): string {
  const n = 2 + Math.floor(next() * 10);
  const lines: string[] = [];
  // Most bodies get a heading early, so the section is exercised.
  if (next() < 0.7) lines.push("## Connections");
  for (let i = 0; i < n; i++) {
    const pool = next() < 0.75 ? PLAIN : POOL;
    lines.push(pool[Math.floor(next() * pool.length)]);
  }
  return lines.join("\n") + (next() < 0.5 ? "\n" : "");
}

test("random bodies: each edge read is a list item marked shows under Connections", () => {
  const failures: { body: string; unseen: string[] }[] = [];
  let withEdges = 0;
  for (let seed = 1; seed <= 20000; seed++) {
    const body = randomBody(rng(seed));
    if (parseConnections(body).length > 0) withEdges++;
    const u = unseen(body);
    if (u.length > 0) failures.push({ body, unseen: u });
  }
  expect(failures.slice(0, 5)).toEqual([]);
  // The generator must reach the interesting case often enough to mean
  // something.
  expect(withEdges).toBeGreaterThan(1000);
});
