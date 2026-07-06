// Shared TypeScript types for schist MCP server

export interface Connection {
  target: string;
  type: string;
  context?: string;
}

export interface Note {
  id: string;
  title: string;
  date: string;
  status: string | null;
  tags: string[];
  concepts: string[];
  body: string;
  connections: Connection[];
  scope?: string;      // e.g., "global", "decisions", "research/ai"
  source?: "human" | "agent"; // allowed values; omitted means undefined
  confidence?: "low" | "medium" | "high"; // omitted means agent did not declare
  file_ref?: string; // external file pointer; schist does not manage the file
}

export interface Concept {
  slug: string;
  title: string;
  description: string;
  tags: string[];
  edgeCount: number;
}

export interface SearchResult {
  id: string;
  title: string;
  date: string;
  status: string | null;
  tags: string[];
  snippet: string;
  scope?: string;
  confidence?: "low" | "medium" | "high"; // omitted means not declared on the note
}

export interface SearchNotesResponse {
  results: SearchResult[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
}

export interface ListConceptsResponse {
  concepts: Concept[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
}

export interface QueryGraphResponse {
  columns: string[];
  rows: unknown[][];
  rowCount: number;
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
}

/**
 * Response shape for the `get_context` tool. Fields are optional because the
 * SQLite reader returns different shapes at each depth tier. `verboseNote`
 * is set when the call was downgraded (depth="full" without a valid verbose
 * reason) or when the rate-limit tracker fires.
 */
export interface GetContextResponse {
  // depth === "minimal":
  noteCount?: number;
  conceptCount?: number;
  edgeCount?: number;
  // depth >= "standard":
  vault?: { path: string; noteCount: number; conceptCount: number; edgeCount: number };
  recent?: Array<Record<string, unknown>>;
  hotConcepts?: Array<Record<string, unknown>>;
  /** depth >= "standard", only when a memory owner resolved AND the memory
   *  DB is reachable — otherwise absent, never an error (D4). */
  recentMemory?: RecentMemoryBlock;
  // depth === "full":
  tagCloud?: Array<{ tag: string; count: number }>;
  // Operational hints (set independently of depth):
  syncWarning?: string;
  verboseNote?: string;
}

export interface SyncStatusResponse {
  is_spoke: boolean;
  spoke_head: string;
  hub_head: string | null;
  ahead: number | null;
  behind: number | null;
  last_sync_error: {
    timestamp?: string;
    contents: string;
  } | null;
  clean_working_tree: boolean;
  hub_error?: string;
}

export interface SyncRetryResponse {
  ok: boolean;
  mode: "push-only" | "pull-rebase-push";
  phase: "await-in-flight" | "pull-rebase" | "push";
  retriable: boolean;
  reason?: string;
  message: string;
  code?: number;
  signal?: NodeJS.Signals;
  timed_out?: boolean;
  cleared_last_sync_error?: boolean;
  awaited_in_flight?: boolean;
}

export interface ComposeBriefResponse {
  markdown: string;
  suggested_tags: string[];
  cross_refs: string[];
  related_notes: Array<{
    id: string;
    title: string;
    reason: string;
  }>;
  recent_paths: Array<{
    path: string;
    commit: string;
  }>;
  /** Present (true) when the recent-paths git lookup failed or timed out, so
   *  an empty recent_paths can't be mistaken for "nothing added". #238. */
  recent_paths_unavailable?: boolean;
}

export interface VaultConfig {
  name: string;
  path: string;
  directories: string[];
  connectionTypes: string[];
  statuses: string[];
  writeBranch: string;
}

export interface ToolError {
  error: string;  // NOT_FOUND | PATH_TRAVERSAL | WRITE_TIMEOUT | GIT_ERROR | INVALID_SQL | VALIDATION_ERROR | INGEST_ERROR | INVALID_ARG | CURSOR_REQUIRED | CURSOR_EXPIRED | CURSOR_INVALID_SIGNATURE | CURSOR_WRONG_TOOL | CURSOR_STALE | CURSOR_QUERY_MISMATCH | ACL_DENIED | SYNC_DIRTY | TOOL_REMOVED
  message: string;
  details?: unknown;
}

export interface GitWriteResult {
  path: string;
  commitSha: string;
}

// ── Memory V2 types ────────────────────────────────────────────────────────

export interface MemoryEntry {
  id: number;
  owner: string;
  date: string;
  entry_type: 'decision' | 'lesson' | 'blocker' | 'completion' | 'observation';
  content: string;
  tags: string[];
  related_doc?: string;
  source_ref?: string;
  confidence: 'low' | 'medium' | 'high';
  created_at: string;
}

/**
 * One row of get_context's recentMemory block (slice C, docs/data-model.md
 * D4). A deliberate subset of MemoryEntry: enough to orient a session and
 * hop memory → note via related_doc; use search_memory for full rows. This
 * shape is load-bearing for session-start flows (/pickup) — extend it by
 * ADDING optional fields, never by renaming or removing these.
 */
export interface RecentMemoryEntry {
  id: number;
  date: string;
  entry_type: MemoryEntry["entry_type"];
  /** Content snippet — get_context truncates to 100 code points. */
  content: string;
  /** Vault note id back-reference (`notes/….md`); absent when the entry has none. */
  related_doc?: string;
}

/**
 * get_context's ephemeral-memory block. Namespaced under its own key so
 * agent memory (fast, session-scoped, not git-backed) reads as clearly
 * distinct from the vault-derived fields beside it.
 */
export interface RecentMemoryBlock {
  /** Resolved memory owner the entries belong to. */
  owner: string;
  /** Most recent entries, newest first; [] when memory is reachable but empty. */
  entries: RecentMemoryEntry[];
}

export interface SearchMemoryResponse {
  entries: MemoryEntry[];
  /** Opaque cursor token for the next page; absent when this is the last page. */
  cursor?: string;
  /** Soft warning when the verbose reason pattern has exceeded the rate limit. */
  verboseNote?: string;
}

export interface AgentStateEntry {
  key: string;
  value: unknown;
  owner: string;
  updated_at: string;
  ttl_hours?: number | null;
}

export interface ConceptAlias {
  duplicate_slug: string;
  canonical_slug: string;
  reason?: string;
  created_by: string;
  created_at: string;
}
