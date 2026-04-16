# MCP Server Setup

The schist MCP server (`mcp-server/`) exposes the knowledge graph to AI agents
via the Model Context Protocol over stdio.

## Configuration

The server requires the vault path, provided in one of two ways:

```
SCHIST_VAULT_PATH=/path/to/your/vault
```

This is intentionally **not** configured in this repository. Set it in your agent
environment (e.g. OpenClaw `mcp.servers.schist.env.SCHIST_VAULT_PATH`).

Alternatively, pass `--vault /path/to/vault` as a CLI argument.

## Starting the server

```bash
cd mcp-server
npm run build
SCHIST_VAULT_PATH=/path/to/vault node dist/index.js
```

## Tool exposure model

<!-- Implementation: mcp-server/src/tool-registry.ts + src/index.ts -->

The server lists **all** tools at `ListTools` time — read, write, memory,
and the capability meta-tool. Write-capable tools are gated at **call
time**, not at listing time: an agent that calls a write tool without
first calling `request_capabilities` gets a `VALIDATION_ERROR`.

Always-listed tools (callable immediately):

- `get_context`, `search_notes` — vault read
- `search_memory`, `get_agent_state`, `list_domains` — memory read
- `request_capabilities` — meta-tool to unlock write invocations

Always-listed tools (callable only after `request_capabilities({capability: "write"})`):

- `get_note`, `create_note`, `add_connection`, `list_concepts`, `query_graph` — vault write
- `add_memory`, `set_agent_state`, `delete_agent_state`, `add_concept_alias` — memory write

To enable write invocations:

```json
{"name": "request_capabilities", "arguments": {"capability": "write"}}
```

**Why list everything up-front.** MCP clients like Claude Code cache
tool discovery at session start and never re-fetch. If write tools were
only listed after the capability unlock (the pre-v0.1.0 design), those
clients would never see them — `add_memory` and friends would be
unreachable from Claude Code even after a successful `request_capabilities`
call. Listing all tools unconditionally costs a few kB in the one-time
ListTools response in exchange for making write tools actually usable.
The call-time gate preserves the explicit-opt-in model.

## Post-commit ingestion

Wire the post-commit hook inside your vault's `.git/hooks/post-commit` to keep
the SQLite index in sync after every commit. The hook should call:

```bash
python3 /path/to/schist/ingestion/ingest.py \
  --vault /path/to/vault \
  --db /path/to/vault/.schist/schist.db
```

The hook file lives in `.git/hooks/` and is never committed to any repository.

## Path validation

The MCP server enforces that all file writes stay within `SCHIST_VAULT_PATH`.
Any relative path resolving outside the vault root is rejected with a
`PATH_TRAVERSAL` error (confirmed error code — see `assertPathSafe()` in
`mcp-server/src/git-writer.ts`).
