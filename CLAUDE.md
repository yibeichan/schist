# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is schist

Agent-first knowledge graph. Git is truth, SQLite is query, humans just watch. AI agents write markdown+YAML frontmatter into a git vault; SQLite is rebuilt on every commit as a disposable query layer; a static D3.js viewer gives humans read-only access.

## Build & Test Commands

### MCP Server (TypeScript, in `mcp-server/`)
```bash
cd mcp-server && npm install        # install deps
npm run build                        # compile TS → dist/
npm run dev                          # watch mode
npm test                             # jest (ESM mode)
npm test -- --testPathPatterns=<pattern>  # run a single test file (Jest 30+)
```

### CLI (Python, in `cli/`)
```bash
pip install -e ./cli                 # editable install
python -m pytest cli/tests/          # run all tests
python -m pytest cli/tests/test_acl.py  # single test file
python -m pytest cli/tests/test_acl.py::test_name -v  # single test
```

### Ingestion (Python, packaged with CLI)
The ingester ships as part of the `schist` package and is exposed as the
`schist-ingest` console script (registered in `cli/pyproject.toml`).
```bash
pip install -e ./cli                                   # provides schist-ingest
schist-ingest --vault <path> --db <path>               # rebuild SQLite from markdown
```

### Viewer
```bash
python viewer/build.py --db ./vault/.schist/schist.db --out ./viewer/static/data
```

## Architecture

Three-layer design: **Agent Layer** (MCP clients, CLI) → **MCP Server** (tool router) → **Storage** (git vault + SQLite).

### Data flow
1. Agent calls MCP tool (e.g. `create_note`) → MCP server writes markdown file + git commit (serialized via async-mutex)
2. Post-commit hook → invokes `schist-ingest` (or `$SCHIST_INGEST_SCRIPT`) to rebuild SQLite from all markdown files
3. Read tools (`search_notes`, `query_graph`) query SQLite directly (read-only)
4. CI/viewer build: SQLite → `graph.json` + `search-index.json` → static site

### Hub & spoke (multi-machine)

When the vault is a spoke (has `.schist/spoke.yaml`), the MCP server adds two
behaviors on top of the data flow above:

- **Auto-push after writes:** `create_note` and `add_connection` fire a
  detached `python3 -m schist sync push` after the local commit. Errors are
  logged but never block the agent.
- **Auto-pull before `get_context`:** bounded by a 5s timeout; falls through
  silently on failure so a flaky hub never stalls reads. Other read tools
  (`search_notes`, `query_graph`, `list_concepts`) do NOT auto-pull — agents
  call `get_context` at session start to refresh.

The hub is a bare git repo with a pre-receive hook that enforces vault.yaml
ACLs. Create one with `schist init --hub --hub-path /path --name X
--participant a --participant b`. Full setup: `docs/hub-spoke-setup.md`.

### Key invariants
- **Git is canonical** — SQLite is derived and disposable, rebuilt from scratch each ingest
- **Append-only** — no deletes or in-place edits via MCP/CLI; to revise, create a new note with `contradicts:`/`extends:` connection
- **Serialized writes** — `git-writer.ts` uses async-mutex (10s timeout) to prevent concurrent git conflicts
- **SELECT-only SQL** — `query_graph` tool rejects mutations

### Component map
| Component | Language | Entry point | Role |
|-----------|----------|-------------|------|
| MCP Server | TypeScript | `mcp-server/src/index.ts` | Primary agent interface, tool router |
| Git Writer | TypeScript | `mcp-server/src/git-writer.ts` | Mutex-serialized git commits |
| SQLite Reader | TypeScript | `mcp-server/src/sqlite-reader.ts` | Read-only FTS5 queries |
| CLI | Python | `cli/schist/__main__.py` | Agent fallback + human CLI |
| ACL Parser | Python | `cli/schist/acl.py` | vault.yaml scope resolution, rate limits |
| Ingestion | Python | `cli/schist/ingest.py` (entry: `schist-ingest`) | Markdown → SQLite rebuild |
| Viewer | Python+JS | `viewer/build.py`, `viewer/src/index.html` | Static D3.js graph + lunr.js search |

### Schema
- **Document notes** (`notes/`, `papers/`): `YYYY-MM-DD-slug.md` with YAML frontmatter (title, date, tags, status, concepts, related) and optional `## Connections` section
- **Concept nodes** (`concepts/`): `slug.md` — stable reference nodes, no date/status/connections
- **Connection types**: extends, contradicts, supports, replicates, applies-method-of, reinterprets, related
- Full spec: `schema/SCHEMA.md`, vault config spec: `schema/vault-yaml.md`

### SQLite tables
Vault DB (`<vault>/.schist/schist.db`): `docs`, `concepts`, `edges`, `docs_fts` (FTS5), `domains`, `concept_aliases` — defined in `cli/schist/schema.sql` (shipped as package data). `docs`/`concepts`/`edges`/`docs_fts`/`domains` are dropped and rebuilt on every ingest — the first four from markdown, `domains` from `vault.yaml`'s top-level `domains:` list (source of truth per `schema/vault-yaml.md`). `concept_aliases` uses `CREATE TABLE IF NOT EXISTS` and survives commit-path rebuilds; on spoke-pull rebuilds it's copied forward from the backup by `_preserve_side_tables` in `cli/schist/sync.py`.

Memory DB (`~/.openclaw/memory/agent-state.db` by default, or `SCHIST_MEMORY_DB`): `agent_memory`, `agent_memory_fts`, `agent_state` — schema inlined in `mcp-server/src/sqlite-reader.ts` as `MEMORY_SCHEMA`. Separate file from the vault DB, never touched by ingestion.

## Git Hooks
- **post-commit** — triggers SQLite ingestion
- **pre-commit** — rejects secrets/API keys in staged files
- **pre-receive** — server-side validation (ACLs, rate limits, schema)

## Requirements
- Node.js >= 20, Python >= 3.12, SQLite >= 3.39 (FTS5), Git >= 2.30

## Environment
- `SCHIST_VAULT_PATH` — path to the vault directory (required by MCP server)
