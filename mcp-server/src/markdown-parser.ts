// SPDX-FileCopyrightText: 2026 Yibei Chen
//
// SPDX-License-Identifier: AGPL-3.0-or-later

import matter from "gray-matter";
import type { Connection } from "./types.js";

// Explicit whitespace set shared verbatim with cli/schist/ingest.py's
// _SLUG_WS_CHARS and cli/schist/markdown_io.py's SLUG_WS_CHARS. JS's \s and
// Python's \s disagree at the edges — JS adds U+FEFF (ZWNBSP), Python adds
// U+001C–U+001F (C0 separators) and U+0085 (NEL) — exactly the cross-language
// drift family behind #303/#318/#338. This is the UNION of both engines' sets
// (30 codepoints, all BMP single code units), so either language's notion of
// whitespace behaves identically in slugs and connection lines. Single-sourced
// here (the lowest-level module); tools.ts imports it for slug normalization.
export const SLUG_WS_CHARS =
  "\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680" +
  "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007" +
  "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff";

// None of the members are regex metacharacters, so they can sit in a class raw.
const WS_CLASS = `[${SLUG_WS_CHARS}]`;
const NON_WS_CLASS = `[^${SLUG_WS_CHARS}]`;

/**
 * Shared regex for connection lines — exported so sqlite-reader doesn't
 * duplicate it. Built from the explicit whitespace union (`\s` → WS_CLASS,
 * `\S` → its negation): native \s / \S membership drifts between JS and
 * Python, so the two languages' CONNECTION_REs parsed divergent codepoints
 * differently — e.g. a NEL (U+0085) separator produced an edge under
 * Python's \s but not under JS's (#338). The context group is [^\n\r]*, not
 * `.*`: JS's `.` stops at U+2028/U+2029, which can sit inside a line.
 * schema/connection-line-parity.json pins this against
 * cli/schist/ingest.py's CONNECTION_RE.
 */
export const CONNECTION_RE = new RegExp(
  `^-${WS_CLASS}+(${NON_WS_CLASS}+):${WS_CLASS}+(${NON_WS_CLASS}+)` +
  `(?:${WS_CLASS}+"([^"]*)")?(?:${WS_CLASS}+—${WS_CLASS}+([^\n\r]*))?$`
);

// Codepoints Python's str.splitlines() treats as line boundaries. This is a
// STRICT SUBSET of SLUG_WS_CHARS — splitlines does NOT break on plain space,
// tab, U+00A0, U+2000–200A, U+202F, U+205F, U+3000, or U+FEFF, and it excludes
// U+001F — so it is defined independently here; do NOT reuse the slug class.
// Python ingest's parse_connections iterates body.splitlines(); a TS reader
// that split only on "\n" saw a heading + edge separated by e.g. NEL (U+0085)
// as ONE line, so a bogus connection type smuggled across such a separator
// bypassed the #317 vocabulary check while ingest still indexed the edge (#359).
const LINE_BOUNDARY = new Set([
  "\n", "\v", "\f", "\r",
  "\u001c", "\u001d", "\u001e",
  "\u0085", "\u2028", "\u2029",
]);

/**
 * Split `text` on the same line boundaries as Python's str.splitlines()
 * (NOT str.split("\n")), collapsing a CR+LF pair into a single break like
 * splitlines does. Linear — one left-to-right index scan, no backtracking
 * regex (see the O(n²) alternated-anchored-regex gotcha the slug code avoids).
 * The trailing empty segment after a final boundary is dropped, matching
 * splitlines(). Single-sourced here and imported by tools.ts so the read
 * path (parseConnections), the write-time validator, and the delete-cascade
 * repair all agree with ingest on where a line ends. #359.
 */
export function splitLinesLikePython(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (LINE_BOUNDARY.has(ch)) {
      lines.push(text.slice(start, i));
      if (ch === "\r" && text[i + 1] === "\n") i++;
      start = i + 1;
    }
  }
  if (start < text.length) lines.push(text.slice(start));
  return lines;
}

/**
 * True when `text` contains any codepoint Python's str.splitlines() (and thus
 * ingest's connection parser) treats as a line boundary. A connection target
 * carrying such a character serializes into `- type: target` and then splits
 * back into MORE than one line on read, so ingest indexes a forged extra edge
 * the caller never intended and no ACL gated (#398). Callers reject the write
 * rather than let buildConnectionLine emit a multi-line entry. Uses the SAME
 * LINE_BOUNDARY set as splitLinesLikePython so the write-time guard and the
 * read-time splitter can't disagree on what ends a line.
 */
export function containsLineBoundary(text: string): boolean {
  for (const ch of text) {
    if (LINE_BOUNDARY.has(ch)) return true;
  }
  return false;
}

/**
 * True when `target` can round-trip through CONNECTION_RE, whose target group
 * is ONE non-whitespace run over the pinned SLUG_WS_CHARS class (#408). An
 * EMPTY target serializes `- type:  "context"` — on read the quoted context
 * slides into the target slot, promoting caller-chosen context text to an
 * edge target (or, with no context, a dead line that parses to nothing while
 * the tool reports success). A whitespace-carrying target (space, tab, NBSP —
 * not just the #398 line boundaries, which are a subset of SLUG_WS_CHARS)
 * writes a line CONNECTION_RE never matches: the write succeeds and commits,
 * but no edge is ever indexed. Callers reject the write instead.
 */
export function isRoundTrippableTarget(target: string): boolean {
  if (!target) return false;
  for (const ch of target) {
    if (SLUG_WS_CHARS.includes(ch)) return false;
  }
  return true;
}

