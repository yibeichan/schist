import type { Tool } from "@modelcontextprotocol/sdk/types.js";

type OutputSchema = NonNullable<Tool["outputSchema"]>;

const string = { type: "string" as const };
const number = { type: "number" as const };
const boolean = { type: "boolean" as const };
const strings = { type: "array" as const, items: string };
const objects = { type: "array" as const, items: { type: "object" as const } };
const connection = object({ target: string, type: string, context: string }, ["target", "type"]);
const searchResult = object({
  id: string, title: string, date: string, status: { type: ["string", "null"] },
  tags: strings, snippet: string, scope: string, confidence: string,
}, ["id", "title", "date", "status", "tags", "snippet"]);
const concept = object({
  slug: string, title: string, description: string, tags: strings,
  edgeCount: number, aliasOf: string, aliases: strings,
}, ["slug", "title", "description", "tags", "edgeCount"]);
const memoryEntry = object({
  id: number, owner: string, date: string, entry_type: string,
  content: string, tags: strings, related_doc: string, source_ref: string,
  confidence: string, created_at: string,
}, ["id", "owner", "date", "entry_type", "content", "tags", "confidence", "created_at"]);

function object(
  properties: Record<string, object>,
  required: string[] = [],
): OutputSchema {
  return { type: "object", properties, required, additionalProperties: true };
}

// These describe the stable, model-useful fields. Optional fields and nested
// records remain extensible as the vault and memory tools evolve.
export const OUTPUT_SCHEMAS: Record<string, OutputSchema> = {
  get_context: object({
    noteCount: number, conceptCount: number, edgeCount: number,
    vault: object({ path: string, noteCount: number, conceptCount: number, edgeCount: number },
      ["path", "noteCount", "conceptCount", "edgeCount"]),
    recent: objects, hotConcepts: objects,
    recentMemory: object({
      owner: string,
      entries: { type: "array", items: object({
        id: number, date: string, entry_type: string, content: string, related_doc: string,
      }, ["id", "date", "entry_type", "content"]) },
    }, ["owner", "entries"]),
    tagCloud: { type: "array", items: object({ tag: string, count: number }, ["tag", "count"]) },
    syncWarning: string, verboseNote: string,
  }),
  sync_status: object({
    is_spoke: boolean, spoke_head: string,
    hub_head: { type: ["string", "null"] },
    ahead: { type: ["number", "null"] }, behind: { type: ["number", "null"] },
    last_sync_error: {
      anyOf: [object({
        timestamp: string, contents: string,
        failure_class: { type: ["string", "null"] },
        retriable: { type: ["boolean", "null"] },
      }, ["contents"]), { type: "null" }],
    },
    clean_working_tree: boolean, blocked_by_ignored: boolean,
    blocking_ignored_paths: strings, hub_error: string,
  }, ["is_spoke", "spoke_head", "hub_head", "ahead", "behind", "last_sync_error", "clean_working_tree", "blocked_by_ignored", "blocking_ignored_paths"]),
  search_notes: object({ results: { type: "array", items: searchResult }, cursor: string }, ["results"]),
  search_memory: object({
    entries: { type: "array", items: memoryEntry }, cursor: string,
    verboseNote: string, zeroHitDiagnostic: string,
  }, ["entries"]),
  // get_agent_state can return null. Its structured result uses {state: null}
  // while the legacy text content continues to be the literal "null".
  get_agent_state: object({
    state: { anyOf: [object({
      key: string, value: {}, owner: string, updated_at: string,
      ttl_hours: { type: ["number", "null"] },
    }, ["key", "value", "owner", "updated_at"]), { type: "null" }] },
  }, ["state"]),
  add_memory: object({ id: number, created_at: string, db: string }, ["id", "created_at", "db"]),
  set_agent_state: object({ key: string, updated_at: string }, ["key", "updated_at"]),
  delete_agent_state: object({ deleted: boolean }, ["deleted"]),
  add_concept_alias: object({
    duplicate_slug: string, canonical_slug: string, reason: string,
    created_by: string, created_at: string, replaced_canonical: string,
    repointed: strings,
  }, ["duplicate_slug", "canonical_slug", "created_by", "created_at"]),
  get_note: object({
    id: string, title: string, date: string,
    status: { type: ["string", "null"] }, tags: strings, concepts: strings,
    body: string, connections: { type: "array", items: connection },
    confidence: string, file_ref: string,
    alias_of: string, aliases: strings,
  }, ["id", "title", "date", "status", "tags", "concepts", "body", "connections"]),
  create_note: object({
    id: string, path: string, commitSha: string,
    commitWarning: string, syncWarning: string,
  }, ["id", "path", "commitSha"]),
  create_concept: object({
    id: string, path: string, commitSha: string,
    commitWarning: string, syncWarning: string,
  }, ["id", "path", "commitSha"]),
  update_note: object({
    id: string, updated: boolean, commitSha: string,
    commitWarning: string, syncWarning: string,
  }, ["id", "updated", "commitSha"]),
  delete_note: object({
    id: string, deleted: boolean, commitSha: string, repaired: strings,
    commitWarning: string, indexWarning: string, syncWarning: string,
  }, ["id", "deleted", "commitSha", "repaired"]),
  add_connection: object({
    source: string, target: string, type: string, commitSha: string,
    commitWarning: string, syncWarning: string,
  }, ["source", "target", "type", "commitSha"]),
  sync_retry: object({
    ok: boolean, mode: string, phase: string, retriable: boolean,
    failure_class: string, reason: string, message: string, code: number,
    signal: string, timed_out: boolean, cleared_last_sync_error: boolean,
    awaited_in_flight: boolean,
  }, ["ok", "mode", "phase", "retriable", "message"]),
  list_concepts: object({ concepts: { type: "array", items: concept }, cursor: string }, ["concepts"]),
  query_graph: object({
    columns: strings, rows: { type: "array", items: { type: "array" } },
    rowCount: number, cursor: string,
  }, ["columns", "rows", "rowCount"]),
  compose_brief: object({
    markdown: string, suggested_tags: strings, cross_refs: strings,
    related_notes: { type: "array", items: object({
      id: string, title: string, reason: string,
    }, ["id", "title", "reason"]) },
    recent_paths: { type: "array", items: object({
      path: string, commit: string,
    }, ["path", "commit"]) },
    recent_paths_unavailable: boolean,
  }, ["markdown", "suggested_tags", "cross_refs", "related_notes", "recent_paths"]),
};

function isToolError(value: unknown): value is { error: string; message: string } {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    && typeof (value as Record<string, unknown>).error === "string"
    && typeof (value as Record<string, unknown>).message === "string";
}

/** Preserve the old text payload while exposing typed data to MCP clients. */
export function formatToolResult(name: string, result: unknown) {
  const content = [{ type: "text" as const, text: JSON.stringify(result, null, 2) }];
  if (isToolError(result)) return { isError: true, content };
  if (name === "get_agent_state") {
    return { content, structuredContent: { state: result } };
  }
  if (result !== null && typeof result === "object" && !Array.isArray(result)) {
    return { content, structuredContent: result as Record<string, unknown> };
  }
  return { content };
}
