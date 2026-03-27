# schist — Architecture Plan

> Agent-first knowledge graph. Git is truth, SQLite is query, humans just watch.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    AGENT LAYER                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ Claude   │  │ Cursor   │  │ Any MCP Client       │  │
│  │ Desktop  │  │ / Codex  │  │ (or CLI fallback)    │  │
│  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘  │
│       │              │                   │              │
│       └──────────────┼───────────────────┘              │
│                      │ MCP (stdio/SSE)                  │
└──────────────────────┼──────────────────────────────────┘
                       │
┌──────────────────────┼──────────────────────────────────┐
│              MCP SERVER (Node.js)                        │
│                      │                                  │
│  ┌───────────────────▼────────────────────────────┐     │
│  │ Tool Router                                    │     │
│  │  search_notes · get_note · create_note         │     │
│  │  add_connection · list_concepts                │     │
│  │  query_graph · get_context                     │     │
│  └──────┬────────────────────────────┬────────────┘     │
│         │ writes                     │ reads            │
│  ┌──────▼──────┐            ┌────────▼──────────┐       │
│  │ Git Writer  │            │ SQLite Reader     │       │
│  │ (serialized │            │ (read-only conn)  │       │
│  │  mutex)     │            │                   │       │
│  └──────┬──────┘            └────────▲──────────┘       │
│         │                            │                  │
└─────────┼────────────────────────────┼──────────────────┘
          │                            │
  ┌───────▼────────┐          ┌────────┴──────────┐
  │ VAULT (git)    │          │ SQLite DB         │
  │ markdown/YAML  │──hook──▶│ docs, concepts,   │
  │ docs/ concepts/│  ingest  │ edges, fts5       │
  └───────┬────────┘          └───────────────────┘
          │
  ┌───────▼────────┐
  │ BUILD (CI/hook)│
  │ parse md →     │
  │ graph.json +   │
  │ search-index   │
  └───────┬────────┘
          │
  ┌───────▼────────┐
  │ STATIC VIEWER  │
  │ D3.js force    │
  │ graph + lunr   │
  │ (GitHub Pages) │
  └────────────────┘
```

## Repo Structure

```
schist/
├── mcp-server/
│   ├── src/
│   │   ├── index.ts              # Entry point, server setup
│   │   ├── tools.ts              # Tool definitions & handlers
│   │   ├── git-writer.ts         # Serialized git commit layer
│   │   ├── sqlite-reader.ts      # Read-only SQLite queries
│   │   ├── markdown-parser.ts    # YAML frontmatter + connection parser
│   │   └── types.ts              # Shared TypeScript types
│   ├── package.json
│   └── tsconfig.json
├── ingestion/
│   ├── ingest.py                 # Markdown → SQLite ingestion (~100 lines)
│   ├── schema.sql                # SQLite DDL
│   └── requirements.txt          # pyyaml, python-frontmatter
├── viewer/
│   ├── src/
│   │   ├── graph.js              # D3.js force-directed graph
│   │   ├── search.js             # lunr.js client-side search
│   │   └── main.js               # App entry point
│   ├── static/
│   │   ├── index.html            # Single-page viewer
│   │   └── style.css             # Minimal dark theme
│   └── build.py                  # Markdown → graph.json + search-index.json
├── cli/
│   ├── schist/
│   │   ├── __init__.py
│   │   ├── __main__.py           # CLI entry point
│   │   ├── commands.py           # add, link, search, query, build, context, schema
│   │   ├── git_ops.py            # Git operations (shared with MCP)
│   │   ├── markdown_io.py        # Read/write markdown with frontmatter
│   │   └── sqlite_query.py       # SQLite query helpers
│   ├── pyproject.toml
│   └── setup.cfg
├── hooks/
│   ├── post-commit                # Triggers ingestion after every commit
│   └── pre-commit                 # Rejects secrets/API keys
├── schema/
│   ├── SCHEMA.md                  # Full markdown schema specification
│   └── default.yaml               # Default schema config (connection types, statuses)
├── docs/
│   ├── agent-integration.md       # MCP config snippets for agents
│   └── vault-setup.md             # How to create and configure a vault
├── PLAN.md                        # This file
├── README.md                      # Project overview + quickstart
├── .gitignore
└── LICENSE                        # MIT
```

## SQLite Schema

```sql
-- schist.db — query layer, rebuilt from markdown on every commit
-- NEVER the source of truth. Disposable. Delete and re-ingest anytime.

