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
 * The connections rule, shared by every reader and writer of a note's
 * `## Connections` section: parseConnections and insertConnectionLine here,
 * buildNote's section replace, and tools.ts's body validator, concept check
 * and delete-cascade repair. Mirrors cli/schist/ingest.py scan_connections
 * and friends, where the rule is written out in full;
 * schema/connection-line-parity.json pins the two, and
 * tests/connection-marked.test.ts checks every edge either reader finds
 * against marked 12.0.2, the renderer the viewer uses.
 *
 * The rule is conservative by construction: it indexes an edge only where
 * the viewer shows it as a top-level list item under the `## Connections`
 * heading, and when a note holds Markdown whose rendering the rule does not
 * follow, it indexes nothing from that note and reports why
 * (ConnectionScan.problem).
 * - Text: CRLF and CR become LF (normalizeNewlines); the body is
 *   connectionBody(file).
 * - Heading: exactly `## Connections` at column 0, then optional spaces or
 *   tabs, outside a fence. The section ends at the next ATX heading of any
 *   level (0-3 spaces, 1-6 `#`, whitespace or end of line).
 * - Fence: 0-3 spaces, 3+ backticks or tildes, an optional info word of ASCII
 *   letters, digits and `_+.#-`, optional spaces; closed by the same spaces
 *   and run; lines between blank or starting with the same spaces, and none
 *   starting (after spaces and tabs) with ``` or ~~~.
 * - Problems from the body start to the section end: a non-Markdown line
 *   separator (VT, FF, U+001C-U+001F, NEL, LS, PS); any other line starting
 *   with ``` or ~~~; a line starting with `<`; a line starting with `[` that
 *   could open a link reference definition. In the section also: `>`, a
 *   line indented by a tab or 4+ spaces, a line of only `-=*_+|:` and
 *   whitespace (SLUG_WS_CHARS at either end ignored), a list marker
 *   followed by a tab or 4+ spaces or by content starting with `#` or `>`.
 *   A fence-like line indented 4+ spaces is never a fence, so it is a
 *   problem anywhere.
 * - Edge line: `-` at column 0, 1-3 spaces, CONNECTION_RE on the trimmed
 *   line, a type starting with an ASCII letter or digit, a target not
 *   starting with `[`.
 * Every step is one pass over the lines.
 */
/** The connection rule's version, equal to cli/schist/ingest.py's
 * CONNECTIONS_RULE_VERSION (tests/connection-rule-version.test.ts pins the
 * two). `schist doctor` reads it from the built server to report a server
 * and CLI that read ## Connections sections differently. */
export const CONNECTIONS_RULE_VERSION = 2;

const CONNECTIONS_HEADING_RE = /^## Connections[ \t]*$/;
const SECTION_END_RE = new RegExp(`^ {0,3}#{1,6}(?:${WS_CLASS}|$)`);
const FENCE_OPEN_RE = /^( {0,3})(`{3,}|~{3,})[A-Za-z0-9_+.#-]* *$/;
const FENCE_LIKE_RE = /^[ \t]*(?:```|~~~)/;
const LIST_MARKER_WS_RE = /^[ \t]*(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)/;
const LIST_MARKER_WS_STICKY = /[ \t]*(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)/y;
const EDGE_PREFIX_RE = /^- {1,3}[^ \t]/;
const NON_MARKDOWN_SEPARATOR_RE = /[\u000b\u000c\u001c-\u001f\u0085\u2028\u2029]/;
const RULE_LINE_RE = /^[-=*_+|: \t]+$/;
const ASCII_ALNUM_RE = /^[A-Za-z0-9]/;

/** What the connection rule finds in a list of lines; see ingest.py
 * ConnectionScan. `heading` is -1 when there is none. */
export interface ConnectionScan {
  heading: number;
  end: number;
  edges: { index: number; connection: Connection }[];
  problem: { index: number; reason: string } | null;
}

/** True when `line` (already split) has the shape of the connections
 * heading. It says nothing about fences; scanConnections checks them. */
export function isConnectionsHeading(line: string): boolean {
  return CONNECTIONS_HEADING_RE.test(line);
}