/**
 * The connections section rule, shared by every reader and writer of a
 * note's `## Connections` section: parseConnections and insertConnectionLine
 * here, buildNote's section replace, and tools.ts's body validator, concept
 * check and delete-cascade repair. Mirrors cli/schist/ingest.py's
 * connections_section and friends; the body cases in
 * schema/connection-line-parity.json and the writer cases in
 * schema/connection-append-parity.json pin the two.
 *
 * The rule reads the note body (gray-matter's content, which is not
 * stripped). Writers scanning a whole file skip the frontmatter lines first
 * (noteBodyStartLine).
 * - A heading is a line of 0-3 spaces, then exactly `## Connections`, then
 *   optional spaces or tabs. Four spaces or a tab make an indented code block.
 * - A fence opens on a line of 0-3 spaces then 3+ backticks (with no backtick
 *   in the rest of the line) or 3+ tildes, and closes on a later line of 0-3
 *   spaces, then the same character at least as many times, then optional
 *   spaces or tabs. HTML blocks follow CommonMark 0.31 section 4.6 (types 1-7,
 *   HTML_BLOCK_STARTS; type 7 cannot interrupt a paragraph). Whichever block
 *   opens first governs until it closes; an unclosed one runs to the end.
 *   Lines in a fence or HTML block are never a heading, never an edge, and
 *   never end the section. Once a list item opens, its indented lines are
 *   skipped the same way (listOpen in scanBlocks). A line starting with `>`
 *   (a block quote) is never a heading or an edge.
 * - The section ends at the next other line of 0-3 spaces, then `## `, then
 *   text (SECTION_END_RE).
 * - An edge line starts with 0-3 spaces, `-` and a space
 *   (EDGE_PREFIX_RE), then matches CONNECTION_RE once trimmed with the
 *   explicit SLUG_WS_CHARS set (not trim(), whose whitespace differs from
 *   Python's str.strip()).
 * - Lines end only at CommonMark line endings (splitConnectionLines).
 */
const CONNECTIONS_HEADING_RE = /^ {0,3}## Connections[ \t]*$/;
const SECTION_END_RE = /^ {0,3}## [ \t]*[^ \t]/;

/** True when `line` (already split) has the shape of a connections heading.
 * It says nothing about fences or HTML blocks; connectionsSection checks both. */
export function isConnectionsHeading(line: string): boolean {
  return CONNECTIONS_HEADING_RE.test(line);
}

/** True when `line` has the shape of a `## ` heading that ends a section. */
export function isSectionEnd(line: string): boolean {
  return SECTION_END_RE.test(line);
}

/** Trim the explicit whitespace union shared with ingest.py. Linear. */
export function trimConnectionWs(text: string): string {
  let a = 0;
  let b = text.length;
  while (a < b && SLUG_WS_CHARS.includes(text[a])) a++;
  while (b > a && SLUG_WS_CHARS.includes(text[b - 1])) b--;
  return text.slice(a, b);
}

const FENCE_OPEN_RE = /^ {0,3}(?:(`{3,})[^`]*|(~{3,})[^\n\r]*)$/;
const FENCE_CLOSE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;

// CommonMark 0.31 HTML blocks (spec section 4.6). Patterns run on the line
// with ASCII letters lowercased (asciiLower), never the /i flag, so the case
// rule matches ingest.py's (Python's re.IGNORECASE folds U+017F to "s").
const HTML_BLOCK_6_TAGS =
  "address|article|aside|base|basefont|blockquote|body|caption|center|col|" +
  "colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|" +
  "footer|form|frame|frameset|h1|h2|h3|h4|h5|h6|head|header|hr|html|iframe|" +
  "legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|" +
  "param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|" +
  "track|ul";
const HTML_ATTR =
  `[ \\t]+[a-z_:][a-z0-9_.:-]*` +
  `(?:[ \\t]*=[ \\t]*(?:[^ \\t"'=<>\`]+|'[^']*'|"[^"]*"))?`;