CREATE TABLE docs (
    id          TEXT PRIMARY KEY,       -- relative path: "notes/2026-03-26-attention.md"
    title       TEXT NOT NULL,
    date        TEXT,                   -- ISO 8601: "2026-03-26"
    status      TEXT DEFAULT 'draft',   -- draft | review | final | archived
    tags        TEXT,                   -- JSON array: '["attention", "transformer"]'
    concepts    TEXT,                   -- JSON array of concept slugs
    body        TEXT NOT NULL,          -- full markdown body (sans frontmatter)
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE concepts (
    slug        TEXT PRIMARY KEY,       -- stable slug: "backpropagation"
    title       TEXT NOT NULL,          -- display name: "Backpropagation"
    description TEXT,                   -- one-liner from concept file
    tags        TEXT,                   -- JSON array
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,          -- doc id or concept slug
    target      TEXT NOT NULL,          -- doc id or concept slug
    type        TEXT NOT NULL,          -- extends | contradicts | supports | replicates | applies-method-of | reinterprets | related
    context     TEXT,                   -- optional annotation
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(source, target, type)
);

-- Full-text search over docs
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title,
    body,
    tags,
    content='docs',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync during ingestion
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;

CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, tags)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags);
END;

-- Indexes
CREATE INDEX idx_edges_source ON edges(source);
CREATE INDEX idx_edges_target ON edges(target);
CREATE INDEX idx_edges_type ON edges(type);
CREATE INDEX idx_docs_status ON docs(status);
CREATE INDEX idx_docs_date ON docs(date);
```

## MCP Server Tool Signatures

All tools use JSON Schema for input validation. Server runs via stdio transport (primary) or SSE for remote access.

### `search_notes`
Full-text search across all documents.
```typescript
{
  name: "search_notes",
  description: "Full-text search across all notes in the knowledge graph",
  inputSchema: {
    type: "object",
    properties: {
      query:  { type: "string", description: "Search query (FTS5 syntax supported)" },
      limit:  { type: "number", description: "Max results", default: 20 },
      status: { type: "string", enum: ["draft", "review", "final", "archived"], description: "Filter by status" },
      tags:   { type: "array", items: { type: "string" }, description: "Filter by tags (AND)" }
    },
    required: ["query"]
  }
}
// Returns: Array of { id, title, date, status, tags, snippet (highlighted) }
```

### `get_note`
Retrieve a single note with its full content and connections.
```typescript
{
  name: "get_note",
  description: "Get a note by ID with full content and connections",
  inputSchema: {
    type: "object",
    properties: {
      id: { type: "string", description: "Note ID (relative path)" }
    },
    required: ["id"]
  }
}
// Returns: { id, title, date, status, tags, concepts, body, connections: [{ target, type, context }] }
```

### `create_note`
Create a new note. Commits to git automatically.
```typescript
{
  name: "create_note",
  description: "Create a new note in the knowledge graph. Auto-commits to git.",
  inputSchema: {
    type: "object",
    properties: {
      title:      { type: "string" },
      body:       { type: "string", description: "Markdown body content" },
      tags:       { type: "array", items: { type: "string" } },
      concepts:   { type: "array", items: { type: "string" }, description: "Concept slugs to link" },
      status:     { type: "string", enum: ["draft", "review", "final"], default: "draft" },
      connections: {
        type: "array",
        items: {
          type: "object",
          properties: {
            target: { type: "string" },
            type:   { type: "string", enum: ["extends", "contradicts", "supports", "replicates", "applies-method-of", "reinterprets"] },
            context: { type: "string" }
          },
          required: ["target", "type"]
        }
      },
      directory:  { type: "string", description: "Subdirectory under vault root", default: "notes" }
    },
    required: ["title", "body"]
  }
}
// Returns: { id, path, commitSha }
```

### `add_connection`
Add a typed edge between two nodes.
```typescript
{
  name: "add_connection",
  description: "Add a typed connection between two notes or concepts",
  inputSchema: {
    type: "object",
    properties: {
      source:  { type: "string", description: "Source note ID or concept slug" },
      target:  { type: "string", description: "Target note ID or concept slug" },
      type:    { type: "string", enum: ["extends", "contradicts", "supports", "replicates", "applies-method-of", "reinterprets", "related"] },
      context: { type: "string", description: "Optional annotation explaining the connection" }
    },
    required: ["source", "target", "type"]
  }
}
// Appends to source doc's ## Connections section. Commits to git.
// Returns: { source, target, type, commitSha }
```

### `list_concepts`
List all concept nodes with optional filtering.
```typescript
{
  name: "list_concepts",
  description: "List all concepts in the knowledge graph",
  inputSchema: {
    type: "object",
    properties: {
      tags:   { type: "array", items: { type: "string" }, description: "Filter by tags" },
      search: { type: "string", description: "Substring match on title/description" },
      limit:  { type: "number", default: 50 }
    }
  }
}
// Returns: Array of { slug, title, description, tags, edgeCount }
```

### `query_graph`
Run read-only SQL against the SQLite database. Power tool for agents.
```typescript
{
  name: "query_graph",
  description: "Execute a read-only SQL query against the knowledge graph database",
  inputSchema: {
    type: "object",
    properties: {
      sql:    { type: "string", description: "SQL query (SELECT only, no mutations)" },
      params: { type: "array", items: {}, description: "Bind parameters" }
    },
    required: ["sql"]
  }
}
// Validates: query must start with SELECT or WITH. No INSERT/UPDATE/DELETE/DROP/ALTER.
// Returns: { columns: string[], rows: any[][], rowCount: number }
```

### `get_context`
Dump session context for agent startup — graph stats, recent activity, hot concepts.
```typescript
{
  name: "get_context",
  description: "Get knowledge graph context summary for agent session initialization",
  inputSchema: {
    type: "object",
    properties: {
      depth: { type: "string", enum: ["minimal", "standard", "full"], default: "standard" }
    }
  }
}
// Returns: {
//   vault: { name, path, noteCount, conceptCount, edgeCount },
//   recent: [{ id, title, date, status }],  // last 10 modified
//   hotConcepts: [{ slug, title, edgeCount }],  // top 10 by connections
//   schema: { connectionTypes, statuses, directories },
//   branch: string,
//   lastCommit: { sha, message, date }
// }
```

## Markdown Schema Spec

See `schema/SCHEMA.md` for the full specification. Summary:

### Document Notes (`notes/`, `papers/`, or custom directories)

```yaml
---
title: "Attention Is All You Need — Key Insights"
date: 2026-03-26
tags: [attention, transformer, architecture]
status: draft
concepts: [self-attention, transformer, scaled-dot-product]
related: [notes/2026-03-20-rnn-limitations.md]
---

