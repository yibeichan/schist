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

export function parseConnections(body: string): Connection[] {
  const connections: Connection[] = [];
  let inSection = false;
  for (const line of splitLinesLikePython(body)) {
    const stripped = line.trim();
    if (stripped.startsWith("## Connections")) {
      inSection = true;
      continue;
    }
    if (inSection && stripped.startsWith("## ")) break;
    if (inSection) {
      const match = stripped.match(CONNECTION_RE);
      if (match) {
        connections.push({
          type: match[1],
          target: match[2],
          context: match[3] || match[4] || undefined,
        });
      }
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

export function parseNote(content: string): {
  metadata: Record<string, unknown>;
  body: string;
  connections: Connection[];
} {
  const parsed = matter(patchFrontmatterFlowHashtags(content));
  const connections = parseConnections(parsed.content);
  return {
    metadata: parsed.data as Record<string, unknown>,
    body: parsed.content,
    connections,
  };
}

function sanitizeContext(context: string): string {
  // Remove patterns that look like connection entries FIRST (multiline match must run before \n removal)
  let safe = context.replace(/^-\s+\S+:\s+/gm, "");
  // Then normalize newlines to spaces
  safe = safe.replace(/\n/g, " ");
  // Strip embedded double-quotes entirely: the context field is delimited by "..."
  // in the serialised format (regex: [^"]*) so any " inside would break parsing.
  // Replace with single-quote to preserve readability of quoted speech.
  safe = safe.replace(/"/g, "'");
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
 * section. Output is always "\n"-joined with a single trailing newline,
 * healing CRLF and exotic separators on write like stripConnectionsTo does.
 *
 * Byte-identical to the CLI's markdown_io.insert_connection_line;
 * schema/connection-append-parity.json pins both, like #318/#338 did for
 * slugs.
 */
export function insertConnectionLine(content: string, line: string): string {
  const lines = splitLinesLikePython(content);
  let sectionFound = false;
  let inSection = false;
  let insertIdx: number | null = null;
  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();
    if (!sectionFound && stripped.startsWith("## Connections")) {
      sectionFound = true;
      inSection = true;
      continue;
    }
    if (inSection && stripped.startsWith("## ")) {
      insertIdx = i;
      break;
    }
  }
  if (!sectionFound) {
    return content.trimEnd() + "\n\n## Connections\n\n" + line + "\n";
  }
  if (insertIdx === null) {
    lines.push(line);
  } else {
    lines.splice(insertIdx, 0, line);
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
    if (content.includes("## Connections")) {
      content = content.replace(
        /## Connections[\s\S]*?(?=\n## |\s*$)/,
        `## Connections\n\n${connectionLines}\n`
      );
    } else {
      content = content.trimEnd() + `\n\n## Connections\n\n${connectionLines}\n`;
    }
  }
  return matter.stringify(content, metadata);
}
