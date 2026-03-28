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

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
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
          },
          required: ["query"],
        },
      },
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
            directory: { type: "string" },
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
            type: {
              type: "string",
              enum: config.connectionTypes,
            },
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
      {
        name: "get_context",
        description: "Get knowledge graph context summary for agent session initialization",
        inputSchema: {
          type: "object" as const,
          properties: {
            depth: { type: "string", enum: ["minimal", "standard", "full"] },
          },
        },
      },
    ],
  }));

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolArgs = (args ?? {}) as Record<string, unknown>;

    let result: unknown;
    try {
      switch (name) {
        case "search_notes":
          result = await search_notes(vaultRoot, toolArgs as Parameters<typeof search_notes>[1]);
          break;
        case "get_note":
          result = await get_note(vaultRoot, toolArgs as Parameters<typeof get_note>[1]);
          break;
        case "create_note":
          result = await create_note(vaultRoot, toolArgs as Parameters<typeof create_note>[1], config);
          break;
        case "add_connection":
          result = await add_connection(vaultRoot, toolArgs as Parameters<typeof add_connection>[1]);
          break;
        case "list_concepts":
          result = await list_concepts(vaultRoot, toolArgs as Parameters<typeof list_concepts>[1]);
          break;
        case "query_graph":
          result = await query_graph(vaultRoot, toolArgs as Parameters<typeof query_graph>[1]);
          break;
        case "get_context":
          result = await get_context(vaultRoot, toolArgs as Parameters<typeof get_context>[1]);
          break;
        default:
          result = { error: "VALIDATION_ERROR", message: `Unknown tool: ${name}` };
      }
    } catch (e: unknown) {
      result = { error: "INGEST_ERROR", message: String(e), details: e };
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
  console.error("[schist] MCP server ready (stdio)");
}

main().catch((e) => {
  console.error("Fatal error:", e);
  process.exit(1);
});