// [type, start pattern]; types 1-5 end at a line containing their end marker
// (htmlBlockEnd), types 6 and 7 before a blank line.
const HTML_BLOCK_STARTS: [number, RegExp][] = [
  [1, /^ {0,3}<(script|pre|style|textarea)(?:[ \t]|>|$)/],
  [2, /^ {0,3}<!--/],
  [3, /^ {0,3}<\?/],
  [4, /^ {0,3}<![a-z]/],
  [5, /^ {0,3}<!\[cdata\[/],
  [6, new RegExp(`^ {0,3}</?(?:${HTML_BLOCK_6_TAGS})(?:[ \\t]|/?>|$)`)],
  [7, new RegExp(
    `^ {0,3}(?:<(?!(?:script|style|pre|textarea)(?:[ \\t/>]|$))` +
    `[a-z][a-z0-9-]*(?:${HTML_ATTR})*[ \\t]*/?>` +
    `|</(?!(?:script|style|pre|textarea)[ \\t>])[a-z][a-z0-9-]*[ \\t]*>)[ \\t]*$`,
  )],
];
const HTML_END_MARKERS: Record<number, string> = { 2: "-->", 3: "?>", 4: ">", 5: "]]>" };
const ATX_HEADING_RE = /^ {0,3}#{1,6}(?:[ \t]|$)/;
const LIST_MARKER_RE = /^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$)/;
const THEMATIC_BREAK_RE = /^ {0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$/;
const EDGE_PREFIX_RE = /^ {0,3}- /;

function asciiLower(line: string): string {
  return line.replace(/[A-Z]/g, (c) => String.fromCharCode(c.charCodeAt(0) + 32));
}

/** Columns of leading spaces and tabs (a tab advances to the next multiple
 * of 4), as CommonMark measures indentation. */
function indentWidth(line: string): number {
  let width = 0;
  for (const ch of line) {
    if (ch === " ") width += 1;
    else if (ch === "\t") width += 4 - (width % 4);
    else break;
  }
  return width;
}

function isBlank(line: string): boolean {
  return /^[ \t]*$/.test(line);
}

const EMPTY_QUOTE_RE = /^ {0,3}>[ \t]*$/;
// The HTML that interrupts a paragraph in marked 12 (its `paragraph` rule):
// lowercase type 6 tags followed by spaces, the end, `>` or `/>`, and the
// type 1 and 2 openers, at the start of the line (no indentation), as written
// (case-sensitive).
const MARKED_HTML_INTERRUPT_RE = new RegExp(
  `^(?:</?(?:${HTML_BLOCK_6_TAGS})(?: +|$|/?>)|<(?:script|pre|style|textarea|!--))`,
);
const MARKED_ITEM_RE = /^( {0,3}(?:[*+-]|[0-9]{1,9}[.)]))((?:[\t ][^\n]*)?)$/;

/** marked's lexer turns leading tabs (after any spaces) into four spaces each
 * before it reads blocks. */
function markedTabs(line: string): string {
  return line.replace(/^( *)(\t+)/, (_m, sp: string, tabs: string) => sp + "    ".repeat(tabs.length));
}

/** [indent, first content line] of the list item marked starts at `line`, or
 * [0, ""] when it starts none. Mirrors ingest.py _marked_item_start. */
function markedItemStart(line: string, inList = false): [number, string] {
  const m = MARKED_ITEM_RE.exec(markedTabs(line));
  // marked's list rule needs the bullet alone or followed by a space or tab
  // and at least one more character, so "- " starts no list; inside a list
  // (inList: the line ended the previous item) it is an item.
  if (!m || (m[2].length === 1 && !inList)) return [0, ""];
  let n = (m[2] + "\n").search(/[^ ]/);
  n = n > 4 ? 1 : n;
  const first = m[2].replace(/^\t+/, (t) => "   ".repeat(t.length));
  return [n + m[1].length, first.slice(n)];
}

/** Whether marked keeps `line` inside the open list item (marked 12
 * Tokenizer.list). Mirrors ingest.py _marked_item_continues. */
function markedItemContinues(line: string, indent: number, prev: string, blankSeen: boolean): boolean {
  const nl = markedTabs(line);
  const k = Math.min(3, indent - 1);
  const begins = [new RegExp(`^ {0,${k}}(?:\`\`\`|~~~)`), new RegExp(`^ {0,${k}}#`)];
  if (begins.some((b) => b.test(nl))) return false;
  if (new RegExp(`^ {0,${k}}(?:[*+-]|[0-9]{1,9}[.)])(?:[ \t][^\n]*)?$`).test(nl)) return false;
  const hr = new RegExp(`^ {0,${k}}((?:- *){3,}|(?:_ *){3,}|(?:\\* *){3,})$`);
  if (hr.test(nl)) return false;
  if (!nl.trim()) return true;
  if (nl.search(/[^ ]/) >= indent) return true;
  if (blankSeen) return false;
  if (prev && prev.search(/[^ ]/) >= 4) return false;
  if (begins.some((b) => b.test(prev)) || hr.test(prev)) return false;
  return true;
}

/** True for a non-indented line that cannot continue a list item: an ATX
 * heading, a fence, an HTML block start or a thematic break. */
function closesList(line: string): boolean {
  if (ATX_HEADING_RE.test(line) || FENCE_OPEN_RE.test(line) || THEMATIC_BREAK_RE.test(line)) return true;
  return startsHtmlBlock(line);
}

function startsHtmlBlock(line: string): boolean {
  const low = asciiLower(line);
  return HTML_BLOCK_STARTS.some(([, pattern]) => pattern.test(low));
}

/** The text after the end marker of an HTML block of type 2-5 on `line`
 * (marked ends the block there and reads on). */
function afterHtmlEnd(kind: number, line: string, searchFrom: number): string {
  const marker = HTML_END_MARKERS[kind];
  const idx = line.indexOf(marker, searchFrom);
  return idx === -1 ? "" : line.slice(idx + marker.length);
}

/** Under the marked reading, what the rest of a line after an HTML end
 * marker opens. An HTML block of type 2-5 that also ends inside `rest` hands
 * its own rest on. Mirrors ingest.py _open_in_rest. */
function openInRest(rest: string): { fenceChar: string; fenceLen: number; htmlKind: number; htmlTag: string } {
  const none = { fenceChar: "", fenceLen: 0, htmlKind: 0, htmlTag: "" };
  for (let guard = 0; guard <= rest.length; guard++) {
    const m = FENCE_OPEN_RE.exec(rest);
    if (m) {
      const run = m[1] ?? m[2];
      return { fenceChar: run[0], fenceLen: run.length, htmlKind: 0, htmlTag: "" };
    }
    const low = asciiLower(rest);
    let next: string | null = null;
    for (const [kind, pattern] of HTML_BLOCK_STARTS) {
      const hm = pattern.exec(low);
      if (hm) {
        const tag = kind === 1 ? hm[1] : "";
        if (kind === 6 || kind === 7 || !htmlBlockEnd(kind, tag, low)) {
          return { fenceChar: "", fenceLen: 0, htmlKind: kind, htmlTag: tag };
        }
        if (kind === 1) return none;
        next = afterHtmlEnd(kind, rest, hm.index + 2);
        break;
      }
    }
    if (next === null) return none;
    rest = next;
  }
  return none;
}

function htmlBlockEnd(kind: number, tag: string, low: string): boolean {
  // Only the opener's own close tag: CommonMark accepts any of the four, but
  // marked (the viewer's renderer) needs the matching one, and the stricter
  // end only keeps the block open longer.
  if (kind === 1) return low.includes(`</${tag}>`);
  return low.includes(HTML_END_MARKERS[kind]);
}

type LineKind = "" | "header" | "fence" | "html" | "list" | "lazy";

/**
 * Classify each line: "" (ordinary), "header" (before `start`), "fence" (a
 * fence delimiter or inside a fenced code block) or "html" (inside an HTML
 * block). Whichever block opens first governs until it closes. `closer` is
 * what an unclosed block still needs at the end: its closing line, "" (a
 * blank line) for HTML block types 6 and 7, or null when nothing is open.
 *
 * With markedReading, the places where marked (the viewer's renderer)
 * departs from CommonMark follow marked: list items follow marked's own rules
 * (markedItemContinues); only marked's own set of HTML openers interrupts a
 * paragraph; an HTML block of type 2-5 ends right after its end
 * marker, so the rest of that line can open a fence, another HTML block or
 * a list item;
 * and a line holding U+2028 or U+2029 is not a heading, a rule or a type 7
 * HTML start. With anyHtmlInterrupts as well, an HTML block of any type may
 * start right after paragraph text: marked also ends paragraphs in places
 * neither rule set predicts (a line followed by a `-` line can read as a
 * table), and starting the block there only skips more. connectionsSection skips
 * a line when either reading skips it. Mirrors ingest.py _scan_blocks.
 */
function scanBlocks(
  lines: string[],
  start = 0,
  markedReading = false,
  anyHtmlInterrupts = false,
): { kinds: LineKind[]; closer: string | null } {
  const kinds: LineKind[] = [];
  let fenceChar = "";
  let fenceLen = 0;
  let fenceIndent = 0; // opener's indentation for a fence opened in a list item
  let htmlKind = 0;
  let htmlTag = "";
  let inParagraph = false;
  // List items: once a list marker line opens a list, an indented line is
  // list-item content, never a top-level heading, edge or section end. The
  // list closes at a non-indented line after a blank line, or at a
  // non-indented heading, fence, HTML block or thematic break. Stricter than
  // CommonMark's content-column rule on purpose (renderers differ there): it
  // only ever skips more lines. Mirrors ingest.py _scan_blocks.
  let listOpen = false;
  // The marked reading follows marked's own list-item rules instead
  // (markedItemStart, markedItemContinues).
  let itemIndent = 0;
  let itemPrev = "";
  let itemBlank = false;
  let prevBlank = true;
  for (let i = 0; i < lines.length; i++) {
    const ln = lines[i];
    const blank = isBlank(ln);
    const wasBlank = prevBlank;
    prevBlank = blank;
    const listBefore = markedReading ? itemIndent !== 0 : listOpen;
    if (i < start) {
      kinds.push("header");
      continue;
    }
    if (fenceChar) {
      kinds.push("fence");
      // A fence opened inside a list item closes only at a closer at least as
      // indented; a shallower fence line ends the item and opens a new fence
      // for a renderer, so keeping this one open only skips more.
      const m = fenceIndent
        ? (indentWidth(ln) >= fenceIndent ? FENCE_CLOSE_RE.exec(ln.replace(/^[ \t]+/, "")) : null)
        : FENCE_CLOSE_RE.exec(ln);
      if (m && m[1][0] === fenceChar && m[1].length >= fenceLen) {
        fenceChar = "";
        fenceLen = 0;
      }
      continue;
    }
    if (htmlKind) {
      if (htmlKind === 6 || htmlKind === 7) {
        if (isBlank(ln)) {
          htmlKind = 0;
          kinds.push("");
          inParagraph = false;
        } else {
          kinds.push("html");
        }
        continue;
      }
      kinds.push("html");
      if (htmlBlockEnd(htmlKind, htmlTag, asciiLower(ln))) {
        const ended = htmlKind;
        htmlKind = 0;
        if (markedReading && ended !== 1) {
          const rest = afterHtmlEnd(ended, ln, 0);
          ({ fenceChar, fenceLen, htmlKind, htmlTag } = openInRest(rest));
          fenceIndent = 0;
          if (!fenceChar && !htmlKind) {
            [itemIndent, itemPrev] = markedItemStart(rest);
            itemBlank = false;
          }
        }
      }
      continue;
    }
    // marked's heading, rule and type 7 HTML patterns stop at U+2028 and
    // U+2029 (JS `.` and `$`), so to marked such a line is none of these.
    const lineSep = markedReading && (ln.includes("\u2028") || ln.includes("\u2029"));
    let probe = ln;
    let endedItem = false;
    if (markedReading) {
      if (itemIndent) {
        if (markedItemContinues(ln, itemIndent, itemPrev, itemBlank)) {
          kinds.push("list");
          if (!ln.trim()) itemBlank = true;
          itemPrev = markedTabs(ln).slice(itemIndent);
          continue;
        }
        itemIndent = 0;
        endedItem = true;
      }
    } else if (listOpen && !blank) {
      if (indentWidth(ln) >= 1) {
        // Item content, or the first line after the list when the renderer's
        // content column is deeper: never a heading or an edge. A fence or
        // HTML block opened here still opens one (at any indentation).
        probe = ln.replace(/^[ \t]+/, "");
        const lowProbe = asciiLower(probe);
        if (!(FENCE_OPEN_RE.test(probe) ||
              HTML_BLOCK_STARTS.some(([kind, pattern]) => kind !== 7 && pattern.test(lowProbe)))) {
          kinds.push("list");
          continue;
        }
      } else if ((wasBlank && !LIST_MARKER_RE.test(ln)) || closesList(ln)) {
        listOpen = false;
      }
    }
    const m = FENCE_OPEN_RE.exec(probe);
    if (m) {
      const run = m[1] ?? m[2];
      fenceChar = run[0];
      fenceLen = run.length;
      fenceIndent = probe !== ln ? indentWidth(ln) : 0;
      kinds.push("fence");
      inParagraph = false;
      continue;
    }
    const low = asciiLower(probe);
    let opened = false;
    for (const [kind, pattern] of HTML_BLOCK_STARTS) {
      if (kind === 7 && (lineSep || (inParagraph && !listBefore && !anyHtmlInterrupts))) {
        continue; // type 7 cannot interrupt a paragraph
      }
      if (markedReading && inParagraph && !listBefore && !anyHtmlInterrupts &&
          !MARKED_HTML_INTERRUPT_RE.test(probe)) {
        break; // marked: only these (case-sensitive) interrupt one
      }
      const hm = pattern.exec(low);
      if (hm) {
        kinds.push("html");
        opened = true;
        inParagraph = false;
        htmlTag = kind === 1 ? hm[1] : "";
        if (kind === 6 || kind === 7 || !htmlBlockEnd(kind, htmlTag, low)) {
          htmlKind = kind;
        } else if (markedReading && kind !== 1) {
          const rest = afterHtmlEnd(kind, probe, hm.index + 2);
          ({ fenceChar, fenceLen, htmlKind, htmlTag } = openInRest(rest));
          fenceIndent = 0;
          if (!fenceChar && !htmlKind) {
            [itemIndent, itemPrev] = markedItemStart(rest);
            itemBlank = false;
          }
        }
        break;
      }
    }
    if (opened) continue;
    if (inParagraph && !listOpen && indentWidth(ln) >= 1 && indentWidth(ln) <= 3 && LIST_MARKER_RE.test(ln)) {
      // An indented list marker right after paragraph text: marked can read
      // it as part of a setext heading's text ("text\n - a\n-"), so it is
      // never an edge, and the paragraph goes on.
      kinds.push("lazy");
      listOpen = true; // marked may read it as a list; skip its content
      if (markedReading) {
        [itemIndent, itemPrev] = markedItemStart(ln, endedItem);
        itemBlank = false;
      }
      continue;
    }
    kinds.push("");
    if (LIST_MARKER_RE.test(ln) && !THEMATIC_BREAK_RE.test(ln)) listOpen = true;
    if (markedReading && !THEMATIC_BREAK_RE.test(ln)) {
      [itemIndent, itemPrev] = markedItemStart(ln, endedItem);
      itemBlank = false;
    }
    if (isBlank(ln) || EMPTY_QUOTE_RE.test(ln) || (!lineSep && (ATX_HEADING_RE.test(ln) || THEMATIC_BREAK_RE.test(ln)))) {
      inParagraph = false;
    } else if (!inParagraph && indentWidth(ln) >= 4) {
      // an indented code line, not a paragraph
    } else {
      inParagraph = true;
    }
  }
  let closer: string | null = null;
  if (fenceChar) closer = fenceChar.repeat(fenceLen);
  else if (htmlKind === 1) closer = `</${htmlTag}>`;
  else if (htmlKind >= 2 && htmlKind <= 5) closer = HTML_END_MARKERS[htmlKind];
  else if (htmlKind === 6 || htmlKind === 7) closer = "";
  return { kinds, closer };
}

/** Per line: true for a line before `start` (frontmatter), a fence
 * delimiter, or a line inside a fenced code block, an HTML block or a list
 * item, under either the CommonMark or the marked reading (scanBlocks). */
/** Line kinds under each reading: CommonMark, marked, and marked with HTML
 * blocks allowed to interrupt any paragraph. Mirrors ingest.py _readings. */
function readings(lines: string[], start: number): LineKind[][] {
  return [
    scanBlocks(lines, start).kinds,
    scanBlocks(lines, start, true).kinds,
    scanBlocks(lines, start, true, true).kinds,
  ];
}

/** Per reading (CommonMark, marked, marked with any HTML interrupting a
 * paragraph), per line: true when that reading puts the line before `start`,
 * in a fence, in an HTML block or in a list item.
 * schema/connection-reading-parity.json pins each reading in both languages. */
export function connectionLineReadings(lines: string[], start = 0): boolean[][] {
  return readings(lines, start).map((r) => r.map((k) => k !== ""));
}

export function skippedLineFlags(lines: string[], start = 0): boolean[] {
  const rs = readings(lines, start);
  return lines.map((_l, i) => rs.some((r) => r[i] !== ""));
}

/** What a fence or HTML block still open after the last line needs before a
 * writer adds a heading or edge at the end: its closing line, "" (a blank
 * line) for HTML block types 6 and 7, or null when nothing is open. */
export function unclosedBlockCloser(lines: string[], start = 0): string | null {
  return scanBlocks(lines, start).closer;
}

/**
 * Locate the first connections section in `lines`, ignoring lines before
 * `start` (a writer passes noteBodyStartLine): the heading index, the end
 * index (the `## ` line that closes the section, or lines.length), and the
 * skipped flags; lines between heading and end whose flag is true are in a
 * fence, HTML block or list item and must be skipped. A line counts as a
 * heading or an edge only when every reading (readings) leaves it alone; a
 * `## ` line ends the section when any does, which only ends it sooner.
 * Null when no heading sits outside all of these.
 */
export function connectionsSection(
  lines: string[],
  start = 0,
): { heading: number; end: number; skipped: boolean[] } | null {
  const rs = readings(lines, start);
  const skipped = lines.map((_l, i) => rs.some((r) => r[i] !== ""));
  let heading = -1;
  for (let i = 0; i < lines.length; i++) {
    if (!skipped[i] && isConnectionsHeading(lines[i])) {
      heading = i;
      break;
    }
  }
  if (heading === -1) return null;
  let end = lines.length;
  for (let i = heading + 1; i < lines.length; i++) {
    if (rs.some((r) => r[i] === "") && isSectionEnd(lines[i])) {
      end = i;
      break;
    }
  }
  return { heading, end, skipped };
}

/** True when `body` has a connections heading outside a fence or HTML block. */
export function hasConnectionsHeading(body: string): boolean {
  return connectionsSection(splitConnectionLines(body)) !== null;
}

/**
 * Python's str.isspace() set (and its re `\s`): the whitespace
 * python-frontmatter's text.strip() and FM_BOUNDARY `\s*` use. ingest.py's
 * PY_WHITESPACE is the same string; schema/frontmatter-boundary-parity.json
 * pins both. Unlike JS trim(), it has U+001C-U+001F and U+0085 but not U+FEFF.
 */
export const PY_WHITESPACE =
  "\t\n\u000b\u000c\r\u001c\u001d\u001e\u001f \u0085  " +
  "        " +
  "       　";

/**
 * Split on CommonMark line endings only (\n, \r\n, \r), the way a Markdown
 * renderer does; VT, FF, U+001C-U+001E, NEL, LS and PS stay inside a line. A
 * final line ending adds no empty line. Every connection reader and writer
 * splits with this; ingest.py split_connection_lines is its twin.
 * (splitLinesLikePython stays for the write-time line-boundary guards.)
 */
export function splitConnectionLines(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\n" || ch === "\r") {
      lines.push(text.slice(start, i));
      if (ch === "\r" && text[i + 1] === "\n") i++;
      start = i + 1;
    }
  }
  if (start < text.length) lines.push(text.slice(start));
  return lines;
}

