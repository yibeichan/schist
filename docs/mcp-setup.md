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

<!-- Implementation: mcp-server/src/index.ts — see PR#1 + PR#2 -->

The server starts in read-only mode. Only three tools are available by default:

- `get_context` — vault summary (note/concept/edge counts + recent notes). Defaults to `depth: "minimal"`.
- `search_notes` — full-text search.
- `request_capabilities` — meta-tool to unlock write tools (see below).

To unlock write tools (`create_note`, `add_connection`, `get_note`,
`list_concepts`, `query_graph`), the agent calls:

```json
{"name": "request_capabilities", "arguments": {"capability": "write"}}
```

This design minimises token cost for read-only sessions (CLI search, context
loading) while keeping full write capability available when needed.

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
