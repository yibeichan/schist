// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The connection rule against the viewer's rendered page, not only marked's
 * tokens. The Markdown rendering code of cli/schist/viewer/index.html (the
 * block between its "Markdown rendering" markers: marked 12.0.2 with raw HTML
 * shown as text, then DOMPurify 3.2.6 with the viewer's settings) runs here
 * on jsdom, and the result is put into a container with innerHTML, as the
 * viewer does. Every edge the rule reads must be a list item of a top-level
 * list whose nearest preceding heading, in document order, is an h2 whose
 * text is "Connections".
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import createDOMPurify from "dompurify";
import * as markedModule from "marked";
import { CONNECTION_RE, connectionBody, parseConnections, trimConnectionWs } from "../src/markdown-parser.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..", "..");
const page = readFileSync(path.join(root, "cli", "schist", "viewer", "index.html"), "utf-8");
const fixture = JSON.parse(readFileSync(path.join(root, "schema", "connection-line-parity.json"), "utf-8")) as {
  bodies: { name: string; body: string }[];
  files: { name: string; text: string }[];
};

const START = "// ── Markdown rendering (";
const END = "// ── end of Markdown rendering";

const dom = new JSDOM("<!doctype html><html><body><div id=\"c\"></div></body></html>");
const win = dom.window as unknown as Window & { marked: unknown; DOMPurify: unknown };
const purify = createDOMPurify(dom.window as unknown as Parameters<typeof createDOMPurify>[0]);
win.marked = markedModule;
win.DOMPurify = purify;

function viewerRenderer(): (text: string) => string {
  const a = page.indexOf(START);
  const b = page.indexOf(END);
  if (a === -1 || b === -1 || b < a) throw new Error("viewer rendering markers not found in index.html");
  const block = page.slice(a, b);
  // The block reads `window.marked`, `marked` and `DOMPurify`, as in the page.
  return new Function("window", "marked", "DOMPurify", `${block}\nreturn markdownToHtml;`)(
    win, markedModule, purify,
  ) as (text: string) => string;
}
const render = viewerRenderer();
const container = dom.window.document.getElementById("c")!;

// Inline Markdown in a target (a code span, emphasis, an escape) renders
// without its markers, so targets are compared with those characters left out.
function key(type: string, target: string): string {
  return `${type}\u0000${target.replace(/[`*_~\\]/g, "")}`;
}

const BLOCK = /^(P|UL|OL|H[1-6]|PRE|BLOCKQUOTE|TABLE|HR|DIV|DETAILS)$/;

/** The item's own leading text: its first paragraph, or its text before the
 * first block element (a nested list or heading is not part of the edge). */
function itemText(li: Element): string {
  let text = "";
  for (const node of Array.from(li.childNodes)) {
    if (node.nodeType === 1) {
      const tag = (node as Element).tagName;
      if (tag === "BR") break; // a hard line break ends the line
      if (BLOCK.test(tag)) {
        // A loose item's text is in its first paragraph.
        if (text.trim() === "" && tag === "P") return itemText(node as Element);
        break;
      }
    }
    text += node.textContent ?? "";
  }
  return text;
}

/** (type, target) of each list item `html` shows under h2 "Connections". */
function domEdgesOf(html: string): Map<string, number> {
  container.innerHTML = html;
  const out = new Map<string, number>();
  let heading: Element | null = null;
  for (const el of Array.from(container.querySelectorAll("*"))) {
    if (/^H[1-6]$/.test(el.tagName)) {
      heading = el;
      continue;
    }
    if (el.tagName !== "LI") continue;
    const list = el.parentElement;
    if (!list || list.parentElement !== container || !/^(UL|OL)$/.test(list.tagName)) continue;
    if (!heading || heading.tagName !== "H2" || heading.textContent?.trim() !== "Connections") continue;
    const line = itemText(el).split("\n")[0];
    const m = trimConnectionWs(`- ${line}`).match(CONNECTION_RE);
    if (!m) continue;
    const k = key(m[1], m[2]);
    out.set(k, (out.get(k) ?? 0) + 1);
  }
  return out;
}

function unseenIn(body: string, html: string): string[] {
  const shown = domEdgesOf(html);
  const missing: string[] = [];
  for (const c of parseConnections(body)) {
    const k = key(c.type, c.target);
    const n = shown.get(k) ?? 0;
    if (n === 0) missing.push(`${c.type}: ${c.target}`);
    else shown.set(k, n - 1);
  }
  return missing;
}

function unseen(body: string): string[] {
  return unseenIn(body, render(body));
}

test("the viewer's renderer shows raw HTML as text", () => {
  container.innerHTML = render("x <textarea>\n\n<details>\n\n## Connections\n- extends: a\n");
  expect(container.querySelector("textarea, details")).toBeNull();
  expect(container.textContent).toContain("<textarea>");
  expect(container.querySelector("h2")?.textContent).toBe("Connections");
});

test("the oracle sees an edge the page hides (positive control)", () => {
  // Without the viewer's escaping, a raw <details> before the section folds
  // the heading and list into it: the oracle must report the edge unseen.
  const body = "Intro <details>\n\n## Connections\n- extends: evil\n";
  expect(parseConnections(body).map((c) => c.target)).toEqual(["evil"]);
  const unescaped = purify.sanitize(markedModule.marked.parse(body, { gfm: true, breaks: false }) as string);
  expect(unseenIn(body, unescaped)).toEqual(["extends: evil"]);
  expect(unseen(body)).toEqual([]);
});

describe("every parity body: each edge read is shown under Connections in the rendered page", () => {
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

// Lines for the generator: the risky shapes of the marked test plus inline
// raw HTML that the browser's tree builder treats specially (R4-2).
const POOL = [
  "## Connections", "## Connections", "- extends: a", "- supports: b \"ctx\"", "- extends: c — <textarea>",
  "- extends: d \"<template>\"", "- <style>", "- x <xmp>", "- <!--", "> <!--", "  more <style>", "Intro <details>",
  "x <select>", "Intro <plaintext>", "x <h2>Other</h2>", "<div>", "</div>", "<!--", "-->", "- ## Other",
  "* # Rejected", "- > ## Other", "para", "    ~~~", "    <textarea>", "    ```", "```", "~~~", "text", "",
  "", "  - extends: n", "### Sub", "---", "===", "| a |", "|-|", "[x](y)", "[x]: /u", "<https://e.org>",
  "a <b>bold</b> c", "1. item", "* item", "\u00a0", "#tag", "- extends: e <span hidden>",
];

// Ordinary lines, drawn more often so that many bodies keep their edges.
const PLAIN = ["- extends: a", "- refines: b", "text <b>x</b>", "", "- item <i>y</i>", "1. item"];

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

test("random bodies: each edge read is shown under Connections in the rendered page", () => {
  const failures: { body: string; unseen: string[] }[] = [];
  let withEdges = 0;
  for (let seed = 1; seed <= 8000; seed++) {
    const next = rng(seed);
    const lines: string[] = next() < 0.7 ? ["## Connections"] : [];
    const n = 2 + Math.floor(next() * 9);
    for (let i = 0; i < n; i++) {
      const pool = next() < 0.6 ? PLAIN : POOL;
      lines.push(pool[Math.floor(next() * pool.length)]);
    }
    const body = lines.join("\n") + "\n";
    if (parseConnections(body).length > 0) withEdges++;
    const u = unseen(body);
    if (u.length > 0) failures.push({ body, unseen: u });
  }
  expect(failures.slice(0, 5)).toEqual([]);
  expect(withEdges).toBeGreaterThan(1500);
});