type FrontmatterKind = "yaml" | "json" | "toml";

/** If a frontmatter boundary of `kind` starts at line start `p` of `s`,
 * where python-frontmatter's FM_BOUNDARY match ends, else -1. YAML is
 * `^-{3,}\s*$` and TOML `^\+{3,}\s*$` (re.MULTILINE: `$` only before "\n"
 * or at the end); JSON is `^(?:{|})$`. */
function delimiterMatch(s: string, p: number, kind: FrontmatterKind): number {
  const n = s.length;
  if (kind === "json") {
    return p < n && (s[p] === "{" || s[p] === "}") && (p + 1 === n || s[p + 1] === "\n") ? p + 1 : -1;
  }
  const ch = kind === "yaml" ? "-" : "+";
  let q = p;
  while (q < n && s[q] === ch) q++;
  if (q - p < 3) return -1;
  let e = q;
  while (e < n && PY_WHITESPACE.includes(s[e])) e++;
  if (e === n) return n;
  const lastNl = s.lastIndexOf("\n", e - 1);
  return lastNl >= q ? lastNl : -1;
}

/**
 * Length of the frontmatter block at the start of `content`, up to and
 * including its closing line's terminator: python-frontmatter's rule, which
 * ingest (the index) uses, written out in ingest.py note_body_offset and
 * here. Turn CRLF into LF, strip Python whitespace, detect YAML (`---`),
 * then JSON (`{`), then TOML (`+++`) on the first line, and close at the next
 * boundary line. 0 when there is no frontmatter (a BOM before `---` means
 * none) or it is never closed. gray-matter's own split differs on `-----`,
 * `---foo`, leading blank lines and a BOM, so connection paths slice with
 * this instead (connectionBody, noteBodyStartLine).
 * schema/frontmatter-boundary-parity.json pins both languages.
 */