Body content in standard markdown.

## Connections

- extends: notes/2026-03-20-rnn-limitations.md "Builds on the limitations identified"
- contradicts: notes/2026-03-15-rnns-sufficient.md "Demonstrates RNNs are not necessary"
- supports: concepts/self-attention "Provides the foundational architecture"
```

### Concept Nodes (`concepts/`)

```yaml
---
title: "Self-Attention"
tags: [mechanism, neural-network]
aliases: [self-attn, intra-attention]
---

One-paragraph definition. Concept files are reference nodes — stable anchors that documents link to.
```

### Rules

1. **File naming**: `YYYY-MM-DD-slug.md` for notes, `slug.md` for concepts
2. **Slug derivation**: lowercase, hyphens, no special chars. `"Self-Attention"` → `self-attention`
3. **Connection syntax**: `- type: target "optional context"` in `## Connections`
4. **Append-only**: Never edit a conclusion in-place. Add a new note that `contradicts:` or `extends:` the old one.
5. **Status lifecycle**: `draft` → `review` → `final` → `archived`. Only forward transitions.
6. **Tags**: lowercase, hyphenated. Flat namespace (no hierarchies).

## CLI Command Reference

Installed as `schist` via `pip install -e ./cli`.

### `schist add`
```
schist add --vault ~/vaults/research \
  --title "Attention Is All You Need" \
  --tags attention,transformer \
  --concepts self-attention,transformer \
  --status draft \
  --body "Content here or reads from stdin"
```
Creates the markdown file, commits to git. Prints the note ID.

