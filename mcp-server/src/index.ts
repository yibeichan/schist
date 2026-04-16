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
} from "./tools.js";
import type { VaultConfig } from "./types.js";
import {
  listAllTools,
  makeWriteTools,
  makeMemoryWriteTools,
} from "./tool-registry.js";

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
    return { tools: listAllTools(config) };
  });

  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const { name, arguments: args } = request.params;
    const toolArgs = (args ?? {}) as Record<string, unknown>;

    let result: unknown;

    // Meta-tool: enable write capabilities for this session
    if (name === "request_capabilities") {
      if (toolArgs.capability === "write") {
        writeEnabled = true;
        const unlocked = [
          ...makeWriteTools(config).map((t) => t.name),
          ...makeMemoryWriteTools(config).map((t) => t.name),
        ];
        result = {
          ok: true,
          message: `Write capability enabled. Invocation now permitted for: ${unlocked.join(", ")}.`,
          tools: unlocked,
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
