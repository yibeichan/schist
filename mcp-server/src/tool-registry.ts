import type { VaultConfig } from "./types.js";

export function makeReadTools(config: VaultConfig) {
  return [
    {
      name: "get_context",
      description:
        "Get knowledge graph context summary. Defaults to 'minimal' (counts + last 3 notes). " +
        "Pass depth='standard' for recent docs + hot concepts. " +
        'depth=\'full\' additionally returns tagCloud and REQUIRES verbose: "<reason ≥12 chars>"; ' +
        "without a valid reason the server downgrades to 'standard' and the response carries a " +
        "verboseNote hint. Call this first in any session.",
      inputSchema: {
        type: "object" as const,
        properties: {
          depth: { type: "string", enum: ["minimal", "standard", "full"] },
          verbose: {
            type: "string",
            description: 'Reason string (≥12 code points after trim) gating depth="full". Logged to server stderr for audit. Omit or use a non-"full" depth if not needed.',
          },
        },
      },
    },
    {
      name: "search_notes",
      description: "Full-text search across all notes in the knowledge graph. Returns id+title+snippet rows; call `get_note` for the full body. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance, or refine the query. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 100, description: "Default 20, capped at 100." },
          status: { type: "string", enum: config.statuses },
          tags: { type: "array", items: { type: "string" } },
          scope: { type: "string", description: 'Filter by scope. Use "inherit" to search agent default scope + global.' },
          owner: { type: "string", description: "Calling agent's id. Required for scope='inherit' under SCHIST_ALLOWED_AGENTS-only deployments where the env has no per-process identity; otherwise optional (falls back to SCHIST_AGENT_NAME / SCHIST_AGENT_ID)." },
          confidence: { type: "string", enum: ["low", "medium", "high"], description: "Filter results by agent-declared confidence. Notes without a declared confidence (NULL) are excluded when this filter is set." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
        required: ["query"],
      },
    },
  ];
}

export function makeMemoryReadTools(_config: VaultConfig) {
  return [
    {
      name: "search_memory",
      description: "Search agent memory entries by text, owner, type, or date range. Returns content snippets (200 code points) by default; pass verbose: \"<reason ≥12 chars>\" to get full content. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance, or refine the query. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string" },
          owner: { type: "string" },
          entry_type: { type: "string", enum: ["decision", "lesson", "blocker", "completion", "observation"] },
          date_from: { type: "string" },
          date_to: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 200, description: "Default 50, capped at 200." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
          verbose: { type: "string", description: "Reason (≥12 Unicode code points after trim) gating full-content return. Logged to server stderr for audit." },
        },
      },
    },
    {
      name: "get_agent_state",
      description: "Get a keyed agent state value (e.g. 'sansan.current_pr').",
      inputSchema: {
        type: "object" as const,
        properties: { key: { type: "string" } },
        required: ["key"],
      },
    },
    {
      name: "list_domains",
      description: "List research domain taxonomy. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 500, description: "Default 100, capped at 500." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
      },
    },
  ];
}

export function makeMemoryWriteTools(_config: VaultConfig) {
  return [
    {
      name: "add_memory",
      description: "Add a memory entry (decision, lesson, blocker, completion, or observation). owner must match SCHIST_AGENT_ID, or appear in SCHIST_ALLOWED_AGENTS for multi-agent shared deployments.",
      inputSchema: {
        type: "object" as const,
        properties: {
          owner: { type: "string" },
          entry_type: { type: "string", enum: ["decision", "lesson", "blocker", "completion", "observation"] },
          content: { type: "string" },
          date: { type: "string" },
          tags: { type: "array", items: { type: "string" } },
          related_doc: { type: "string" },
          source_ref: { type: "string" },
          confidence: { type: "string", enum: ["low", "medium", "high"] },
        },
        required: ["owner", "entry_type", "content"],
      },
    },
    {
      name: "set_agent_state",
      description: "Set a keyed agent state value. Key prefix must match owner (e.g. sansan.*). team.* requires owner=eleven.",
      inputSchema: {
        type: "object" as const,
        properties: {
          key: { type: "string" },
          value: {},
          owner: { type: "string" },
          ttl_hours: { type: "number" },
        },
        required: ["key", "value", "owner"],
      },
    },
    {
      name: "delete_agent_state",
      description: "Delete a keyed agent state value (owner-enforced).",
      inputSchema: {
        type: "object" as const,
        properties: {
          key: { type: "string" },
          owner: { type: "string" },
        },
        required: ["key", "owner"],
      },
    },
    {
      name: "add_concept_alias",
      description: "Mark a concept slug as a duplicate of a canonical slug.",
      inputSchema: {
        type: "object" as const,
        properties: {
          duplicate_slug: { type: "string" },
          canonical_slug: { type: "string" },
          reason: { type: "string" },
          created_by: { type: "string" },
        },
        required: ["duplicate_slug", "canonical_slug", "created_by"],
      },
    },
  ];
}

