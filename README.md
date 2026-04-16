# schist

Agent-first knowledge graph. Git is truth, SQLite is query, humans just watch.

**schist** is a generic, domain-agnostic knowledge graph where AI agents are the primary writers. Content is markdown + YAML frontmatter, version-controlled in git. SQLite provides the query layer. A static web viewer (D3.js + lunr.js) gives humans a read-only visualization.

## Why

- Agents need structured knowledge, not just chat history
- Git gives you version control, branching, and collaboration for free
- SQLite gives you instant full-text search and graph queries without a server
- Markdown is readable by both agents and humans
- No vendor lock-in — it's just files

## Quick Start

```bash
# Clone schist
git clone https://github.com/youruser/schist.git
cd schist

# Install CLI
pip install -e ./cli

# Install MCP server dependencies
cd mcp-server && npm install && npm run build && cd ..

# Create a vault
mkdir -p ~/vaults/research/{notes,papers,concepts,logs,.schist}

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

## Multi-Machine (Hub & Spokes)

A single laptop is fine. The real value comes when several machines share one
knowledge graph — e.g. Claude running on a laptop, on an HPC cluster, and on a
Raspberry Pi all writing into the same vault and seeing each other's notes.

schist uses a **hub + spokes** topology. The hub is a bare git repo (on any
machine reachable by SSH) that holds the canonical vault and enforces an ACL
on every push. Each spoke is a sparse-checkout clone of the hub tied to one
scope and one identity.

```
               ┌─────────────┐
               │  Hub (bare) │
               │  vault.git  │
               │  pre-receive│
               └──────┬──────┘
                      │
         ┌────────────┼────────────┐
         │            │            │
    ┌────▼────┐  ┌────▼────┐  ┌────▼────┐
    │ laptop  │  │   HPC   │  │   Pi    │
    │ spoke   │  │  spoke  │  │  spoke  │
    └─────────┘  └─────────┘  └─────────┘
```

On a write, the spoke's MCP server commits locally and pushes to the hub in
the background. On a read, it pulls from the hub first (bounded, falls
through on failure). The hub's pre-receive hook rejects any push that writes
outside the pusher's declared scope.

End-to-end setup: [`docs/hub-spoke-setup.md`](./docs/hub-spoke-setup.md).

## Cross-Project Agent Memory

Schist's memory subsystem (`~/.openclaw/memory/agent-state.db`) is shared
across every Claude project on the same machine, scoped by
`SCHIST_AGENT_ID`. Any project can wire in the schist MCP server to
capture lessons and recall them from elsewhere. Details and the
`project:<slug>` tag convention: [`docs/cross-project-memory.md`](./docs/cross-project-memory.md).

## Agent Integration

### Claude Desktop / Claude Code

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

### Cursor

Add to `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["./schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "./vault"
      }
    }
  }
}
```

### Session Start Hook

For any agent, add this to the system prompt or session init:

```
On session start, run: schist context --vault /path/to/vault
```

This gives the agent a snapshot of graph stats, recent notes, and hot concepts.

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_notes` | Full-text search across all notes |
| `get_note` | Get a note with full content and connections |
| `create_note` | Create a new note (auto-commits to git) |
| `add_connection` | Add a typed edge between two nodes |
| `list_concepts` | List all concept nodes |
| `query_graph` | Run read-only SQL against the graph database |
| `get_context` | Dump graph context for agent session init |

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

## Architecture

- **MCP Server** (Node.js + TypeScript) — primary agent interface via `@modelcontextprotocol/sdk`
- **CLI** (Python) — agent fallback + human command line
- **Ingestion** (Python) — markdown → SQLite, triggered by git post-commit hook
- **Viewer** (static HTML/JS) — D3.js force graph + lunr.js search, deployed to GitHub Pages
- **Git hooks** — post-commit triggers ingestion, pre-commit rejects secrets

See [PLAN.md](./PLAN.md) for the full architecture document.
See [schema/SCHEMA.md](./schema/SCHEMA.md) for the markdown schema specification.

## Security Model

- Agents write to `drafts/` branch, not `main`
- No delete, no force-push, no history rewrite via MCP
- Pre-commit hook rejects commits containing secrets/API keys
- Web viewer is static only — no server-side execution
- Vault repo uses scoped deploy keys

## Requirements

- Node.js ≥ 22
- Python ≥ 3.12
- SQLite ≥ 3.39 (FTS5 support)
- Git ≥ 2.30

## License

MIT
