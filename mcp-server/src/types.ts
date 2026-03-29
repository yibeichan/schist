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
  status: string;
  tags: string[];
  concepts: string[];
  body: string;
  connections: Connection[];
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
  status: string;
  tags: string[];
  snippet: string;
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
  error: string;  // NOT_FOUND | PATH_TRAVERSAL | WRITE_TIMEOUT | GIT_ERROR | INVALID_SQL | VALIDATION_ERROR | INGEST_ERROR
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

export interface AgentStateEntry {
  key: string;
  value: unknown;
  owner: string;
  updated_at: string;
  ttl_hours?: number | null;
}

export interface Domain {
  slug: string;
  label: string;
  description?: string;
  parent_slug?: string;
}

export interface ConceptAlias {
  duplicate_slug: string;
  canonical_slug: string;
  reason?: string;
  created_by: string;
  created_at: string;
}
