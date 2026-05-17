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

The server lists **all** tools at `ListTools` time and accepts calls to
any of them unconditionally. There is no opt-in meta-tool.

Read tools:

- `get_context`, `search_notes`, `get_note`, `list_concepts`, `query_graph` — vault read
- `search_memory`, `get_agent_state`, `list_domains` — memory read

Write tools:

- `create_note`, `add_connection`, `assign_domain` — vault write
- `add_memory`, `set_agent_state`, `delete_agent_state`, `add_concept_alias` — memory write

**Where authorization lives.** Writes are authorized at the data layer by
`validateOwner` (see `mcp-server/src/agent-identity.ts`), which checks the
incoming `owner` against `SCHIST_AGENT_ID` / `SCHIST_ALLOWED_AGENTS`. A
mismatched or missing owner produces `CONFIG_ERROR` or `VALIDATION_ERROR`.
Reads are unrestricted.

**Why no capability meta-tool.** Earlier versions required agents to call
`request_capabilities({capability: "write"})` before writes would succeed.
That gate was unauthenticated (any caller could flip it) and provided no
real access control — it was a UX speed bump that conflicted with both
agent ergonomics and the actual authorization model. Removed in #72.

## Post-commit ingestion

Wire the post-commit hook inside your vault's `.git/hooks/post-commit` to keep
the SQLite index in sync after every commit. The hook should call:

```bash
schist-ingest \
  --vault /path/to/vault \
  --db /path/to/vault/.schist/schist.db
```

(`schist-ingest` is the console script installed by `pip install schist`. If you
work from a clone instead of an installed wheel, use `uv pip install --system -e ./cli`
(or `pip install -e ./cli`) to register it on your `PATH`.)

The hook file lives in `.git/hooks/` and is never committed to any repository.

## Path validation

The MCP server enforces that all file writes stay within `SCHIST_VAULT_PATH`.
Any relative path resolving outside the vault root is rejected with a
`PATH_TRAVERSAL` error (confirmed error code — see `assertPathSafe()` in
`mcp-server/src/git-writer.ts`).