export function frontmatterLength(content: string): number {
  const norm = content.split("\r\n").join("\n");
  let lead = 0;
  while (lead < norm.length && PY_WHITESPACE.includes(norm[lead])) lead++;
  let end = norm.length;
  while (end > lead && PY_WHITESPACE.includes(norm[end - 1])) end--;
  const s = norm.slice(lead, end);
  let kind: FrontmatterKind | null = null;
  let first = -1;
  for (const k of ["yaml", "json", "toml"] as FrontmatterKind[]) {
    first = delimiterMatch(s, 0, k);
    if (first !== -1) {
      kind = k;
      break;
    }
  }
  if (kind === null) return 0;
  let p = s.indexOf("\n", first);
  while (p !== -1) {
    const start = p + 1;
    if (delimiterMatch(s, start, kind) !== -1) {
      const nl = norm.indexOf("\n", lead + start);
      const off = nl === -1 ? norm.length : nl + 1;
      // Map the offset in `norm` back onto `content`: each CRLF before it was
      // one character shorter in `norm`.
      let pairs = 0;
      let i = content.indexOf("\r\n");
      while (i !== -1 && i - pairs < off) {
        pairs++;
        i = content.indexOf("\r\n", i + 2);
      }
      return off + pairs;
    }
    p = s.indexOf("\n", start);
  }
  return 0;
}