export function makeWriteTools(config: VaultConfig) {
  return [
    {
      name: "get_note",
      description: "Get a note by ID with full content and connections",
      inputSchema: {
        type: "object" as const,
        properties: { id: { type: "string" } },
        required: ["id"],
      },
    },
    {
      name: "create_note",
      description: "Create a new note in the knowledge graph. Auto-commits to git.",
      inputSchema: {
        type: "object" as const,
        properties: {
          owner: { type: "string", description: "Agent identity. Required; validated against SCHIST_ALLOWED_AGENTS or SCHIST_AGENT_ID. Stamped on the note's source_agent frontmatter and the git commit message." },
          title: { type: "string" },
          body: { type: "string" },
          tags: { type: "array", items: { type: "string" } },
          concepts: { type: "array", items: { type: "string" } },
          status: { type: "string", enum: config.statuses },
          connections: {
            type: "array",
            items: {
              type: "object",
              properties: {
                target: { type: "string" },
                type: { type: "string" },
                context: { type: "string" },
              },
              required: ["target", "type"],
            },
          },
          directory: {
            type: "string",
            enum: config.directories,
          },
          confidence: {
            type: "string",
            enum: ["low", "medium", "high"],
            description: "Optional. Agent's stated confidence in the note's content. Omit if not declared; do not default to 'medium' to preserve 'agent did not declare' vs 'agent said medium' as distinct states.",
          },
        },
        required: ["owner", "title", "body"],
      },
    },
    {
      name: "add_connection",
      description: "Add a typed connection between two notes or concepts",
      inputSchema: {
        type: "object" as const,
        properties: {
          owner: { type: "string", description: "Agent identity. Required; validated against SCHIST_ALLOWED_AGENTS or SCHIST_AGENT_ID. Stamped on the git commit message." },
          source: { type: "string" },
          target: { type: "string" },
          type: { type: "string", enum: config.connectionTypes },
          context: { type: "string" },
        },
        required: ["owner", "source", "target", "type"],
      },
    },
    {
      name: "assign_domain",
      description: "Assign a research domain to a note. Domain must exist in vault.yaml domains list.",
      inputSchema: {
        type: "object" as const,
        properties: {
          owner: { type: "string", description: "Agent identity. Required; validated against SCHIST_ALLOWED_AGENTS or SCHIST_AGENT_ID. Stamped on the git commit message." },
          id: { type: "string", description: "Note ID (relative path)" },
          domain: { type: "string", description: "Domain slug from vault.yaml domains list" },
        },
        required: ["owner", "id", "domain"],
      },
    },
    {
      name: "list_concepts",
      description: "List all concepts in the knowledge graph. Paginated: when results are capped, the response includes a `cursor` token — echo it back on the next call to advance. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED.",
      inputSchema: {
        type: "object" as const,
        properties: {
          tags: { type: "array", items: { type: "string" } },
          search: { type: "string" },
          limit: { type: "integer", minimum: 1, maximum: 200, description: "Default 50, capped at 200." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
      },
    },
    {
      name: "query_graph",
      description: "Execute a read-only SQL query against the knowledge graph database. **Server-paginated** — your query is wrapped as `SELECT * FROM (<your_sql>) AS user_query LIMIT N OFFSET M` (default N=100, cap 1000). Your own LIMIT/ORDER BY/OFFSET inside the SQL are respected. When the page is capped, the response includes a `cursor` token — echo it back on the next call to advance. Identical queries within 300s without a cursor are refused with CURSOR_REQUIRED. Only SELECT and WITH statements are allowed; mutation keywords are rejected.",
      inputSchema: {
        type: "object" as const,
        properties: {
          sql: { type: "string" },
          params: { type: "array", items: {} },
          limit: { type: "integer", minimum: 1, maximum: 1000, description: "Outer LIMIT applied by the server wrap. Default 100, capped at 1000. This is in addition to any LIMIT in your SQL." },
          cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call. Echo verbatim; do not modify." },
        },
        required: ["sql"],
      },
    },
  ];
}

/**
 * Return the full set of tool definitions exposed by the schist MCP server.
 *
 * All tools are listed unconditionally and callable without any opt-in
 * meta-tool. Authorization is split between two tiers:
 *
 *   - Memory writes (add_memory, set_agent_state, delete_agent_state,
 *     add_concept_alias) call `validateOwner` (see `agent-identity.ts`)
 *     against the configured SCHIST_AGENT_ID / SCHIST_ALLOWED_AGENTS.
 *   - Vault writes (create_note, add_connection, assign_domain) call the
 *     same `validateOwner` (closed by #63): each accepts a required
 *     `owner` arg, stamped onto note frontmatter (`source_agent`) and the
 *     git commit message.
 */
export function listAllTools(config: VaultConfig) {
  return [
    ...makeReadTools(config),
    ...makeMemoryReadTools(config),
    ...makeWriteTools(config),
    ...makeMemoryWriteTools(config),
  ];
}
