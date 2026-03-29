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