/** The note body connection parsing reads: the text after the frontmatter's
 * closing line by frontmatterLength's rule, as ingest reads it. */
export function connectionBody(content: string): string {
  return content.slice(frontmatterLength(content));
}

/** Number of splitConnectionLines lines that belong to the frontmatter
 * (frontmatterLength). Writers pass it as connectionsSection's `start`. */
export function noteBodyStartLine(content: string): number {
  return splitConnectionLines(content.slice(0, frontmatterLength(content))).length;
}

/**
 * The edge a body line carries under the connection rule, or null: the line
 * must start with 0-3 spaces, "-" and a space (EDGE_PREFIX_RE; a tab or 4+
 * spaces make code, and marked reads "-<tab>" after a quote as quote text),
 * and, trimmed with trimConnectionWs, match
 * CONNECTION_RE,
 * and a bracket reference (`[moltbook]`, `[[wiki-note]]`) is skipped (#415).
 * ingest.py parse_connection_lines applies the same test.
 */
export function connectionLineMatch(line: string): RegExpMatchArray | null {
  if (!EDGE_PREFIX_RE.test(line)) return null;
  const stripped = trimConnectionWs(line);
  const match = stripped.match(CONNECTION_RE);
  if (!match || match[2].startsWith("[")) return null;
  return match;
}

/** Edges of a raw body's `## Connections` section, each with its index in
 * splitConnectionLines(body). ingest.py parse_connection_lines is its twin. */
export function parseConnectionLines(body: string): { index: number; connection: Connection }[] {
  const out: { index: number; connection: Connection }[] = [];
  const lines = splitConnectionLines(body);
  const section = connectionsSection(lines);
  if (section === null) return out;
  for (let i = section.heading + 1; i < section.end; i++) {
    if (section.skipped[i]) continue;
    const match = connectionLineMatch(lines[i]);
    if (match) {
      out.push({
        index: i,
        connection: { type: match[1], target: match[2], context: match[3] || match[4] || undefined },
      });
    }
  }
  return out;
}

export function parseConnections(body: string): Connection[] {
  return parseConnectionLines(body).map((e) => e.connection);
}

const HASHTAG_AT_START_RE = /^#[^\s,\]\}]+/u;

// A quote (' or ") only *opens* a quoted scalar when it sits where a node can
// start: at the beginning of the value, or right after a flow indicator
// ([ { , :). Anywhere else it is a literal apostrophe/quote inside a plain
// scalar (e.g. the ' in `it's`) and must not toggle quote state — this is how
// YAML actually tokenizes, and matching it keeps us from desyncing on bare
// scalars that contain apostrophes.
function opensQuotedScalar(lastSignificant: string): boolean {
  return (
    lastSignificant === "" ||
    lastSignificant === "[" ||
    lastSignificant === "{" ||
    lastSignificant === "," ||
    lastSignificant === ":"
  );
}

