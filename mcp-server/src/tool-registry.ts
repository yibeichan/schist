import type { VaultConfig } from "./types.js";

export function makeReadTools(config: VaultConfig) {
  return [
    {
      name: "get_context",
      description:
        "Get knowledge graph context summary. Defaults to 'minimal' (counts + last 3 notes). " +
        "Pass depth='standard' or depth='full' for richer detail. Call this first in any session.",
      inputSchema: {
        type: "object" as const,
        properties: {
          depth: { type: "string", enum: ["minimal", "standard", "full"] },
        },
      },
    },
    {
      name: "search_notes",
      description: "Full-text search across all notes in the knowledge graph",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string" },
          limit: { type: "number" },
          status: { type: "string", enum: config.statuses },
          tags: { type: "array", items: { type: "string" } },
          scope: { type: "string", description: 'Filter by scope. Use "inherit" to search agent default scope + global.' },
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
      description: "Search agent memory entries by text, owner, type, or date range.",
      inputSchema: {
        type: "object" as const,
        properties: {
          query: { type: "string" },
          owner: { type: "string" },
          entry_type: { type: "string", enum: ["decision", "lesson", "blocker", "completion", "observation"] },
          date_from: { type: "string" },
          date_to: { type: "string" },
          limit: { type: "number" },
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
      description: "List research domain taxonomy.",
      inputSchema: { type: "object" as const, properties: {} },
    },
  ];
}

export function makeMemoryWriteTools(_config: VaultConfig) {
  return [
    {
      name: "add_memory",
      description: "Add a memory entry (decision, lesson, blocker, completion, or observation). owner must match SCHIST_AGENT_ID.",
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
        },
        required: ["title", "body"],
      },
    },
    {
      name: "add_connection",
      description: "Add a typed connection between two notes or concepts",
      inputSchema: {
        type: "object" as const,
        properties: {
          source: { type: "string" },
          target: { type: "string" },
          type: { type: "string", enum: config.connectionTypes },
          context: { type: "string" },
        },
        required: ["source", "target", "type"],
      },
    },
    {
      name: "list_concepts",
      description: "List all concepts in the knowledge graph",
      inputSchema: {
        type: "object" as const,
        properties: {
          tags: { type: "array", items: { type: "string" } },
          search: { type: "string" },
          limit: { type: "number" },
        },
      },
    },
    {
      name: "query_graph",
      description: "Execute a read-only SQL query against the knowledge graph database",
      inputSchema: {
        type: "object" as const,
        properties: {
          sql: { type: "string" },
          params: { type: "array", items: {} },
        },
        required: ["sql"],
      },
    },
  ];
}

export function makeCapabilityTool() {
  return {
    name: "request_capabilities",
    description:
      "Enable write-capable tools for this session. Pass capability='write' to " +
      "allow invocation of: get_note, create_note, add_connection, list_concepts, " +
      "query_graph, add_memory, set_agent_state, delete_agent_state, " +
      "add_concept_alias. These tools are always listed by ListTools so MCP " +
      "clients that cache discovery can see them; the gate here controls " +
      "whether calls to them succeed. Read-only sessions (search + context) " +
      "do not need to call this.",
    inputSchema: {
      type: "object" as const,
      properties: {
        capability: { type: "string", enum: ["write"] },
      },
      required: ["capability"],
    },
  };
}

/**
 * Return the full set of tool definitions exposed by the schist MCP server.
 *
 * All tools are listed unconditionally. Write-capable tools still require
 * `request_capabilities({capability: "write"})` to succeed at invocation
 * time — the capability gate is per-call, not per-list. This matters because
 * MCP clients like Claude Code cache tool discovery at session start and
 * never re-fetch; conditional listing made write tools unreachable for
 * those clients.
 */
export function listAllTools(config: VaultConfig) {
  return [
    ...makeReadTools(config),
    ...makeMemoryReadTools(config),
    makeCapabilityTool(),
    ...makeWriteTools(config),
    ...makeMemoryWriteTools(config),
  ];
}
