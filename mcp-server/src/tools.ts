import * as fs from "fs/promises";
import * as path from "path";
import { spawn } from "child_process";
import { fileURLToPath } from "url";
import { load as yamlLoad } from "js-yaml";
import * as sqliteReader from "./sqlite-reader.js";
import { writeNote } from "./git-writer.js";
import { buildNote, buildConnectionLine } from "./markdown-parser.js";
import type { VaultConfig, ToolError } from "./types.js";

function slugify(title: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .replace(/\s+/g, "-")
      .replace(/-+/g, "-")
      .trim() || "untitled"
  );
}

/** Returns the raw slug without the "untitled" fallback — used to detect empty-slug titles */
function rawSlug(title: string): string {
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

/**
 * Normalise any caught value into a plain ToolError object so that
 * JSON.stringify always produces { error, message } — Error.message is
 * non-enumerable and would otherwise be silently dropped.
 */
function normalizeError(e: unknown, fallbackCode = "GIT_ERROR"): ToolError {
  // Already a plain ToolError shape (thrown by git-writer / assertPathSafe)
  if (
    e !== null &&
    typeof e === "object" &&
    "error" in e &&
    "message" in e &&
    typeof (e as Record<string, unknown>).error === "string"
  ) {
    const te = e as Record<string, unknown>;
    return { error: te.error as string, message: te.message as string, details: te.details };
  }
  // Real Error instance — .message is non-enumerable, must be lifted explicitly
  if (e instanceof Error) {
    const extra = e as Error & { error?: string };
    return {
      error: extra.error ?? fallbackCode,
      message: e.message,
      details: { stack: e.stack },
    };
  }
  return { error: fallbackCode, message: String(e) };
}

export async function loadVaultConfig(vaultRoot: string): Promise<VaultConfig> {
  const configPath = path.join(vaultRoot, "schist.yaml");
  const content = await fs.readFile(configPath, "utf-8");

  // Use js-yaml instead of hand-rolled regexes: handles inline comments,
  // quoted strings with ":", multiline values, and all valid YAML.
  const raw = yamlLoad(content) as Record<string, unknown>;

  const getString = (key: string, def: string): string => {
    const v = raw[key];
    return typeof v === "string" ? v.trim() : def;
  };

  const getStringList = (key: string, def: string[]): string[] => {
    const v = raw[key];
    if (Array.isArray(v)) return v.map(String).filter(Boolean);
    return def;
  };

  return {
    name: getString("name", path.basename(vaultRoot)),
    path: vaultRoot,
    directories: getStringList("directories", ["notes", "papers", "concepts"]),
    connectionTypes: getStringList("connection_types", [
      "extends", "contradicts", "supports", "replicates",
      "applies-method-of", "reinterprets", "related",
    ]),
    statuses: getStringList("statuses", ["draft", "review", "final", "archived"]),
    writeBranch: getString("write_branch", "drafts"),
  };
}

function triggerIngestion(vaultRoot: string): void {
  // Resolves schist repo root from dist/ runtime path — do not replace with __dirname (ESM)
  const schist_repo = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../"
  );
  const dbPath = path.join(vaultRoot, ".schist", "schist.db");
  const scriptPath = path.join(schist_repo, "ingestion", "ingest.py");

  const child = spawn("python3", [scriptPath, "--vault", vaultRoot, "--db", dbPath], {
    cwd: vaultRoot,
    stdio: "ignore",
  });
  child.unref();
  child.on("error", (err) => {
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
    return normalizeError(e, "INGEST_ERROR");
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
    return normalizeError(e, "INGEST_ERROR");
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

    if (rawSlug(args.title) === "") {
      return {
        error: "VALIDATION_ERROR",
        message: "Title must contain at least one alphanumeric character",
      } satisfies ToolError;
    }

    const slug = slugify(args.title);
    const date = today();

    // Guard against same-day same-title collision: append HH-MM-SS suffix when
    // the target path already exists so we never silently overwrite a note or
    // produce a git "nothing to commit" error.
    const baseFilename = `${date}-${slug}.md`;
    const basePath = `${directory}/${baseFilename}`;
    let relPath = basePath;
    try {
      await fs.access(path.join(vaultRoot, basePath));
      // File exists — append time suffix to make the path unique
      const timeSuffix = new Date()
        .toISOString()
        .split("T")[1]
        .slice(0, 8)       // HH:MM:SS
        .replace(/:/g, "-"); // colons not safe in filenames on all OSes
      relPath = `${directory}/${date}-${slug}-${timeSuffix}.md`;
    } catch {
      // File does not exist — use base path as-is
    }

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
    return normalizeError(e, "GIT_ERROR");
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
    return normalizeError(e, "GIT_ERROR");
  }
}

export async function list_concepts(
  vaultRoot: string,
  args: { tags?: string[]; search?: string; limit?: number }
): Promise<unknown> {
  try {
    return sqliteReader.listConcepts(vaultRoot, args);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}

export async function query_graph(
  vaultRoot: string,
  args: { sql: string; params?: unknown[] }
): Promise<unknown> {
  try {
    return sqliteReader.queryGraph(vaultRoot, args.sql, args.params);
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }
}

export async function get_context(
  vaultRoot: string,
  // Default to "minimal" for agent session-start: only note/concept/edge counts
  // + last 3 modified. Agents that need richer context request standard/full.
  args: { depth?: "minimal" | "standard" | "full" }
): Promise<unknown> {
  try {
    return sqliteReader.getContext(vaultRoot, args.depth ?? "minimal");
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}

// ── Memory V2 Tools ────────────────────────────────────────────────────────

/** Helper: validate SCHIST_AGENT_ID matches owner (skip if env var not set) */
function assertAgentIdentity(owner: string): void {
  const agentId = process.env.SCHIST_AGENT_ID;
  if (agentId && agentId !== owner) {
    throw { error: "VALIDATION_ERROR", message: `Owner '${owner}' does not match SCHIST_AGENT_ID '${agentId}'` };
  }
}

// READ-ONLY memory tools (no capability gate)

export async function search_memory(
  _vaultRoot: string,
  args: { query?: string; owner?: string; entry_type?: string; date_from?: string; date_to?: string; limit?: number }
): Promise<unknown> {
  try {
    return sqliteReader.searchMemory(args);
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }
}

export async function get_agent_state(
  _vaultRoot: string,
  args: { key: string }
): Promise<unknown> {
  try {
    return sqliteReader.getAgentState(args.key);
  } catch (e: unknown) {
    return normalizeError(e, "INVALID_SQL");
  }
}

export async function list_domains(
  vaultRoot: string,
  _args: Record<string, never>
): Promise<unknown> {
  try {
    return sqliteReader.listDomains(vaultRoot);
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}

// WRITE memory tools (require write capability gate)

export async function add_memory(
  _vaultRoot: string,
  args: {
    owner: string;
    entry_type: string;
    content: string;
    date?: string;
    tags?: string[];
    related_doc?: string;
    source_ref?: string;
    confidence?: string;
  }
): Promise<unknown> {
  try {
    assertAgentIdentity(args.owner);
    return sqliteReader.addMemory(args);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function set_agent_state(
  _vaultRoot: string,
  args: { key: string; value: unknown; owner: string; ttl_hours?: number }
): Promise<unknown> {
  try {
    assertAgentIdentity(args.owner);
    return sqliteReader.setAgentState(args.key, args.value, args.owner, args.ttl_hours);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function delete_agent_state(
  _vaultRoot: string,
  args: { key: string; owner: string }
): Promise<unknown> {
  try {
    assertAgentIdentity(args.owner);
    return sqliteReader.deleteAgentState(args.key, args.owner);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function add_concept_alias(
  vaultRoot: string,
  args: { duplicate_slug: string; canonical_slug: string; reason?: string; created_by: string }
): Promise<unknown> {
  try {
    assertAgentIdentity(args.created_by);
    return sqliteReader.addConceptAlias(vaultRoot, args.duplicate_slug, args.canonical_slug, args.reason, args.created_by);
  } catch (e: unknown) {
    return normalizeError(e, "VALIDATION_ERROR");
  }
}

export async function assign_domain(
  vaultRoot: string,
  args: { doc_id: string; domain_slug: string }
): Promise<unknown> {
  try {
    // Add domain_slug as a tag on the doc (stored in docs.tags JSON array)
    const note = sqliteReader.getNote(vaultRoot, args.doc_id);
    if (!note) return { error: "NOT_FOUND", message: `Doc '${args.doc_id}' not found` };
    // Return the domain assignment — actual persistence is via create_note/update
    return { id: args.doc_id, domain_slug: args.domain_slug };
  } catch (e: unknown) {
    return normalizeError(e, "INGEST_ERROR");
  }
}
