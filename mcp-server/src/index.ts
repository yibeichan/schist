import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import * as path from "path";
import * as fs from "fs/promises";
import {
  search_notes,
  get_note,
  create_note,
  add_connection,
  list_concepts,
  query_graph,
  get_context,
  loadVaultConfig,
  // Memory V2
  add_memory,
  search_memory,
  get_agent_state,
  set_agent_state,
  delete_agent_state,
  add_concept_alias,
  list_domains,
  assign_domain,
} from "./tools.js";
import type { VaultConfig } from "./types.js";

function resolveVaultPath(): string {
  const envVault = process.env.SCHIST_VAULT_PATH;
  if (envVault) return path.resolve(envVault);

  const vaultArgIndex = process.argv.indexOf("--vault");
  if (vaultArgIndex !== -1 && process.argv[vaultArgIndex + 1]) {
    return path.resolve(process.argv[vaultArgIndex + 1]);
  }

  console.error("Error: SCHIST_VAULT_PATH env var or --vault argument required");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

function makeReadTools(config: VaultConfig) {
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

function makeMemoryReadTools(_config: VaultConfig) {
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

function makeMemoryWriteTools(_config: VaultConfig) {
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
    {
      name: "assign_domain",
      description: "Assign a research domain to a doc.",
      inputSchema: {
        type: "object" as const,
        properties: {
          doc_id: { type: "string" },
          domain_slug: { type: "string" },
        },
        required: ["doc_id", "domain_slug"],
      },
    },
  ];
}

function makeWriteTools(config: VaultConfig) {
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

function makeCapabilityTool() {
  return {
    name: "request_capabilities",
    description:
      "Unlock additional schist tools beyond the default read-only set. " +
      "Pass capability='write' to enable: get_note, create_note, add_connection, " +
      "list_concepts, query_graph. " +
      "Read-only sessions (search + context only) do not need to call this.",
    inputSchema: {
      type: "object" as const,
      properties: {
        capability: { type: "string", enum: ["write"] },
      },
      required: ["capability"],
    },
  };
}

// ---------------------------------------------------------------------------
// Server bootstrap
// ---------------------------------------------------------------------------

async function main() {
  const vaultRoot = resolveVaultPath();

  try {
    await fs.access(path.join(vaultRoot, "schist.yaml"));
  } catch {
    console.error(`Error: schist.yaml not found at ${path.join(vaultRoot, "schist.yaml")}`);
    process.exit(1);
  }

  let config: VaultConfig;
  try {
    config = await loadVaultConfig(vaultRoot);
  } catch (e) {
    console.error("Error loading vault config:", e);
    process.exit(1);
  }

  console.error(`[schist] Vault: ${vaultRoot}`);
  console.error(`[schist] Config: ${config.name}`);

  const server = new Server(
    { name: "schist", version: "0.1.0" },
    { capabilities: { tools: {} } }
  );

  // Lazy capability state: starts read-only, expanded on request_capabilities
  let writeEnabled = false;

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    const tools = [
      ...makeReadTools(config),
      ...makeMemoryReadTools(config),
      makeCapabilityTool(),
      ...(writeEnabled ? makeWriteTools(config) : []),
      ...(writeEnabled ? makeMemoryWriteTools(config) : []),
    ];
    return { tools };
  });

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolArgs = (args ?? {}) as Record<string, unknown>;

    let result: unknown;

    // Meta-tool: enable write capabilities for this session
    if (name === "request_capabilities") {
      if (toolArgs.capability === "write") {
        writeEnabled = true;
        result = {
          ok: true,
          message: "Write tools unlocked: get_note, create_note, add_connection, list_concepts, query_graph",
          tools: makeWriteTools(config).map((t) => t.name),
        };
      } else {
        result = { error: "VALIDATION_ERROR", message: `Unknown capability: ${String(toolArgs.capability)}` };
      }
    } else {
      try {
        switch (name) {
          case "search_notes":
            result = await search_notes(vaultRoot, toolArgs as Parameters<typeof search_notes>[1]);
            break;
          case "get_note":
            result = writeEnabled
              ? await get_note(vaultRoot, toolArgs as Parameters<typeof get_note>[1])
              : { error: "VALIDATION_ERROR", message: "get_note requires write capability. Call request_capabilities first." };
            break;
          case "create_note":
            result = writeEnabled
              ? await create_note(vaultRoot, toolArgs as Parameters<typeof create_note>[1], config)
              : { error: "VALIDATION_ERROR", message: "create_note requires write capability. Call request_capabilities first." };
            break;
          case "add_connection":
            result = writeEnabled
              ? await add_connection(vaultRoot, toolArgs as Parameters<typeof add_connection>[1])
              : { error: "VALIDATION_ERROR", message: "add_connection requires write capability. Call request_capabilities first." };
            break;
          case "list_concepts":
            result = writeEnabled
              ? await list_concepts(vaultRoot, toolArgs as Parameters<typeof list_concepts>[1])
              : { error: "VALIDATION_ERROR", message: "list_concepts requires write capability. Call request_capabilities first." };
            break;
          case "query_graph":
            result = writeEnabled
              ? await query_graph(vaultRoot, toolArgs as Parameters<typeof query_graph>[1])
              : { error: "VALIDATION_ERROR", message: "query_graph requires write capability. Call request_capabilities first." };
            break;
          case "get_context":
            result = await get_context(vaultRoot, toolArgs as Parameters<typeof get_context>[1]);
            break;
          // Memory V2 — read (always available)
          case "search_memory":
            result = await search_memory(vaultRoot, toolArgs as Parameters<typeof search_memory>[1]);
            break;
          case "get_agent_state":
            result = await get_agent_state(vaultRoot, toolArgs as Parameters<typeof get_agent_state>[1]);
            break;
          case "list_domains":
            result = await list_domains(vaultRoot, toolArgs as Parameters<typeof list_domains>[1]);
            break;
          // Memory V2 — write (require writeEnabled)
          case "add_memory":
            result = writeEnabled
              ? await add_memory(vaultRoot, toolArgs as Parameters<typeof add_memory>[1])
              : { error: "VALIDATION_ERROR", message: "add_memory requires write capability. Call request_capabilities first." };
            break;
          case "set_agent_state":
            result = writeEnabled
              ? await set_agent_state(vaultRoot, toolArgs as Parameters<typeof set_agent_state>[1])
              : { error: "VALIDATION_ERROR", message: "set_agent_state requires write capability." };
            break;
          case "delete_agent_state":
            result = writeEnabled
              ? await delete_agent_state(vaultRoot, toolArgs as Parameters<typeof delete_agent_state>[1])
              : { error: "VALIDATION_ERROR", message: "delete_agent_state requires write capability." };
            break;
          case "add_concept_alias":
            result = writeEnabled
              ? await add_concept_alias(vaultRoot, toolArgs as Parameters<typeof add_concept_alias>[1])
              : { error: "VALIDATION_ERROR", message: "add_concept_alias requires write capability." };
            break;
          case "assign_domain":
            result = writeEnabled
              ? await assign_domain(vaultRoot, toolArgs as Parameters<typeof assign_domain>[1])
              : { error: "VALIDATION_ERROR", message: "assign_domain requires write capability." };
            break;
          default:
            result = { error: "VALIDATION_ERROR", message: `Unknown tool: ${name}` };
        }
      } catch (e: unknown) {
        result = { error: "INGEST_ERROR", message: String(e), details: e };
      }
    }

    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  });

  const shutdown = async () => {
    console.error("[schist] Shutting down...");
    await server.close();
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("[schist] MCP server ready (stdio) — default tools: get_context, search_notes");
}

main().catch((e) => {
  console.error("Fatal error:", e);
  process.exit(1);
});