/** True when `line` has the shape of an ATX heading, which ends a
 * connections section. */
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

/** Turn CRLF and CR into LF, as Python's text-mode file reads (ingest's)
 * do. Every connection reader and writer starts here, and note files are
 * read through readNoteText so the server sees what ingest sees. */
export function normalizeNewlines(text: string): string {
  return text.replace(/\r\n?/g, "\n");
}

// UTF-8 with no replacement: an invalid byte throws, as Python's
// read_text(encoding="utf-8") raises (ingest then skips the note). ignoreBOM
// keeps a leading U+FEFF in the text, as Python's "utf-8" codec does.
const STRICT_UTF8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

/** A note file that is not valid UTF-8. Ingest skips such a note (no docs
 * row, no edges), so tools refuse to read connections from it or write it. */
export class NoteDecodeError extends Error {
  readonly error = "VALIDATION_ERROR";
}

/** Decode a note file's bytes the way ingest reads them (strict UTF-8), with
 * CRLF and CR turned into LF. `name` is used in the error message. */
export function decodeNoteBytes(bytes: Uint8Array, name: string): string {
  let text: string;
  try {
    text = STRICT_UTF8.decode(bytes);
  } catch {
    throw new NoteDecodeError(
      `${name} is not valid UTF-8, so ingest skips it and indexes nothing from it; ` +
      "re-save the file as UTF-8 first. Nothing was written.",
    );
  }
  return normalizeNewlines(text);
}

/** Python's str.strip(" \t") == "": a blank line. */
export function isBlank(line: string): boolean {
  for (let i = 0; i < line.length; i++) {
    if (line[i] !== " " && line[i] !== "\t") return false;
  }
  return true;
}

/** Python's str.lstrip(" \t") */
function lstripSpaceTab(line: string): string {
  let i = 0;
  while (i < line.length && (line[i] === " " || line[i] === "\t")) i++;
  return line.slice(i);
}

/** If lines[i] opens a fence the rule accepts, the index of its closing
 * line, else -1. */
function fenceEnd(lines: string[], i: number): number {
  const m = FENCE_OPEN_RE.exec(lines[i]);
  if (m === null) return -1;
  const prefix = m[1];
  const run = m[2];
  for (let j = i + 1; j < lines.length; j++) {
    const ln = lines[j];
    if (isBlank(ln)) continue;
    if (!ln.startsWith(prefix)) return -1;
    if (FENCE_LIKE_RE.test(ln)) {
      const rest = ln.slice(prefix.length);
      if (rest.startsWith(run) && /^ *$/.test(rest.slice(run.length))) return j;
      return -1;
    }
  }
  return -1;
}

/** For a line starting with `[`: false only when its first unescaped `]`
 * comes before any other `[` and is not followed by `:`. ingest.py
 * _may_open_definition is its twin. */
function mayOpenDefinition(s: string): boolean {
  let i = 1;
  while (i < s.length) {
    const c = s[i];
    if (c === "\\") {
      i += 2;
      continue;
    }
    if (c === "[") return true;
    if (c === "]") return s[i + 1] === ":";
    i++;
  }
  return true;
}

function hex4(ch: string): string {
  return ch.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0");
}

/** Why `line` stops a note's edges from being indexed, or null. ingest.py
 * _line_problem is its twin; the reasons are the same strings. */
