# schist

**Agent-first knowledge graph. Git is truth, SQLite is query, humans just watch.**

Your AI agents write code, run ops, do research. But between sessions, they forget everything. Chat history isn't memory. Vector databases aren't knowledge. You need structure — notes, concepts, connections — that agents can write to and read from, across projects, across machines, across time.

**schist** is that structure. A markdown-native knowledge graph where agents are the primary writers. Content lives in git (versioned, branched, merged). SQLite gives you instant search and graph queries. A static web viewer lets humans see what agents know.

No vendor lock-in. No cloud dependency. No server to run. Just files.

## Why schist

If you're using AI agents for real work, you've hit these problems:

- **Agents repeat research** they did last session because context windows don't persist
- **Decisions get lost** — that architecture choice from Tuesday? Gone.
- **No cross-project memory** — your coding agent doesn't know what your ops agent learned
- **Tool output isn't knowledge** — logs, search results, and transcripts aren't structured

schist gives agents a persistent, queryable knowledge base that survives session restarts.

## 30-Second Demo

```bash
# Install
pip install schist
npm install -g @schist/mcp-server


# Create a vault
schist init ~/vaults/my-project --name my-project --identity local

# Verify everything works
schist doctor --vault ~/vaults/my-project

# Add a note (you or your agent)
schist add --vault ~/vaults/my-project \
  --title "Why we chose SQLite over Postgres" \
  --tags architecture,decision \
  --body "SQLite is file-based, zero-config, and fast enough for our read patterns."

# Search it
schist search --vault ~/vaults/my-project "sqlite"
```

Then plug it into your agent with one command:

```bash
schist --vault ~/vaults/my-project init --print-mcp-config --identity local
```

Your agent now has `search_notes`, `create_note`, `get_context`, and more — persistent knowledge across every session.
The MCP server also advertises usage instructions to compatible clients, telling agents to prefer the indexed schist tools (`search_notes`, `search_memory`, `query_graph`, `get_context`) over filesystem grep/find for vault content and to persist new knowledge through `create_note` or `add_memory`.

## What It Looks Like

Notes are markdown + YAML frontmatter:

```markdown
---
title: Why we chose SQLite over Postgres
date: 2026-05-17
tags: [architecture, decision]
status: final
connections:
  - target: concepts/sqlite
    type: supports
---

SQLite is file-based, zero-config, and fast enough for our read patterns...
```

The static viewer renders a D3.js force graph of your knowledge:

```
   [SQLite] ──supports──▶ [Why we chose SQLite]
       │                        │
   extends                    supports
       │                        │
       ▼                        ▼
   [Database] ────────▶ [Architecture Decisions]
```

## How It's Different

| | schist | Obsidian | Notion | Raw markdown |
|--|--------|----------|--------|-------------|
| **Primary writer** | AI agents | Humans | Humans | Anyone |
| **Version control** | Git (built-in) | Manual sync | Cloud | Manual |
| **Query engine** | SQLite + FTS5 | Plugin-based | API | grep |
| **Multi-machine sync** | Hub + spoke (git) | Sync service | Cloud | Manual |
| **Agent interface** | MCP + CLI | None | API | None |
| **Offline** | Fully | Mostly | No | Yes |
| **Vendor lock-in** | None (it's files) | Vault format | Proprietary | None |

## Multi-Machine (Hub & Spokes)

A single laptop works. The real power comes when multiple machines share one knowledge graph — Claude on your laptop, an agent on an HPC cluster, a Raspberry Pi running ops, all writing into the same vault.

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

Each spoke commits locally and pushes to the hub. The hub's pre-receive hook enforces ACLs — agents can only write within their declared scope. Setup guide: [`docs/hub-spoke-setup.md`](./docs/hub-spoke-setup.md).

## Agent Integration

schist speaks [MCP](https://modelcontextprotocol.io/) — works with any MCP-compatible agent.

### Claude Code

```bash
schist --vault /path/to/vault init --print-mcp-config --format claude --identity local
# Run the printed `claude mcp add` command
```

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["./schist/mcp-server/dist/index.js"],
      "env": { "SCHIST_VAULT_PATH": "./vault" }
    }
  }
}
```

### Any MCP Client

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/schist/mcp-server/dist/index.js"],
      "env": { "SCHIST_VAULT_PATH": "/path/to/vault" }
    }
  }
}
```

### Session Start Hook

Add to your system prompt or session init:

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
| `search_memory` | Search cross-project agent memory |
| `add_memory` | Add a memory entry (decision, lesson, blocker, etc.) |

## CLI Commands

| Command | Description |
|---------|-------------|
| `schist init` | Create a new vault |
| `schist doctor` | Verify setup health |
| `schist add` | Create a new note |
| `schist link` | Add a connection between nodes |
| `schist search` | Full-text search |
| `schist query` | Raw SQL query |
| `schist build` | Generate static viewer data |
| `schist context` | Dump session context |
| `schist schema` | Print or validate schema |

## Architecture

```
Agent ──MCP──▶ ┌─────────────────────────┐
               │     MCP Server (Node)    │
               │  tools → git-writer      │
               │          → sqlite-reader │
               └──────┬──────────┬────────┘
                      │ writes   │ reads
               ┌──────▼──┐  ┌────▼──────┐
               │ Git repo│  │ SQLite DB │
               │ (truth) │  │ (query)   │
               └──────┬──┘  └───────────┘
                      │ ingest (post-commit hook)
               ┌──────▼──────────┐
               │ Static Viewer   │
               │ D3.js + lunr.js │
               └─────────────────┘
```

- **Git is truth** — markdown + YAML, versioned, branched, mergeable
- **SQLite is query** — rebuilt from markdown on every commit, disposable
- **MCP is the interface** — agents read and write through standard protocol
- **Viewer is optional** — humans can browse, but agents don't need it

Full architecture: [PLAN.md](./PLAN.md). Schema: [schema/SCHEMA.md](./schema/SCHEMA.md). Authoring conventions: [CONVENTIONS.md](./CONVENTIONS.md).

## Security

- Hub pre-receive hook enforces vault.yaml ACLs per-scope and per-agent
- No delete, no force-push, no history rewrite via MCP
- Pre-commit hook rejects commits containing secrets/API keys
- Web viewer is static only — no server-side execution
- Write authorization enforced against `SCHIST_AGENT_ID` / `SCHIST_ALLOWED_AGENTS`
- `query_graph` rejects non-SELECT SQL

MCP read tools assume a trusted caller with full vault read access. Deployment
constraints and the full trust model are documented in [SECURITY.md](./SECURITY.md).

## Cross-Project Agent Memory

schist includes a shared memory subsystem scoped by agent identity. Any project using the schist MCP server can capture lessons, decisions, and blockers — and recall them from any other project on the same machine. Details: [`docs/cross-project-memory.md`](./docs/cross-project-memory.md).

## Getting Started

Full platform-specific setup guide: [`docs/getting-started.md`](./docs/getting-started.md)

**Requirements:** Node.js ≥ 20, Python ≥ 3.12, SQLite ≥ 3.39 (FTS5), Git ≥ 2.30

## License

MIT
