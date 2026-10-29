# @schist/mcp-server

The [MCP](https://modelcontextprotocol.io/) server for
[schist](https://github.com/yibeichan/schist), a knowledge vault for agents
built on git and SQLite. It gives an MCP client tools to search, read and
write a vault's notes and concepts, follow the links between them, and keep
agent memory across sessions.

## Install

```bash
pip install schist                  # the CLI: creates, indexes and syncs vaults
npm install -g @schist/mcp-server   # this server; needs Node.js 22 or later
```

## Connect a client

Create a vault with the CLI, then print the MCP config for it:

```bash
schist init ~/vaults/my-project --name my-project --identity local
schist --vault ~/vaults/my-project init --print-mcp-config --identity local \
  --mcp-server-path "$(npm root -g)/@schist/mcp-server/dist/index.js"
```

For Claude Code it prints a `claude mcp add` command to run; pass
`--format cursor` for a Cursor config. Any other MCP client runs the server
with `node` and these environment variables:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/node_modules/@schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/path/to/vault",
        "SCHIST_AGENT_ID": "local",
        "SCHIST_IDENTITY": "local"
      }
    }
  }
}
```

- `SCHIST_VAULT_PATH`: the vault the server reads and writes.
- `SCHIST_AGENT_ID`: the identity write tools act as; without it (or
  `SCHIST_ALLOWED_AGENTS` for several agents sharing one server) every write
  tool returns `CONFIG_ERROR`.
- `SCHIST_IDENTITY`: the participant name a hub checks when the server pushes
  a spoke vault's changes.
- `SCHIST_MEMORY_DB`: the agent memory database (default
  `~/.schist/memory/agent-state.db`).

`schist doctor --vault <vault>` checks the setup, including the client's MCP
entry.

## Agent memory from the shell

The package also installs `schist-memory`, which reads and writes the same
memory database as the server's `add_memory` and `search_memory` tools:

```bash
schist-memory add-memory --agent local --type lesson "content"
schist-memory search "query" --limit 10
```

## Documentation

- [Getting started](https://github.com/yibeichan/schist/blob/main/docs/getting-started.md)
- [MCP tools](https://github.com/yibeichan/schist#mcp-tools)
- [Sharing a vault through a hub](https://github.com/yibeichan/schist/blob/main/docs/hub-spoke-setup.md)
- [Cross-project memory](https://github.com/yibeichan/schist/blob/main/docs/cross-project-memory.md)

## License

AGPL-3.0-or-later. See [LICENSE](https://github.com/yibeichan/schist/blob/main/LICENSE).