function lineProblem(line: string, inSection: boolean): string | null {
  const sep = NON_MARKDOWN_SEPARATOR_RE.exec(line);
  if (sep !== null) {
    return (
      `the line holds U+${hex4(sep[0])}, which some readers take as a ` +
      "line break and Markdown does not"
    );
  }
  const s = lstripSpaceTab(line);
  if (s.startsWith("```") || s.startsWith("~~~")) {
    return (
      "a fence the connection rule does not follow (it must open with at " +
      "most 3 spaces of indentation and close with the same run at the " +
      "same indentation, with an info word of letters, digits and _+.#- " +
      "only)"
    );
  }
  if (s.startsWith("<")) return 'the line starts with "<" (HTML)';
  if (s.startsWith("[") && mayOpenDefinition(s)) {
    return 'the line starts like a link reference definition ("[label]:")';
  }
  if (!inSection || s === "") return null;
  if (s.startsWith(">")) return "a block quote in the Connections section";
  const lead = line.slice(0, line.length - s.length);
  if (lead.includes("\t") || lead.length >= 4) {
    return "a line indented by a tab or 4+ spaces in the Connections section";
  }
  // Whitespace at either end does not count: marked trims the end of a list's
  // last item with JS trimEnd(), so `=` + U+FEFF underlines it.
  const rule = trimConnectionWs(s);
  if (rule !== "" && RULE_LINE_RE.test(rule)) {
    return (
      'a line of only "-", "=", "*", "_", "+", "|" or ":" in the ' +
      "Connections section (a heading underline, rule or table)"
    );
  }
  const m = LIST_MARKER_WS_RE.exec(line);
  if (m !== null && (m[1].includes("\t") || m[1].length >= 4)) {
    return "a list marker followed by a tab or 4+ spaces in the Connections section";
  }
  if (m !== null) {
    // Past nested markers too (`- - ## x` is a heading in an item).
    let pos = m[0].length;
    for (;;) {
      LIST_MARKER_WS_STICKY.lastIndex = pos;
      const nested = LIST_MARKER_WS_STICKY.exec(line);
      if (nested === null) break;
      pos = LIST_MARKER_WS_STICKY.lastIndex;
    }
    if (line[pos] !== "#" && line[pos] !== ">") return null;
    return "a list item that holds a heading or a block quote in the Connections section";
  }
  return null;
}

/**
 * The edge `line` carries under the connection rule, or null: `-` at column
 * 0, 1-3 spaces, CONNECTION_RE on the line trimmed with trimConnectionWs, a
 * type starting with an ASCII letter or digit, and a target that is not a
 * bracket reference (`[moltbook]`, `[[wiki-note]]`, #415). ingest.py
 * connection_line_edge applies the same test.
 */
export function connectionLineMatch(line: string): RegExpMatchArray | null {
  if (!EDGE_PREFIX_RE.test(line)) return null;
  const match = trimConnectionWs(line).match(CONNECTION_RE);
  if (!match || !ASCII_ALNUM_RE.test(match[1]) || match[2].startsWith("[")) return null;
  return match;
}

/** Apply the connection rule to `lines` (LF-split), ignoring lines before
 * `start` (a writer passes noteBodyStartLine). One pass. ingest.py
 * scan_connections is its twin. */
export function scanConnections(lines: string[], start = 0): ConnectionScan {
  const n = lines.length;
  let heading = -1;
  let end = n;
  let edges: ConnectionScan["edges"] = [];
  let problem: ConnectionScan["problem"] = null;
  let i = start;
  scan: while (i < n) {
    const line = lines[i];
    const inSection = heading !== -1;
    if (inSection && isSectionEnd(line)) {
      end = i;
      break;
    }
    const close = fenceEnd(lines, i);
    if (close !== -1) {
      for (let j = i; j <= close; j++) {
        if (NON_MARKDOWN_SEPARATOR_RE.test(lines[j])) {
          problem = { index: j, reason: lineProblem(lines[j], false)! };
          break scan;
        }
      }
      i = close + 1;
      continue;
    }
    if (!inSection && isConnectionsHeading(line)) {
      heading = i;
      i++;
      continue;
    }
    const reason = lineProblem(line, inSection);
    if (reason !== null) {
      problem = { index: i, reason };
      break;
    }
    if (inSection) {
      const m = connectionLineMatch(line);
      if (m) {
        edges.push({
          index: i,
          connection: { type: m[1], target: m[2], context: m[3] || m[4] || undefined },
        });
      }
    }
    i++;
  }
  if (problem !== null) {
    edges = [];
    if (heading === -1) {
      for (let k = problem.index + 1; k < n; k++) {
        if (isConnectionsHeading(lines[k])) {
          heading = k;
          break;
        }
      }
    }
    end = n;
    if (heading !== -1) {
      for (let k = Math.max(heading, problem.index) + 1; k < n; k++) {
        if (isSectionEnd(lines[k])) {
          end = k;
          break;
        }
      }
    }
  }
  return { heading, end, edges, problem };
}

