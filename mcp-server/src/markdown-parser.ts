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
 * Python's \s but not under JS's (#338). Semantics are otherwise identical.
 * schema/connection-line-parity.json pins this against
 * cli/schist/ingest.py's CONNECTION_RE.
 */
export const CONNECTION_RE = new RegExp(
  `^-${WS_CLASS}+(${NON_WS_CLASS}+):${WS_CLASS}+(${NON_WS_CLASS}+)` +
  `(?:${WS_CLASS}+"([^"]*)")?(?:${WS_CLASS}+—${WS_CLASS}+(.*))?$`
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
 * - A heading is a line of 0-3 spaces, then exactly `## Connections`, then
 *   optional spaces or tabs. Four spaces or a tab make an indented code block.
 * - A fence opens on a line of 0-3 spaces then 3+ backticks or 3+ tildes, and
 *   closes on a later line of 0-3 spaces, then the same character at least as
 *   many times, then optional spaces or tabs. An unclosed fence runs to the
 *   end. Fence lines and lines inside a fence are never a heading, never an
 *   edge, and never end the section.
 * - The section ends at the next line outside a fence whose trimmed text
 *   starts with `## `.
 */
const CONNECTIONS_HEADING_RE = /^ {0,3}## Connections[ \t]*$/;
const FENCE_OPEN_RE = /^ {0,3}(`{3,}|~{3,})/;
const FENCE_CLOSE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;

/** True when `line` (already split) has the shape of a connections heading.
 * It says nothing about fences; connectionsSection checks both. */
export function isConnectionsHeading(line: string): boolean {
  return CONNECTIONS_HEADING_RE.test(line);
}

function scanFences(lines: string[]): { flags: boolean[]; open: string } {
  const flags: boolean[] = [];
  let fenceChar = "";
  let fenceLen = 0;
  for (const ln of lines) {
    if (!fenceChar) {
      const m = FENCE_OPEN_RE.exec(ln);
      if (m) {
        fenceChar = m[1][0];
        fenceLen = m[1].length;
      }
      flags.push(m !== null);
      continue;
    }
    flags.push(true);
    const m = FENCE_CLOSE_RE.exec(ln);
    if (m && m[1][0] === fenceChar && m[1].length >= fenceLen) {
      fenceChar = "";
      fenceLen = 0;
    }
  }
  return { flags, open: fenceChar.repeat(fenceLen) };
}

/** Per line: true for a fence delimiter or a line inside a fence. */
export function fencedLineFlags(lines: string[]): boolean[] {
  return scanFences(lines).flags;
}

/** The delimiter that closes a fence still open after the last line, or ""
 * when every fence is closed. Writers append it before adding a heading or
 * edge at the end, so the new line is not swallowed by the fence. */
export function unclosedFence(lines: string[]): string {
  return scanFences(lines).open;
}

/**
 * Locate the first connections section in `lines`: the heading index, the
 * end index (the `## ` line that closes the section, or lines.length), and
 * the fenced flags; lines between heading and end whose flag is true must be
 * skipped. Null when no heading sits outside a fence.
 */
export function connectionsSection(
  lines: string[],
): { heading: number; end: number; fenced: boolean[] } | null {
  const fenced = fencedLineFlags(lines);
  let heading = -1;
  for (let i = 0; i < lines.length; i++) {
    if (!fenced[i] && isConnectionsHeading(lines[i])) {
      heading = i;
      break;
    }
  }
  if (heading === -1) return null;
  let end = lines.length;
  for (let i = heading + 1; i < lines.length; i++) {
    if (!fenced[i] && lines[i].trim().startsWith("## ")) {
      end = i;
      break;
    }
  }
  return { heading, end, fenced };
}

/** True when `body` has a connections heading outside a fence. */
export function hasConnectionsHeading(body: string): boolean {
  return connectionsSection(splitLinesLikePython(body)) !== null;
}

export function parseConnections(body: string): Connection[] {
  const connections: Connection[] = [];
  const lines = splitLinesLikePython(body);
  const section = connectionsSection(lines);
  if (section === null) return connections;
  for (let i = section.heading + 1; i < section.end; i++) {
    if (section.fenced[i]) continue;
    const stripped = lines[i].trim();
    const match = stripped.match(CONNECTION_RE);
    if (match) {
      // #415: skip bracket references like `[moltbook]` / `[[wiki-note]]`,
      // exactly as cli/schist/ingest.py parse_connections and tools.ts
      // bodyConnectionEdgeLines do. This parser was the ODD ONE OUT: the
      // index never contains these lines, so get_note showed an "edge"
      // query_graph could never return (reader split-brain). Note only a
      // single-token bracket reaches this check — a multi-word one
      // (`[Moltbook, Jan 2026]`) never matches CONNECTION_RE at all.
      // Pinned by schema/connection-line-parity.json body cases.
      if (match[2].startsWith("[")) continue;
      connections.push({
        type: match[1],
        target: match[2],
        context: match[3] || match[4] || undefined,
      });
    }
  }
  return connections;
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
  const parsed = matter(patchFrontmatterFlowHashtags(content), YAML_ONLY);
  const connections = parseConnections(parsed.content);
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
 * section at EOF when no heading line exists. Scans splitLinesLikePython
 * boundaries — the SAME splitter parseConnections and ingest read with —
 * never an insert regex anchored on a bare "\n". The old regex required
 * `## Connections\n` literally, so a CRLF heading (`## Connections\r\n`,
 * core.autocrlf / Windows checkout) matched nothing, String.replace returned
 * the content unchanged, writeNote deduped the no-op, and add_connection
 * reported success while the edge was silently dropped (#366) — the same
 * shape as the missing-trailing-newline drop (#295). The heading gate is a
 * line-scan, not a substring test, so a mid-line prose mention of
 * "## Connections" gets a real section instead of an edge outside any
 * section. The heading must be exact and outside a fenced code block
 * (connectionsSection, the rule parseConnections reads with), so a
 * `## Connections` shown in a fence never receives an edge. Output is always "\n"-joined with a single trailing newline,
 * healing CRLF and exotic separators on write like stripConnectionsTo does.
 *
 * Byte-identical to the CLI's markdown_io.insert_connection_line;
 * schema/connection-append-parity.json pins both, like #318/#338 did for
 * slugs.
 */
export function insertConnectionLine(content: string, line: string): string {
  const lines = splitLinesLikePython(content);
  const section = connectionsSection(lines);
  if (section === null) {
    // When the only `## Connections` sits in an unclosed fence, the new
    // heading would land inside that fence too, so close it first.
    const fence = unclosedFence(lines);
    const closer = fence ? "\n" + fence : "";
    return content.trimEnd() + closer + "\n\n## Connections\n\n" + line + "\n";
  }
  if (section.end < lines.length) {
    lines.splice(section.end, 0, line);
  } else {
    // The section runs to EOF. If it ends inside an unclosed fence, close
    // the fence so the appended edge is outside it.
    const fence = unclosedFence(lines);
    if (fence) lines.push(fence);
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
    // fenced code block. A substring test matched mid-line mentions (prose,
    // code samples) and replaced the text after them (#732).
    const lines = splitLinesLikePython(content);
    const found = connectionsSection(lines);
    if (found === null) {
      const fence = unclosedFence(lines);
      const closer = fence ? "\n" + fence : "";
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
