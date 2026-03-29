#!/usr/bin/env node
/**
 * schist memory CLI — research-db commands
 *
 * Usage:
 *   schist-memory add-memory --agent <id> --type <type> "content"
 *   schist-memory search "query" [--agent <id>] [--type <type>] [--limit N]
 *   schist-memory state get <key>
 *   schist-memory state set <key> "value" --agent <id> [--ttl N]
 *
 * Entry types: decision, lesson, blocker, completion, observation
 *
 * Env vars:
 *   SCHIST_MEMORY_DB — path to agent-state.db (default: ~/.openclaw/memory/agent-state.db)
 *   SCHIST_AGENT_ID  — agent identity for write validation
 */

import * as sqliteReader from "../sqlite-reader.js";

function usage(): void {
  console.error(`
schist-memory — agent memory and state CLI

COMMANDS

  add-memory --agent <id> --type <type> [options] "<content>"
    Add a memory entry.
    --agent  required  agent id (e.g. sansan, eleven, ninjia)
    --type   required  entry_type: decision|lesson|blocker|completion|observation
    --date   optional  ISO date (default: today)
    --tags   optional  comma-separated tags
    --ref    optional  source reference (PR number, issue, etc.)
    --conf   optional  confidence: low|medium|high (default: medium)

  search "<query>" [options]
    Full-text search over agent memory.
    --agent   optional  filter by agent id
    --type    optional  filter by entry_type
    --from    optional  ISO date lower bound
    --to      optional  ISO date upper bound
    --limit   optional  max results (default: 20)

  state get <key>
    Get an agent_state value by key (e.g. sansan.current_pr)

  state set <key> "<value>" --agent <id> [--ttl N]
    Set an agent_state key. value is stored as JSON.
    --agent  required  owner agent id
    --ttl    optional  expire after N hours

ENVIRONMENT
  SCHIST_MEMORY_DB   path to SQLite database (default: ~/.openclaw/memory/agent-state.db)
  SCHIST_AGENT_ID    agent identity — enforced on writes
`);
  process.exit(1);
}

function parseArgs(argv: string[]): { command: string; subcommand?: string; args: Map<string, string>; positional: string[] } {
  const [command, ...rest] = argv;
  if (!command) return { command: "", args: new Map(), positional: [] };

  let subcommand: string | undefined;
  const positional: string[] = [];
  const args = new Map<string, string>();

  const tokens = [...rest];
  let i = 0;

  // For 'state' command, first token is subcommand
  if (command === "state" && tokens.length > 0 && !tokens[0].startsWith("--")) {
    subcommand = tokens.shift()!;
  }

  while (i < tokens.length) {
    const tok = tokens[i];
    if (tok.startsWith("--")) {
      const key = tok.slice(2);
      const val = tokens[i + 1] && !tokens[i + 1].startsWith("--") ? tokens[++i] : "true";
      args.set(key, val);
    } else {
      positional.push(tok);
    }
    i++;
  }

  return { command, subcommand, args, positional };
}

function formatEntry(e: Record<string, unknown>): void {
  const tags = e.tags ? `\n  tags:       ${e.tags}` : "";
  const ref = e.source_ref ? `\n  ref:        ${e.source_ref}` : "";
  const conf = e.confidence ? `\n  confidence: ${e.confidence}` : "";
  console.log(
    `[${e.id}] ${e.date} | ${e.owner} | ${e.entry_type}${conf}` +
    `\n  ${e.content}` +
    tags + ref +
    `\n  created: ${e.created_at}\n`
  );
}

