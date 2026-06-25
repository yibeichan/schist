import matter from "gray-matter";
import type { Connection } from "./types.js";

/** Shared regex for connection lines — exported so sqlite-reader doesn't duplicate it. */
export const CONNECTION_RE = /^-\s+(\S+):\s+(\S+)(?:\s+"([^"]*)")?(?:\s+—\s+(.*))?$/;

export function parseConnections(body: string): Connection[] {
  const connections: Connection[] = [];
  let inSection = false;
  for (const line of body.split("\n")) {
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

const HASHTAG_AT_START_RE = /^#[\p{L}\p{N}_-]+/u;

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