// Count the run of backslashes ending just before index i. Inside a
// double-quoted scalar a `"` closes the string only when preceded by an even
// number of backslashes (an odd count means the quote itself is escaped).
function trailingBackslashes(line: string, i: number): number {
  let count = 0;
  for (let j = i - 1; j >= 0 && line[j] === "\\"; j--) count++;
  return count;
}

function quoteFlowHashtags(line: string): string {
  if (!line.includes("#") || (!line.includes("[") && !line.includes("{"))) return line;

  let result = "";
  let flowDepth = 0;
  let inSingle = false;
  let inDouble = false;
  // Last non-whitespace char seen in unquoted (structural) context. Drives the
  // node-start decision for quote openers; "" means start-of-line.
  let lastSignificant = "";

  for (let i = 0; i < line.length; i++) {
    const ch = line[i];

    if (inSingle) {
      // YAML single-quoted scalar: '' is an escaped apostrophe; a lone ' closes.
      if (ch === "'") {
        if (line[i + 1] === "'") {
          result += "''";
          i++;
          continue;
        }
        inSingle = false;
        lastSignificant = "'";
      }
      result += ch;
      continue;
    }

    if (inDouble) {
      if (ch === '"' && trailingBackslashes(line, i) % 2 === 0) {
        inDouble = false;
        lastSignificant = '"';
      }
      result += ch;
      continue;
    }

    // Unquoted structural context.
    if (ch === "'" && opensQuotedScalar(lastSignificant)) {
      inSingle = true;
      lastSignificant = "'";
      result += ch;
      continue;
    }
    if (ch === '"' && opensQuotedScalar(lastSignificant)) {
      inDouble = true;
      lastSignificant = '"';
      result += ch;
      continue;
    }
    if (ch === "[" || ch === "{") {
      flowDepth++;
      lastSignificant = ch;
      result += ch;
      continue;
    }
    if (ch === "]" || ch === "}") {
      if (flowDepth > 0) flowDepth--;
      lastSignificant = ch;
      result += ch;
      continue;
    }
    if (ch === "#" && flowDepth > 0) {
      const match = line.slice(i).match(HASHTAG_AT_START_RE);
      if (match) {
        result += `"${match[0]}"`;
        i += match[0].length - 1;
        lastSignificant = match[0][match[0].length - 1];
        continue;
      }
    }

    result += ch;
    if (ch !== " " && ch !== "\t") lastSignificant = ch;
  }

  return result;
}

function patchFrontmatterFlowHashtags(content: string): string {
  const lines = content.split("\n");
  let inFrontmatter = false;
  let patched = false;
  const patchedLines = lines.map((line, i) => {
    if (i === 0 && line.trim() === "---") {
      inFrontmatter = true;
      return line;
    }
    if (inFrontmatter && line.trim() === "---") {
      inFrontmatter = false;
      return line;
    }
    if (!inFrontmatter) return line;

    const next = quoteFlowHashtags(line);
    if (next !== line) patched = true;
    return next;
  });

  return patched ? patchedLines.join("\n") : content;
}

// Python's bytes.strip() whitespace: space, \t, \n, \r, \v, \f. Not JS
// trim(), which also strips U+00A0, U+FEFF, U+2028 and the rest of Unicode's
// space separators, so `---\u00a0` would pass here and fail at the hub.
const OPENER_BLANK_RE = /^[ \t\n\r\v\f]*$/;

/**
 * True when the hub's pre-receive hook refuses a note with this content: a
 * port of `_bad_opener` in cli/schist/pre_receive.py. After one leading BOM,
 * the note opens with `---`, the fourth character is not another `-`, and
 * the rest of the first line (up to `\n`) is not blank.
 *
 * This is stricter than parseNote, which reads `---yaml`, `---YML` and
 * `--- yaml` as YAML (gray-matter aliases them to its yaml engine). A writer
 * that keeps a note's first line must check this before committing it.
 * schema/frontmatter-opener-parity.json pins both implementations.
 */
export function hasNonYamlOpener(content: string): boolean {
  const s = content.startsWith("\uFEFF") ? content.slice(1) : content;
  if (!s.startsWith("---") || s[3] === "-") return false;
  const nl = s.indexOf("\n");
  const rest = nl === -1 ? s.slice(3) : s.slice(3, nl);
  return !OPENER_BLANK_RE.test(rest);
}

// gray-matter reads the text after the opening `---` as a language name, and
// its built-in `javascript` engine evaluates the block. Note content arrives
// from every participant through the hub, so only YAML is parsed: the other
// built-in engines throw (gray-matter maps js/JS/javascript to `javascript`
// and coffee/cson to `coffee`; any other name is already unregistered).
function refuseLanguage(name: string) {
  return {
    parse(): never {
      throw new Error(
        `note frontmatter must be YAML; a '---${name}' opening line is not supported`,
      );
    },
  };
}

const YAML_ONLY = {
  engines: {
    javascript: refuseLanguage("javascript"),
    coffee: refuseLanguage("coffee"),
    json: refuseLanguage("json"),
  },
};

export function parseNote(content: string): {
  metadata: Record<string, unknown>;
  body: string;
  connections: Connection[];
} {
  const patched = patchFrontmatterFlowHashtags(content);
  const parsed = matter(patched, YAML_ONLY);
  // Connections come from ingest's body slice (connectionBody), not
  // gray-matter's content, so get_note and the index agree on notes where
  // the two frontmatter splits differ (`-----`, `---foo`, a BOM, leading
  // blank lines). `body` stays gray-matter's: writers round-trip it.
  const connections = parseConnections(connectionBody(patched));
  return {
    metadata: parsed.data as Record<string, unknown>,
    body: parsed.content,
    connections,
  };
}