async function cmdAddMemory(args: Map<string, string>, positional: string[]): Promise<void> {
  const agent = args.get("agent");
  const type = args.get("type");
  const content = positional[0] || args.get("content");

  if (!agent) { console.error("Error: --agent is required"); process.exit(1); }
  if (!type) { console.error("Error: --type is required"); process.exit(1); }
  if (!content) { console.error("Error: content argument is required"); process.exit(1); }

  const validTypes = ["decision", "lesson", "blocker", "completion", "observation"];
  if (!validTypes.includes(type)) {
    console.error(`Error: --type must be one of: ${validTypes.join(", ")}`);
    process.exit(1);
  }

  const entry = {
    owner: agent,
    entry_type: type,
    content,
    date: args.get("date"),
    tags: args.get("tags") ? args.get("tags")!.split(",").map(t => t.trim()) : undefined,
    source_ref: args.get("ref"),
    confidence: args.get("conf") as "low" | "medium" | "high" | undefined,
  };

  try {
    const result = sqliteReader.addMemory(entry);
    console.log(`✅ Memory entry added: id=${result.id} created_at=${result.created_at}`);
  } catch (e: unknown) {
    const err = e as { error?: string; message?: string } | Error;
    console.error(`Error: ${"message" in err ? err.message : String(e)}`);
    process.exit(1);
  }
}

async function cmdSearch(args: Map<string, string>, positional: string[]): Promise<void> {
  const query = positional[0] || args.get("query");

  const opts: Parameters<typeof sqliteReader.searchMemory>[0] = {
    query,
    owner: args.get("agent"),
    entry_type: args.get("type"),
    date_from: args.get("from"),
    date_to: args.get("to"),
    limit: args.has("limit") ? parseInt(args.get("limit")!, 10) : 20,
  };

  try {
    const results = sqliteReader.searchMemory(opts) as unknown as Record<string, unknown>[];
    if (!results.length) {
      console.log("No results.");
      return;
    }
    console.log(`${results.length} result(s):\n`);
    results.forEach(formatEntry);
  } catch (e: unknown) {
    const err = e as { error?: string; message?: string } | Error;
    console.error(`Error: ${"message" in err ? err.message : String(e)}`);
    process.exit(1);
  }
}

async function cmdStateGet(args: Map<string, string>, positional: string[]): Promise<void> {
  const key = positional[0] || args.get("key");
  if (!key) { console.error("Error: key is required"); process.exit(1); }

  try {
    const result = sqliteReader.getAgentState(key) as Record<string, unknown> | null;
    if (!result) {
      console.log(`(not set)`);
    } else {
      console.log(`key:        ${result.key}`);
      console.log(`owner:      ${result.owner}`);
      console.log(`updated_at: ${result.updated_at}`);
      console.log(`value:      ${result.value}`);
    }
  } catch (e: unknown) {
    const err = e as { error?: string; message?: string } | Error;
    console.error(`Error: ${"message" in err ? err.message : String(e)}`);
    process.exit(1);
  }
}

async function cmdStateSet(args: Map<string, string>, positional: string[]): Promise<void> {
  const key = positional[0];
  const value = positional[1] || args.get("value");
  const agent = args.get("agent");

  if (!key) { console.error("Error: key is required"); process.exit(1); }
  if (!value) { console.error("Error: value is required"); process.exit(1); }
  if (!agent) { console.error("Error: --agent is required"); process.exit(1); }

  const ttlRaw = args.get("ttl");
  const ttl = ttlRaw ? parseInt(ttlRaw, 10) : undefined;

  // Parse value: try JSON, fall back to string
  let parsedValue: unknown;
  try {
    parsedValue = JSON.parse(value);
  } catch {
    parsedValue = value;
  }

  try {
    sqliteReader.setAgentState(key, parsedValue, agent, ttl);
    console.log(`✅ State set: ${key} = ${value}`);
  } catch (e: unknown) {
    const err = e as { error?: string; message?: string } | Error;
    console.error(`Error: ${"message" in err ? err.message : String(e)}`);
    process.exit(1);
  }
}

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  if (!argv.length) { usage(); return; }

  const { command, subcommand, args, positional } = parseArgs(argv);

  switch (command) {
    case "add-memory":
      await cmdAddMemory(args, positional);
      break;
    case "search":
      await cmdSearch(args, positional);
      break;
    case "state":
      if (subcommand === "get") {
        await cmdStateGet(args, positional);
      } else if (subcommand === "set") {
        await cmdStateSet(args, positional);
      } else {
        console.error(`Error: unknown state subcommand '${subcommand ?? ""}'. Use: get, set`);
        process.exit(1);
      }
      break;
    default:
      console.error(`Error: unknown command '${command}'`);
      usage();
  }
}

main().catch(e => {
  console.error("Fatal:", e);
  process.exit(1);
});
