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
  sync_status,
  sync_retry,
  compose_brief,
  loadVaultConfig,
  // Memory V2
  add_memory,
  search_memory,
  get_agent_state,
  set_agent_state,
  delete_agent_state,
  add_concept_alias,
} from "./tools.js";
import type { VaultConfig } from "./types.js";
import { listAllTools, REMOVED_TOOLS } from "./tool-registry.js";

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

  // #110: warn once at startup when neither SCHIST_AGENT_NAME nor
  // SCHIST_AGENT_ID is set. The cursor-protocol refusal LRU keys on
  // `(tool, queryHash, owner, vaultRoot)`; with no agent identity the
  // owner segment collapses to "" and every anonymous caller in this
  // process shares one refusal bucket — fine for single-user dev, but
  // a footgun in multi-tenant (e.g. OpenClaw) shared-MCP deployments.
  if (!process.env.SCHIST_AGENT_NAME && !process.env.SCHIST_AGENT_ID) {
    console.error(
      "[schist] WARN: neither SCHIST_AGENT_NAME nor SCHIST_AGENT_ID is set. " +
      "Cursor-refusal isolation will collapse to a shared anonymous bucket. " +
      "Set one (NAME for human-readable; ID for stable identity in multi-agent setups)."
    );
  }

  const server = new Server(
    { name: "schist", version: "0.1.0" },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => {
    return { tools: listAllTools(config) };
  });

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
        case "compose_brief":
          result = await compose_brief(vaultRoot, toolArgs as Parameters<typeof compose_brief>[1]);
          break;
        case "get_context":
          result = await get_context(vaultRoot, toolArgs as Parameters<typeof get_context>[1]);
          break;
        case "sync_status":
          result = await sync_status(vaultRoot);
          break;
        case "sync_retry":
          result = await sync_retry(vaultRoot, toolArgs as Parameters<typeof sync_retry>[1]);
          break;
        case "search_memory":
          result = await search_memory(vaultRoot, toolArgs as Parameters<typeof search_memory>[1]);
          break;
        case "get_agent_state":
          result = await get_agent_state(vaultRoot, toolArgs as Parameters<typeof get_agent_state>[1]);
          break;
        case "add_memory":
          result = await add_memory(vaultRoot, toolArgs as Parameters<typeof add_memory>[1]);
          break;
        case "set_agent_state":
          result = await set_agent_state(vaultRoot, toolArgs as Parameters<typeof set_agent_state>[1]);
          break;
        case "delete_agent_state":
          result = await delete_agent_state(vaultRoot, toolArgs as Parameters<typeof delete_agent_state>[1]);
          break;
        case "add_concept_alias":
          result = await add_concept_alias(vaultRoot, toolArgs as Parameters<typeof add_concept_alias>[1]);
          break;
        default:
          // Object.hasOwn, not `name in REMOVED_TOOLS`: `name` is
          // client-controlled, and `in` walks the prototype chain — a call
          // named "constructor"/"toString"/"__proto__" would otherwise match
          // an inherited Object.prototype member and return a malformed
          // TOOL_REMOVED (non-string value, dropped by JSON.stringify).
          if (Object.hasOwn(REMOVED_TOOLS, name)) {
            result = { error: "TOOL_REMOVED", message: REMOVED_TOOLS[name] };
          } else {
            result = { error: "VALIDATION_ERROR", message: `Unknown tool: ${name}` };
          }
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
  console.error("[schist] MCP server ready (stdio).");
  console.error("[schist] All writes (memory + vault) are authorized by validateOwner against SCHIST_AGENT_ID / SCHIST_ALLOWED_AGENTS.");
  // Loud startup signal when no identity env is set: every write tool will
  // return CONFIG_ERROR on the first call, but operators upgrading from a
  // version that didn't gate vault writes would otherwise only discover the
  // breakage from a downstream tool failure. See PR #131 / #63.
  if (process.env.SCHIST_AGENT_ID === undefined && process.env.SCHIST_ALLOWED_AGENTS === undefined) {
    console.error("[schist] WARNING: neither SCHIST_AGENT_ID nor SCHIST_ALLOWED_AGENTS is set — all write tools will return CONFIG_ERROR. Set one of these env vars in your MCP server config.");
  }
}

main().catch((e) => {
  console.error("Fatal error:", e);
  process.exit(1);
});