function sanitizeContext(context: string): string {
  // Flatten EVERY line boundary splitLinesLikePython splits on (the whole
  // LINE_BOUNDARY set — not just "\n") to a space, FIRST. A raw \r, \v, \f,
  // U+0085, U+2028, etc. otherwise survives into the quoted context and, on
  // read, splits the connection line so its tail forges an edge with an
  // attacker-chosen type and target — the #398 injection through the context
  // field rather than the target. The old "\n"-only flatten left the other 9
  // boundaries live, and the `^…/gm` strip below anchors only at JS line
  // terminators (\n \r U+2028 U+2029), so a `\v`/`\f`/`\x1c`-separated
  // `- type: target` payload (or a double `- a: - type: target` payload that
  // survives one strip) reached disk intact. With boundaries flattened the
  // context is single-line and CONNECTION_RE captures it whole.
  let safe = context;
  for (const ch of LINE_BOUNDARY) {
    if (safe.includes(ch)) safe = safe.split(ch).join(" ");
  }
  // Defense-in-depth: drop a leading connection-entry-looking prefix. With
  // boundaries already flattened this can no longer forge a multi-line edge.
  safe = safe.replace(/^-\s+\S+:\s+/gm, "");
  // Strip embedded double-quotes entirely: the context field is delimited by "..."
  // in the serialised format (regex: [^"]*) so any " inside would break parsing.
  // Replace with single-quote to preserve readability of quoted speech.
  safe = safe.replace(/"/g, "'");
  // NOTE (#408): the CLI's sanitize_context edge-strips with the pinned
  // SLUG_WS_CHARS union while trim() uses JS's WhiteSpace set, and the
  // prefix regex above uses native \s/\S where the CLI uses the union class
  // — so the two writers can serialize byte-different context for inputs
  // carrying exotic control chars (U+001F, U+FEFF edges). With boundaries
  // already flattened every such divergence is cosmetic, never a forged
  // edge; pin a parity fixture before relying on byte-identical context.
  return safe.trim();
}

export function buildConnectionLine(conn: Connection): string {
  let line = `- ${conn.type}: ${conn.target}`;
  if (conn.context) {
    const safeContext = sanitizeContext(conn.context);
    if (safeContext) {
      line += ` "${safeContext}"`;
    }
  }
  return line;
}

/**
 * Insert `line` into `content`'s `## Connections` section, creating the
 * section at EOF when no heading line exists. Scans splitConnectionLines
 * boundaries (CommonMark line endings) — the SAME splitter parseConnections
 * and ingest read with —
 * never an insert regex anchored on a bare "\n". The old regex required
 * `## Connections\n` literally, so a CRLF heading (`## Connections\r\n`,
 * core.autocrlf / Windows checkout) matched nothing, String.replace returned
 * the content unchanged, writeNote deduped the no-op, and add_connection
 * reported success while the edge was silently dropped (#366) — the same
 * shape as the missing-trailing-newline drop (#295). The heading gate is a
 * line-scan, not a substring test, so a mid-line prose mention of
 * "## Connections" gets a real section instead of an edge outside any
 * section. The heading must be exact and outside a fenced code block or
 * HTML block, and only body lines count (connectionsSection and
 * noteBodyStartLine, the rule parseConnections reads with), so a
 * `## Connections` shown in a fence never receives an edge. Output is always
 * "\n"-joined with a single trailing newline, turning CRLF and CR into LF;
 * VT, FF, NEL, U+2028 and the like stay inside their line.
 *
 * Byte-identical to the CLI's markdown_io.insert_connection_line;
 * schema/connection-append-parity.json pins both, like #318/#338 did for
 * slugs.
 */
export function insertConnectionLine(content: string, line: string): string {
  const lines = splitConnectionLines(content);
  // Scan only the body: frontmatter lines (a YAML block scalar holding
  // "  ```" or "  ## Connections") must not open a fence or a section.
  const start = noteBodyStartLine(content);
  const section = connectionsSection(lines, start);
  if (section === null) {
    // When the body ends inside an unclosed fence or HTML block, the new
    // heading would land inside it too, so close it first.
    // A type 6/7 HTML block ("" closer) is ended by the blank line below.
    const open = unclosedBlockCloser(lines, start);
    const closer = open ? "\n" + open : "";
    return content.trimEnd() + closer + "\n\n## Connections\n\n" + line + "\n";
  }
  if (section.end < lines.length) {
    lines.splice(section.end, 0, line);
  } else {
    // The section runs to EOF. If it ends inside an unclosed fence or HTML
    // block, close it (a blank line for HTML types 6 and 7) so the appended
    // edge is outside it.
    const open = unclosedBlockCloser(lines, start);
    if (open !== null) lines.push(open);
    lines.push(line);
  }
  return lines.join("\n") + "\n";
}

export function buildNote(
  metadata: Record<string, unknown>,
  body: string,
  connections?: Connection[]
): string {
  let content = body;
  if (connections && connections.length > 0) {
    const connectionLines = connections.map(buildConnectionLine).join("\n");
    // The section is found by connectionsSection, the same rule as
    // parseConnections and insertConnectionLine: an exact heading outside a
    // fenced code block or HTML block. `content` is a body (no
    // frontmatter). A substring test matched mid-line mentions (prose,
    // code samples) and replaced the text after them (#732).
    const lines = splitConnectionLines(content);
    const found = connectionsSection(lines);
    if (found === null) {
      const open = unclosedBlockCloser(lines);
      const closer = open ? "\n" + open : "";
      content = content.trimEnd() + closer + `\n\n## Connections\n\n${connectionLines}\n`;
    } else {
      const start = found.heading;
      const end = found.end;
      const section = ["## Connections", "", ...connectionLines.split("\n"), ""];
      content = [...lines.slice(0, start), ...section, ...lines.slice(end)].join("\n");
      if (end === lines.length) content = content.trimEnd() + "\n";
    }
  }
  // Hand gray-matter a file object, not a string: given a string,
  // matter.stringify first parses it as a note with the default engines, so a
  // body that opens with `---js` would be evaluated and a body that opens with
  // a YAML block would be merged into the frontmatter.
  const out = matter.stringify({ content }, metadata, YAML_ONLY);
  // With no metadata gray-matter writes no frontmatter block, and a body that
  // opens with `---` would become the note's first line: read back as its
  // frontmatter, or refused by the hub. An empty block keeps it body text.
  if (Object.keys(metadata).length === 0 && /^\uFEFF*---/.test(content)) {
    return `---\n---\n${out}`;
  }
  return out;
}
