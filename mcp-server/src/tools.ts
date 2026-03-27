import * as fs from "fs/promises";
import * as path from "path";
import { execFile as execFileCb } from "child_process";
import { promisify } from "util";
import * as sqliteReader from "./sqlite-reader.js";
import { writeNote } from "./git-writer.js";
import { buildNote, buildConnectionLine, parseConnections } from "./markdown-parser.js";
import type { VaultConfig, ToolError } from "./types.js";

const execFile = promisify(execFileCb);

function slugify(title: string): string {
  return title
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .trim();
}

function today(): string {
  return new Date().toISOString().split("T")[0];
}

export async function loadVaultConfig(vaultRoot: string): Promise<VaultConfig> {
  const configPath = path.join(vaultRoot, "schist.yaml");
  const content = await fs.readFile(configPath, "utf-8");

  const getField = (key: string, def: string): string => {
    const match = content.match(new RegExp(`^${key}:\\s*["']?(.+?)["']?\\s*$`, "m"));
    return match ? match[1].trim() : def;
  };

  const getList = (key: string, def: string[]): string[] => {
    const blockMatch = content.match(new RegExp(`^${key}:\\s*\\n((?:[ \\t]+-[ \\t]+.+\\n?)+)`, "m"));
    if (blockMatch) {
      return blockMatch[1]
        .split("\n")
        .map((l) => l.replace(/^\s+-\s+["']?/, "").replace(/["']?\s*$/, "").trim())
        .filter(Boolean);
    }
    const inlineMatch = content.match(new RegExp(`^${key}:\\s*\\[([^\\]]+)\\]`, "m"));
    if (inlineMatch) {
      return inlineMatch[1]
        .split(",")
        .map((s) => s.trim().replace(/["']/g, ""))
        .filter(Boolean);
    }
    return def;
  };

  return {
    name: getField("name", path.basename(vaultRoot)),
    path: vaultRoot,
    directories: getList("directories", ["notes", "papers", "concepts"]),
    connectionTypes: getList("connection_types", [
      "extends", "contradicts", "supports", "replicates",
      "applies-method-of", "reinterprets", "related",
    ]),
    statuses: getList("statuses", ["draft", "review", "final", "archived"]),
    writeBranch: getField("write_branch", "drafts"),
  };
}

function triggerIngestion(vaultRoot: string): void {
  const schist_repo = path.resolve(
    path.dirname(new URL(import.meta.url).pathname),
    "../../"
  );
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  const scriptPath = path.join(schist_repo, "ingestion", "ingest.py");

  execFile("python3", [scriptPath, "--vault", vaultRoot, "--db", dbPath], {
    cwd: vaultRoot,
  }).catch((err: unknown) => {
    console.error("[schist] ingestion failed:", err);
  });
}

export async function search_notes(
  vaultRoot: string,
  args: { query: string; limit?: number; status?: string; tags?: string[] }
): Promise<unknown> {
  try {
    return sqliteReader.searchNotes(vaultRoot, args.query, {
      limit: args.limit,
      status: args.status,
      tags: args.tags,
    });
  } catch (e: unknown) {
    return { error: "INGEST_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}

export async function get_note(
  vaultRoot: string,
  args: { id: string }
): Promise<unknown> {
  try {
    const filePath = path.join(vaultRoot, args.id);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Note path is outside vault root" } satisfies ToolError;
    }

    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Note not found: ${args.id}` } satisfies ToolError;
    }

    const { parseNote } = await import("./markdown-parser.js");
    const { metadata, body, connections } = parseNote(content);
    const meta = metadata as Record<string, unknown>;

    return {
      id: args.id,
      title: (meta.title as string) ?? "",
      date: (meta.date as string) ?? "",
      status: (meta.status as string) ?? "draft",
      tags: Array.isArray(meta.tags) ? meta.tags : [],
      concepts: Array.isArray(meta.concepts) ? meta.concepts : [],
      body,
      connections,
    };
  } catch (e: unknown) {
    return { error: "INGEST_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}

export async function create_note(
  vaultRoot: string,
  args: {
    title: string;
    body: string;
    tags?: string[];
    concepts?: string[];
    status?: string;
    connections?: Array<{ target: string; type: string; context?: string }>;
    directory?: string;
  },
  config: VaultConfig
): Promise<unknown> {
  try {
    const directory = args.directory ?? "notes";
    if (directory.includes("..") || path.isAbsolute(directory)) {
      return {
        error: "VALIDATION_ERROR",
        message: "Invalid directory: must be relative and not contain ..",
      } satisfies ToolError;
    }
    if (!config.directories.includes(directory)) {
      return {
        error: "VALIDATION_ERROR",
        message: `Directory "${directory}" not configured. Allowed: ${config.directories.join(", ")}`,
      } satisfies ToolError;
    }

    const slug = slugify(args.title);
    const date = today();
    const filename = `${date}-${slug}.md`;
    const relPath = `${directory}/${filename}`;

    const metadata: Record<string, unknown> = {
      title: args.title,
      date,
      tags: args.tags ?? [],
      concepts: args.concepts ?? [],
      status: args.status ?? "draft",
      source_agent: "mcp",
    };

    const noteContent = buildNote(metadata, args.body, args.connections);
    const result = await writeNote(vaultRoot, relPath, noteContent);

    triggerIngestion(vaultRoot);

    return { id: relPath, path: relPath, commitSha: result.commitSha };
  } catch (e: unknown) {
    const err = e as Partial<ToolError>;
    if (err?.error) return e;
    return { error: "GIT_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}

export async function add_connection(
  vaultRoot: string,
  args: { source: string; target: string; type: string; context?: string }
): Promise<unknown> {
  try {
    const filePath = path.join(vaultRoot, args.source);
    const absVaultRoot = path.resolve(vaultRoot);
    const absFilePath = path.resolve(filePath);
    if (!absFilePath.startsWith(absVaultRoot + path.sep)) {
      return { error: "PATH_TRAVERSAL", message: "Source path is outside vault root" } satisfies ToolError;
    }

    let content: string;
    try {
      content = await fs.readFile(filePath, "utf-8");
    } catch {
      return { error: "NOT_FOUND", message: `Source note not found: ${args.source}` } satisfies ToolError;
    }

    const conn = { target: args.target, type: args.type, context: args.context };
    const connLine = buildConnectionLine(conn);

    let newContent: string;
    if (content.includes("## Connections")) {
      newContent = content.replace(/(## Connections\n(?:.*\n)*?)(\n## |\s*$)/, (match, section, after) => {
        return section.trimEnd() + "\n" + connLine + "\n" + after;
      });
    } else {
      newContent = content.trimEnd() + "\n\n## Connections\n\n" + connLine + "\n";
    }

    const result = await writeNote(vaultRoot, args.source, newContent);

    return { source: args.source, target: args.target, type: args.type, commitSha: result.commitSha };
  } catch (e: unknown) {
    const err = e as Partial<ToolError>;
    if (err?.error) return e;
    return { error: "GIT_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}

export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number }
): Promise<unknown> {
  try {
    return sqliteReader.listConcepts(vaultRoot, args);
  } catch (e: unknown) {
    return { error: "INGEST_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}

export async function query_graph(
  vaultRoot: string,
  args: { sql: string; params?: unknown[] }
): Promise<unknown> {
  try {
    return sqliteReader.queryGraph(vaultRoot, args.sql, args.params);
  } catch (e: unknown) {
    const err = e as Partial<ToolError>;
    if (err?.error) return e;
    return { error: "INVALID_SQL", message: String(e), details: e } satisfies ToolError;
  }
}

export async function get_context(
  vaultRoot: string,
  args: { depth?: "minimal" | "standard" | "full" }
): Promise<unknown> {
  try {
    return sqliteReader.getContext(vaultRoot, args.depth ?? "standard");
  } catch (e: unknown) {
    return { error: "INGEST_ERROR", message: String(e), details: e } satisfies ToolError;
  }
}