### `schist link`
```
schist link --vault ~/vaults/research \
  --source notes/2026-03-26-attention.md \
  --target concepts/self-attention.md \
  --type supports \
  --context "Provides foundational architecture"
```
Appends to the source doc's `## Connections` section. Commits.

### `schist search`
```
schist search --vault ~/vaults/research "attention mechanism"
schist search --vault ~/vaults/research "attention" --status final --tags transformer
```
Queries FTS5. Returns ranked results with snippets.

### `schist query`
```
schist query --vault ~/vaults/research \
  "SELECT d.title, COUNT(e.id) as connections
   FROM docs d LEFT JOIN edges e ON d.id = e.source
   GROUP BY d.id ORDER BY connections DESC LIMIT 10"
```
Raw SQL against the SQLite DB. SELECT only.

### `schist build`
```
schist build --vault ~/vaults/research --out ./viewer/static/data
```
Parses all markdown → outputs `graph.json` and `search-index.json` for the static viewer.

### `schist context`
```
schist context --vault ~/vaults/research --depth standard
```
Dumps session context (same as `get_context` MCP tool). Use as agent SessionStart hook.

### `schist schema`
```
schist schema --vault ~/vaults/research
schist schema --vault ~/vaults/research --validate
```
Prints the active schema config. With `--validate`, checks all vault files against the schema.

## Build Pipeline

### Local Development
```
vault commit → post-commit hook → python ingest.py → schist.db updated
```

### GitHub Actions (for viewer deployment)
```yaml
# .github/workflows/deploy-viewer.yml
name: Deploy Viewer
on:
  push:
    branches: [main]
    paths: ['vault/**']

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          submodules: true
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - run: pip install pyyaml python-frontmatter
      - run: python viewer/build.py --vault ./vault --out ./viewer/static/data
      - uses: actions/upload-pages-artifact@v3
        with:
          path: ./viewer/static

  deploy:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
    steps:
      - uses: actions/deploy-pages@v4
```

### Local Viewer (Tailscale)
```bash
# Serve on Tailscale IP only
cd viewer/static
python -m http.server 8420 --bind $(tailscale ip -4)
```

## Agent Integration Guide

### Claude Desktop / Claude Code
```json
// ~/.claude/settings.json or project .claude/settings.json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/path/to/vault",
        "SCHIST_DB_PATH": "/path/to/vault/.schist/schist.db"
      }
    }
  }
}
```

### Cursor
```json
// .cursor/mcp.json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["./mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "./vault"
      }
    }
  }
}
```

### OpenClaw
```yaml
# In gateway config, add as MCP skill
skills:
  - name: schist
    type: mcp
    command: node
    args: ["/path/to/schist/mcp-server/dist/index.js"]
    env:
      SCHIST_VAULT_PATH: /path/to/vault
```

### Session Start Hook
Add to your agent's system prompt or session init:
```
On session start, run: schist context --vault /path/to/vault --depth standard
```
This gives the agent a snapshot of the graph state before it starts working.

## Phase Build Order

### Phase 1: Foundation (2-3 hours)
**Goal:** Markdown schema + SQLite ingestion working end-to-end.