/** True when `body` has a connections heading line (found even after a
 * problem, so a concept body with one is refused either way). */
export function hasConnectionsHeading(body: string): boolean {
  return scanConnections(splitConnectionLines(normalizeNewlines(body))).heading !== -1;
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
 * here. Turn CRLF and CR into LF (normalizeNewlines; Python's text-mode
 * read does the same before ingest sees the file), strip Python whitespace,
 * detect YAML (`---`),
 * then JSON (`{`), then TOML (`+++`) on the first line, and close at the next
 * boundary line. 0 when there is no frontmatter (a BOM before `---` means
 * none) or it is never closed. gray-matter's own split differs on `-----`,
 * `---foo`, leading blank lines and a BOM, so connection paths slice with
 * this instead (connectionBody, noteBodyStartLine).
 * schema/frontmatter-boundary-parity.json pins both languages.
 */
export function frontmatterLength(content: string): number {
  // CR alone becomes LF too (same length, so offsets map back as below).
  const norm = normalizeNewlines(content);
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

/** The note body the connection rule reads, and the text ingest stores in
 * docs.body for the viewer to render: CRLF and CR turned into LF, the text
 * after the frontmatter's closing line (frontmatterLength), without leading
 * and trailing blank lines (lines of only spaces and tabs). ingest.py
 * connection_body is its twin. */
export function connectionBody(content: string): string {
  const text = normalizeNewlines(content);
  const lines = text.slice(frontmatterLength(text)).split("\n");
  let a = 0;
  let b = lines.length;
  while (a < b && isBlank(lines[a])) a++;
  while (b > a && isBlank(lines[b - 1])) b--;
  return lines.slice(a, b).join("\n");
}

/** Number of splitConnectionLines lines that belong to the frontmatter
 * (frontmatterLength). Writers pass it as scanConnections's `start`. */
export function noteBodyStartLine(content: string): number {
  return splitConnectionLines(content.slice(0, frontmatterLength(content))).length;
}

/** The connection rule applied to a whole note file's text: its lines
 * (LF-split after normalizeNewlines) and the scan of its body, with line
 * indexes into those lines. Writers use this; it finds the same edges as
 * parseConnections(connectionBody(text)). ingest.py scan_note_connections
 * is its twin. */
export function scanNoteConnections(content: string): { lines: string[]; scan: ConnectionScan } {
  const text = normalizeNewlines(content);
  const lines = splitConnectionLines(text);
  return { lines, scan: scanConnections(lines, noteBodyStartLine(text)) };
}

/** Edges of a note body's `## Connections` section, each with its index in
 * splitConnectionLines(normalizeNewlines(body)); empty when the rule finds a
 * problem. ingest.py parse_connection_lines is its twin. */
export function parseConnectionLines(body: string): { index: number; connection: Connection }[] {
  return scanConnections(splitConnectionLines(normalizeNewlines(body))).edges;
}

export function parseConnections(body: string): Connection[] {
  return parseConnectionLines(body).map((e) => e.connection);
}

// A `#tag` in a flow collection: `#`, then characters other than the shared
// whitespace union (not JS's own \s), `,`, `]` and `}`. Sticky, matched at
// a position. ingest.py _FLOW_HASHTAG_RE is its twin.
const FLOW_HASHTAG_RE = new RegExp(`#[^${SLUG_WS_CHARS},\\]\\}]+`, "y");

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
      FLOW_HASHTAG_RE.lastIndex = i;
      const match = FLOW_HASHTAG_RE.exec(line);
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

/** Quote `#tags` in YAML flow collections of the frontmatter before YAML
 * parsing: only the text before frontmatterLength (python-frontmatter's
 * split, which ingest uses), and only when it is YAML. The body is never
 * changed. ingest.py patch_frontmatter_flow_hashtags is its twin. */
function patchFrontmatterFlowHashtags(content: string): string {
  const offset = frontmatterLength(content);
  const head = content.slice(0, offset);
  let lead = 0;
  while (lead < head.length && PY_WHITESPACE.includes(head[lead])) lead++;
  if (!head.startsWith("---", lead)) return content;
  const lines = head.split("\n");
  let patched = false;
  const patchedLines = lines.map((line) => {
    const next = quoteFlowHashtags(line);
    if (next !== line) patched = true;
    return next;
  });
  return patched ? patchedLines.join("\n") + content.slice(offset) : content;
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
  connectionsProblem: { line: number; reason: string } | null;
} {
  // CRLF and CR become LF first, as ingest's text-mode read does, so the
  // frontmatter split and the connections are ingest's.
  const patched = patchFrontmatterFlowHashtags(normalizeNewlines(content));
  const parsed = matter(patched, YAML_ONLY);
  // Connections come from ingest's reading of the file (scanNoteConnections,
  // the same edges as connectionBody + parseConnections), not gray-matter's
  // content, so get_note and the index agree on notes where the two
  // frontmatter splits differ (`-----`, `---foo`, a BOM, leading blank
  // lines). `body` stays gray-matter's: writers round-trip it.
  const { scan } = scanNoteConnections(patched);
  return {
    metadata: parsed.data as Record<string, unknown>,
    body: parsed.content,
    connections: scan.edges.map((e) => e.connection),
    connectionsProblem: connectionsProblemOf(scan),
  };
}

/** A rewrite refused because this server and ingest would not read the
 * note's frontmatter as the same block: nothing written. normalizeError
 * turns it into a VALIDATION_ERROR. */
export class FrontmatterSplitError extends Error {
  readonly error = "VALIDATION_ERROR";
}

// What may follow gray-matter's split on the closing line before ingest's
// split: the rest of a `---` closing line, which ingest takes as part of it.
const CLOSING_LINE_TAIL_RE = /^[ \t\v\f]*\n?$/;

/**
 * Why parseNote's frontmatter (gray-matter's split) is not the block ingest
 * reads (frontmatterLength), or null when they are the same block. They
 * differ on a note that opens with a BOM, blank lines, `-----`, `---yaml`,
 * a JSON `{` line or a TOML `+++` line, and on one whose frontmatter closes
 * on a line like `-----` or `---foo`. Rewriting such a note from parseNote's
 * metadata and body would change the frontmatter ingest reads, so writers
 * refuse it (parseNoteForRewrite).
 */
export function frontmatterSplitMismatch(content: string): string | null {
  const patched = patchFrontmatterFlowHashtags(normalizeNewlines(content));
  const parsed = matter(patched, YAML_ONLY);
  const ours = patched.endsWith(parsed.content) ? patched.length - parsed.content.length : -1;
  const ingest = frontmatterLength(patched);
  if (ours === ingest) return null;
  if (ours > 0 && ingest > ours && CLOSING_LINE_TAIL_RE.test(patched.slice(ours, ingest))) return null;
  if (ours === 0) return "the index reads a frontmatter block at the start of the note that this server reads as body text";
  if (ingest === 0) {
    return "this server reads a frontmatter block that the index does not (the note opens with a BOM or a line other than `---`)";
  }
  return "this server and the index end the note's frontmatter at different lines";
}

/** parseNote for a writer that rewrites the note from its metadata and body:
 * throws FrontmatterSplitError when frontmatterSplitMismatch finds one. */
export function parseNoteForRewrite(content: string, action: string): ReturnType<typeof parseNote> {
  const parsed = parseNote(content);
  const mismatch = frontmatterSplitMismatch(content);
  if (mismatch !== null) {
    throw new FrontmatterSplitError(
      `${action} refused: ${mismatch}, so rewriting it would change the frontmatter the index reads. ` +
      "Edit the file so it opens with a plain `---` line (no BOM or blank lines before it) and its " +
      "frontmatter closes with a `---` line, then retry. Nothing was written.",
    );
  }
  return parsed;
}

/** A scan's problem as the tools report it (1-based line in the scanned
 * lines), or null when the problem does not matter: there is no
 * Connections heading, so nothing would be indexed anyway. */
export function connectionsProblemOf(scan: ConnectionScan): { line: number; reason: string } | null {
  if (scan.problem === null || scan.heading === -1) return null;
  return { line: scan.problem.index + 1, reason: scan.problem.reason };
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

/** A write that would leave the note's indexed edges different from what
 * the writer meant: refused, nothing written. normalizeError turns it into
 * a VALIDATION_ERROR. */
export class ConnectionWriteError extends Error {
  readonly error = "VALIDATION_ERROR";
}

function edgeKey(c: Connection): string {
  return `${c.type}\u0000${c.target}\u0000${c.context ?? ""}`;
}

/** True when `a` and `b` hold the same edges, counting repeats. */
export function sameEdges(a: Connection[], b: Connection[]): boolean {
  if (a.length !== b.length) return false;
  const count = new Map<string, number>();
  for (const c of a) count.set(edgeKey(c), (count.get(edgeKey(c)) ?? 0) + 1);
  for (const c of b) {
    const k = edgeKey(c);
    const v = count.get(k) ?? 0;
    if (v === 0) return false;
    count.set(k, v - 1);
  }
  return true;
}

// A refusal names at most this many connections, each cut to this many
// characters, so a note with a huge Connections section cannot make the
// error itself huge. markdown_io.py's _describe_edges uses the same limits.
const DESCRIBE_EDGES_MAX = 20;
const DESCRIBE_EDGE_CHARS = 200;

function describeEdges(edges: Connection[]): string {
  if (edges.length === 0) return "none";
  const shown = edges.slice(0, DESCRIBE_EDGES_MAX).map((c) => {
    // Code points, as Python counts, so the two cut a long target alike.
    const s = Array.from(`${c.type}: ${c.target}`);
    return s.length > DESCRIBE_EDGE_CHARS ? s.slice(0, DESCRIBE_EDGE_CHARS).join("") + "…" : s.join("");
  });
  const more = edges.length - shown.length;
  return shown.join(", ") + (more > 0 ? `, … and ${more} more` : "");
}

/** Throw ConnectionWriteError unless the note file `text` reads exactly
 * `expected` under the connection rule. Every writer runs this on what it is
 * about to write. ingest.py's writers (markdown_io) do the same. */
export function verifyNoteConnections(text: string, expected: Connection[], action: string): void {
  const { scan } = scanNoteConnections(text);
  const got = scan.edges.map((e) => e.connection);
  if (sameEdges(got, expected)) return;
  const problem = connectionsProblemOf(scan);
  throw new ConnectionWriteError(
    `${action} refused: the note would be indexed with connections ` +
    `[${describeEdges(got)}], not [${describeEdges(expected)}]` +
    (problem ? `; line ${problem.line}: ${problem.reason}, so no connections are indexed from this note` : "") +
    ". Nothing was written.",
  );
}

/** The edge a connection line the writers build carries, or a
 * ConnectionWriteError when the rule would not read it as one. */
export function edgeOfLine(line: string): Connection {
  const m = connectionLineMatch(line);
  if (!m) throw new ConnectionWriteError(`"${line}" is not a line the connection rule reads as an edge. Nothing was written.`);
  return { type: m[1], target: m[2], context: m[3] || m[4] || undefined };
}

/** Drop trailing blank lines (spaces and tabs only). */
function trimTrailingBlankLines(lines: string[]): string[] {
  let b = lines.length;
  while (b > 0 && isBlank(lines[b - 1])) b--;
  return lines.slice(0, b);
}

/**
 * Insert `line` into `content`'s `## Connections` section after its last
 * non-blank line, or add the section at the end of the note when there is
 * none. Reads with scanNoteConnections (the rule ingest reads with, from the
 * body only: a YAML block scalar holding "## Connections" is never a
 * section). Output is "\n"-joined with a single trailing newline; CRLF and
 * CR become LF.
 *
 * The result is checked: it must read as the note's edges plus the new one,
 * or ConnectionWriteError is thrown and nothing is written (for instance
 * when the note holds Markdown the rule does not follow, so no edge of it is
 * indexed).
 *
 * Byte-identical to the CLI's markdown_io.insert_connection_line;
 * schema/connection-append-parity.json pins both.
 */
export function insertConnectionLine(content: string, line: string): string {
  const added = edgeOfLine(line);
  const { lines, scan } = scanNoteConnections(content);
  const before = scan.edges.map((e) => e.connection);
  let out: string;
  if (scan.problem !== null) {
    throw new ConnectionWriteError(
      `adding the connection refused: line ${scan.problem.index + 1}: ${scan.problem.reason}; ` +
      "the connection rule indexes no connections from this note, so the new one would not be " +
      "indexed either. Edit the note first. Nothing was written.",
    );
  }
  if (scan.heading === -1) {
    out = trimTrailingBlankLines(lines).join("\n") + "\n\n## Connections\n\n" + line + "\n";
  } else {
    let last = scan.heading;
    for (let i = scan.heading + 1; i < scan.end; i++) if (!isBlank(lines[i])) last = i;
    const insert = last === scan.heading ? ["", line] : [line];
    out = [...lines.slice(0, last + 1), ...insert, ...lines.slice(last + 1)].join("\n") + "\n";
  }
  verifyNoteConnections(out, [...before, added], "adding the connection");
  return out;
}

/**
 * Serialize a note. With `connections`, the body's `## Connections` section
 * (found by the connection rule) is replaced by them, or added at the end.
 * The result is checked: it must read as exactly `connections`, or, without
 * them, as the edges `body` holds; otherwise ConnectionWriteError is thrown.
 */
export function buildNote(
  metadata: Record<string, unknown>,
  body: string,
  connections?: Connection[]
): string {
  let content = body;
  let expected: Connection[];
  if (connections && connections.length > 0) {
    const connectionLines = connections.map(buildConnectionLine);
    expected = connectionLines.map(edgeOfLine);
    const lines = splitConnectionLines(normalizeNewlines(content));
    const found = scanConnections(lines);
    if (found.heading === -1) {
      content = trimTrailingBlankLines(lines).join("\n") + `\n\n## Connections\n\n${connectionLines.join("\n")}\n`;
    } else {
      const section = ["## Connections", "", ...connectionLines, ""];
      const out = [...lines.slice(0, found.heading), ...section, ...lines.slice(found.end)];
      // At the end of the note, drop trailing blank lines (spaces and tabs
      // only, never JS trimEnd()'s wider set) and end with one newline.
      content = found.end === lines.length
        ? trimTrailingBlankLines(out).join("\n") + "\n"
        : out.join("\n");
    }
  } else {
    expected = parseConnections(content);
  }
  // Hand gray-matter a file object, not a string: given a string,
  // matter.stringify first parses it as a note with the default engines, so a
  // body that opens with `---js` would be evaluated and a body that opens with
  // a YAML block would be merged into the frontmatter.
  let out = matter.stringify({ content }, metadata, YAML_ONLY);
  // With no metadata gray-matter writes no frontmatter block, and a body that
  // opens with `---`, or with anything else ingest reads as frontmatter
  // (frontmatterLength: blank lines then `---`, a JSON `{` block, a TOML
  // `+++` block), would become the note's frontmatter, or be refused by the
  // hub. An empty block keeps it body text.
  const empty = Object.keys(metadata).length === 0;
  const wrap = empty && (/^\uFEFF*---/.test(content) || frontmatterLength(content) > 0);
  if (wrap) out = `---\n---\n${out}`;
  // Ingest must read exactly the block written: a `---` line, the YAML
  // (none when wrapping), a `---` line; or no block at all.
  const close = empty && !wrap ? -1 : out.indexOf("\n---\n", 3);
  const written = close === -1 ? 0 : close + 5;
  if ((!empty || wrap) && close === -1 || frontmatterLength(out) !== written) {
    throw new FrontmatterSplitError(
      "writing the note refused: the index would not read the frontmatter that was written. Nothing was written.",
    );
  }
  verifyNoteConnections(out, expected, "writing the note");
  return out;
}
