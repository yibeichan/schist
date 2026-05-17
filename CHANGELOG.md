# Changelog

All notable changes to schist are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added
- `schist doctor` command — one-command health check for schist setup (Python, Node, Git, vault, SQLite, hooks, MCP config)
- `schist init --print-mcp-config` — generates ready-to-paste MCP server config for Claude Code and Cursor
- `docs/getting-started.md` — linear onboarding guide with platform-specific instructions (Linux, macOS, HPC)
- `docs/hub-spoke-pi-hpc-mac.md` — opinionated topology guide for Pi hub + HPC/Mac spoke setup
- `schist doctor` reports `uv` availability (WARN if missing — pip still works as a fallback)
- `cli/uv.lock` — checked in for reproducible source installs across Mac / Pi / HPC

### Changed
- Node.js minimum version relaxed from >=22 to >=20 (no Node 22-specific features used)
- MCP server no longer requires `request_capabilities` before write tools — the gate provided no real access control and added friction in shared-MCP deployments (PR #76, closes #72, #73). Memory-write authorization continues to be enforced at the data layer by `validateOwner` against `SCHIST_AGENT_ID` / `SCHIST_ALLOWED_AGENTS`. Vault-write identity enforcement is tracked in #63.
- Recommended Python package manager for source installs is now [uv](https://docs.astral.sh/uv/) — faster installs, lockfile-aware. The published `pip install schist` end-user path is unchanged; dev installs should prefer `uv pip install --system -e ./cli`. Docs, CI, and error messages updated accordingly. pip remains a supported fallback.

### Removed
- `request_capabilities` MCP meta-tool. Calling it now returns `VALIDATION_ERROR: Unknown tool: request_capabilities` (PR #76).

## [0.1.0] - Unreleased

### Added
- Agent-first knowledge graph with markdown + YAML frontmatter storage
- MCP server (`@schist/mcp-server`) for Claude Desktop, Claude Code, and Cursor integration
- Python CLI (`schist`) with full CRUD operations for notes, connections, and concepts
- SQLite ingestion layer triggered by git post-commit hook (FTS5 search, graph queries)
- Static D3.js viewer with force-directed graph and lunr.js search
- Hub & spoke multi-machine topology with ACL-based scoped writes
- Pre-commit hook that rejects commits containing secrets or API keys
- Cross-project agent memory subsystem (`~/.openclaw/memory/agent-state.db`)
- `/learn` and `/recall` CLI skills for cross-project lesson capture and retrieval

### Changed
- Moved ingestion module from standalone `ingestion/` into `schist` package (PR #31)
- `schist-ingest` now shipped as console script via `pip install schist`
- MCP server now lists all tools at `ListTools` time; write access gated at call-time (PR #30)

### Fixed
- setuptools 82+ flat-layout collision with local `cli/hooks/` directory (PR #28)
- Viewer `normalize_endpoint` stripping `.md` from note paths (PR #27)
- MCP `triggerIngestion` path after ingestion move (PR #31, second commit)
- In-process ingest leaving partial DB on failure; now deletes on exception (PR #31)

### Security
- All write tools (`create_note`, `add_connection`, `add_memory`, `set_agent_state`) require explicit capability unlock via `request_capabilities`
- `query_graph` tool rejects non-SELECT SQL
- Git writes serialized via async-mutex (10s timeout) to prevent concurrent commit conflicts

[Unreleased]: https://github.com/yibeichan/schist/compare/v0.0.0...v0.1.0
