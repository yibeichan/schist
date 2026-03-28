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

export function parseNote(content: string): {
  metadata: Record<string, unknown>;
  body: string;
  connections: Connection[];
} {
  const parsed = matter(content);
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