- [ ] Write `schema/default.yaml` — connection types, statuses, directory config
- [ ] Write `ingestion/schema.sql` — full DDL
- [ ] Write `ingestion/ingest.py` — parse markdown vault → populate SQLite
- [ ] Write `hooks/post-commit` — trigger ingestion
- [ ] Write `hooks/pre-commit` — secret detection (grep for common patterns)
- [ ] Test: create sample vault, run ingestion, verify SQLite contents

### Phase 2: CLI (2-3 hours)
**Goal:** `schist` command fully operational.

- [ ] Scaffold `cli/` Python package with `pyproject.toml`
- [ ] Implement `schist add` — create note, commit
- [ ] Implement `schist link` — add connection, commit
- [ ] Implement `schist search` — FTS5 query
- [ ] Implement `schist query` — raw SQL passthrough
- [ ] Implement `schist build` — generate `graph.json` + `search-index.json`
- [ ] Implement `schist context` — graph stats dump
- [ ] Implement `schist schema` — print/validate schema
- [ ] Test: full CLI workflow with sample vault

### Phase 3: MCP Server (3-4 hours)
**Goal:** Agents can read/write the graph via MCP protocol.

- [ ] Scaffold `mcp-server/` with `@modelcontextprotocol/sdk` v1.28.0
- [ ] Implement all 7 tool handlers
- [ ] Implement git-writer with mutex (serialized commits)
- [ ] Implement SQLite reader (read-only connection)
- [ ] Implement markdown parser for frontmatter + connections
- [ ] Wire stdio transport
- [ ] Test: connect via MCP inspector, exercise all tools

### Phase 4: Static Viewer (2-3 hours)
**Goal:** Read-only web visualization deployed.

- [ ] Write `viewer/build.py` — markdown → `graph.json` + `search-index.json`
- [ ] Write D3.js force graph (`viewer/src/graph.js`)
- [ ] Write lunr.js search (`viewer/src/search.js`)
- [ ] Write `viewer/static/index.html` + `style.css`
- [ ] Write GitHub Actions workflow for auto-deploy
- [ ] Test: build from sample vault, verify graph renders

### Phase 5: Integration & Polish (1-2 hours)
**Goal:** Everything wired together, documented, tested.

- [ ] Write agent integration configs (Claude, Cursor, OpenClaw)
- [ ] Write `docs/agent-integration.md`
- [ ] Write `docs/vault-setup.md`
- [ ] Polish README with quickstart guide
- [ ] End-to-end test: agent creates note via MCP → ingestion runs → viewer shows it
- [ ] Tag v0.1.0

**Total estimated effort: 10-15 hours**

## Key Design Decisions

1. **SQLite is disposable.** Delete it anytime, re-ingest from markdown. Git is the only source of truth.
2. **No delete operations.** Append-only knowledge. Archive instead of delete. This is intentional — knowledge graphs should grow, not shrink.
3. **Serialized writes via mutex.** One commit at a time. Concurrent agent writes queue up. This is simpler and more correct than merge conflict resolution.
4. **Concept slugs are stable identifiers.** Once a concept slug exists, it never changes. Title can be updated, slug cannot.
5. **Connection types are a closed set per schema.** Default: extends, contradicts, supports, replicates, applies-method-of, reinterprets, related. Configurable in `schema/default.yaml`.
6. **Vault is a separate repo.** `schist` is the tool, vault is the content. They're decoupled via `--vault-path`. One schist installation can serve multiple vaults.
7. **Drafts branch for agent writes.** Agents commit to `drafts/`, human reviews and merges to `main`. This is the safety boundary.
8. **FTS5 for search, not embeddings.** Embeddings add complexity (model dependency, vector DB). FTS5 is fast, zero-dependency, and sufficient for structured knowledge graphs where connections carry more signal than semantic similarity.
9. **Static viewer, no server.** GitHub Pages or `python -m http.server` on Tailscale. No auth needed — if you can reach the Tailscale IP, you're authorized.
10. **Python CLI + Node MCP server.** Python for CLI/ingestion (better markdown/YAML ecosystem, faster scripting). Node for MCP server (SDK is Node-first, TypeScript types). They share the SQLite DB and git repo, not code.
