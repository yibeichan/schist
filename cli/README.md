# schist

Agent-first knowledge graph. Git is truth, SQLite is query, humans just watch.

**schist** is a generic, domain-agnostic knowledge graph where AI agents are the primary writers. Content is markdown + YAML frontmatter, version-controlled in git. SQLite provides the query layer. A static web viewer (D3.js + lunr.js) gives humans a read-only visualization.

## Installation

```bash
pip install schist
```

This installs the `schist` CLI and the `schist-ingest` console script (used by the git post-commit hook).

## Quick Start

```bash
# Create a vault
mkdir -p ~/vaults/research/{notes,papers,concepts,.schist}

# Add your first note
schist add --vault ~/vaults/research \
  --title "First Note" \
  --tags getting-started \
  --body "Hello, knowledge graph."

# Search it
schist search --vault ~/vaults/research "knowledge"

# Get graph context (useful for agent session start)
schist context --vault ~/vaults/research
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `schist add` | Create a new note |
| `schist link` | Add a connection between nodes |
| `schist search` | Full-text search |
| `schist query` | Raw SQL query |
| `schist build` | Generate static viewer data |
| `schist context` | Dump session context |
| `schist schema` | Print or validate schema |

## MCP Server

The schist MCP server (`@schist/mcp-server`) is published separately on npm:

```bash
npm install -g @schist/mcp-server
```

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/absolute/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/absolute/path/to/vault"
      }
    }
  }
}
```

See the [full README](https://github.com/yibeichan/schist#readme) for architecture docs, hub & spoke setup, and MCP tool reference.

## License

MIT
