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
- `schist doctor` reports `uv` availability (WARN if missing — pip still works as a fallback). Note: hosts without `uv` will now show one new `[WARN] uv: not found` line in the default check output; downstream scripts that scrape doctor output to assert "no warnings" may need adjustment.
- `cli/uv.lock` — checked in for reproducible source installs across Mac / Pi / HPC. CI enforces freshness via `uv lock --check` to catch pyproject.toml edits that forget to regenerate the lock.

### Changed
- **Docs:** `docs/mcp-setup.md` adds a comprehensive **Cursor pagination protocol** section covering all cursor adopters, the worked example for cursor consumption, identical-query refusal semantics, the full cursor error-code table, the `query_graph` server-pagination behavior, and reason-string verbose for `search_memory` + `get_context`. This completes #50 PR 8 — the migration documentation for the 7-tool cursor protocol rollout. Tool registry descriptions are already self-documenting (each cursor adopter advertises pagination + 300s refusal in its description string from PRs 3–7).
- **BEHAVIOR CHANGE:** `get_context` MCP tool — `depth: "full"` now requires `verbose: "<reason ≥12 chars>"` to actually compute the `tagCloud` field. Without a valid reason, the server silently downgrades to `depth: "standard"` and the response carries a `verboseNote` hint indicating the upgrade path. No error is raised on missing/whitespace-only verbose — agents that lazily ask for "full" still get a usable standard response. Type misuse (`verbose: true`) and length misuse (string <12 code points) are rejected as `INVALID_ARG`. The `verbose` field is validated for type on all depths, but its semantic effect (gating tagCloud) only applies on `depth: "full"`. Refs #50 (PR 7 of the context-efficiency rollout).
- **BREAKING:** `list_concepts` MCP tool now returns `{ concepts: Concept[], cursor?: string }` instead of a bare `Concept[]` array, and accepts new `cursor` and `limit` inputs. Pagination is cursor-based with a stable `ORDER BY slug ASC` tiebreaker. Default `limit` is 50; hard cap is 200. Callers that previously iterated `response` as an array must now destructure: `const { concepts } = response`. Refs #50 (PR 6 of the context-efficiency rollout).
- **BREAKING:** `list_domains` MCP tool now returns `{ domains: Domain[], cursor?: string }` instead of a bare `Domain[]` array, and accepts new `cursor` and `limit` inputs. Pagination is cursor-based. Default `limit` is 100; hard cap is 500. Callers that previously iterated `response` as an array must now destructure: `const { domains } = response`. Refs #50 (PR 6 of the context-efficiency rollout).
- `list_domains` now applies a default `limit: 100` (cap 500). Previously unbounded — a footgun on vaults with hundreds of domains. Refs #50.
- **BREAKING:** `query_graph` MCP tool is now server-paginated. The server wraps every caller query as `SELECT * FROM (<caller_sql>) AS user_query LIMIT N OFFSET M` where N defaults to 100 and caps at 1000. A caller running `SELECT * FROM docs` on a 1000-doc vault used to get all 1000 rows; it now gets 100 rows + a `cursor` token. The caller's own `LIMIT` / `ORDER BY` / `OFFSET` inside the SQL are respected verbatim — a caller passing `SELECT * FROM docs LIMIT 5` still gets exactly 5 rows (no cursor, since 5 < 100). Pass the cursor on the next call to advance, or pass a `limit` arg (up to 1000) for one-shot larger results. Response shape gains an optional `cursor` field; `columns` / `rows` / `rowCount` unchanged. Identical queries within 300s without a cursor are refused with `CURSOR_REQUIRED`. Refs #50 (PR 5 of the context-efficiency rollout). Concurrent-ingest caveat: see the spec's "Concurrent-ingest limitation" subsection — long pagination across ingest commits can skip or duplicate rows; bounded by the 300s cursor TTL. Keyset-cursor migration tracked in #90.
- `search_notes` MCP tool now returns `{ results: SearchResult[], cursor?: string }` instead of a bare `SearchResult[]` array, and accepts a new `cursor` input. Pagination is cursor-based and deterministic (bm25 + id-ASC tiebreaker). Identical queries within 300s without a cursor are refused with `CURSOR_REQUIRED`. Default `limit` is unchanged (20); the hard cap is 100. Per spec, `search_notes` has no `verbose` mode — call `get_note` for full bodies. Refs #50 (PR 4 of the context-efficiency rollout).
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
- **`triggerSpokePush` / `maybeSpokePull` now spawn the `schist` console-script** instead of `python3 -m schist` (#120). The `python3 -m schist` form fails silently on hosts where schist is installed via `uv tool install` or `pipx` — both produce the `schist` binary on PATH but install into an isolated venv, so `python3` has no importable `schist` module. Pre-fix this manifested as silent `ModuleNotFoundError` exits during write-heavy sessions: the sentinel `.schist/last-sync-error` recorded each failure but agents never saw the divergence until session end. The console-script approach matches the existing `schist-ingest` pattern at `triggerIngestion`. New `SCHIST_BIN` env var lets operators pin a specific binary path if needed.
- **Write-tool responses now surface `syncWarning`** when `.schist/last-sync-error` is present (#120). `create_note`, `add_connection`, and `assign_domain` read the sentinel on each successful response and include the failure message — without clearing. `get_context` still owns clearing (its existing behavior). This means a write-heavy session that diverges from hub sees the warning on every write rather than discovering the divergence at session end. The warning text points at `get_context` as the acknowledge path.
- setuptools 82+ flat-layout collision with local `cli/hooks/` directory (PR #28)
- Viewer `normalize_endpoint` stripping `.md` from note paths (PR #27)
- MCP `triggerIngestion` path after ingestion move (PR #31, second commit)
- In-process ingest leaving partial DB on failure; now deletes on exception (PR #31)

### Security
- All write tools (`create_note`, `add_connection`, `add_memory`, `set_agent_state`) require explicit capability unlock via `request_capabilities`
- `query_graph` tool rejects non-SELECT SQL
- Git writes serialized via async-mutex (10s timeout) to prevent concurrent commit conflicts

[Unreleased]: https://github.com/yibeichan/schist/compare/v0.0.0...v0.1.0
