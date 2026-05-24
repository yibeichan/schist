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
- `search_memory`, `get_agent_state` — memory read

Write tools:

- `create_note`, `add_connection` — vault write
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

## Cursor pagination protocol

<!-- Spec: docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md -->
<!-- Implementation: mcp-server/src/protocol/cursor.ts -->

Read tools that can return many rows use **cursor pagination** to cap response
size and protect the agent's context window. Tools that adopted the protocol
(across #50 PRs 3–7):

| Tool | Default `limit` | Cap | Cursor | Verbose mode |
|------|-----------------|-----|--------|--------------|
| `search_notes` | 20 | 100 | yes | no (call `get_note` for bodies) |
| `search_memory` | 50 | 200 | yes | yes — `verbose: "<reason ≥12 chars>"` returns full `content` (default is a 200-cp snippet) |
| `query_graph` | 100 | 1000 | yes | no |
| `list_concepts` | 50 | 200 | yes | no |
| `get_context` | n/a (tiered: `minimal` / `standard` / `full`) | n/a | no | yes — `depth: "full"` requires `verbose: "<reason ≥12 chars>"`; without it the server downgrades to `standard` + emits a `verboseNote` hint |

`get_note` and `get_agent_state` are single-fetch by ID and don't paginate.

### Consuming a cursor

Each capped response wraps results in `{ <rows>: [...], cursor?: string }`.
When `cursor` is present, echo it back **verbatim** on the next call (alongside
the same query args) to fetch the next page. Drop the cursor (or change the
query) to start over.

```jsonc
// Call 1 — first page
agent.call("search_notes", { query: "schist", limit: 20 })
// → { results: [...20 rows...], cursor: "eyJ0b29sIjo..." }

// Call 2 — next page (echo cursor verbatim, same args)
agent.call("search_notes", { query: "schist", limit: 20, cursor: "eyJ0b29sIjo..." })
// → { results: [...20 rows...], cursor: "eyJ0b29sIjo..." }  // or no cursor on last page
```

The cursor is a HMAC-signed `payload.signature` base64url token. It encodes
the tool name, the canonicalized query hash, and the page offset. **Per-process**
secret rotates on server restart — cursors don't survive a restart (agents
re-page from the start).

### Identical-query refusal

Calling a read tool with the **same** args (excluding `cursor` and `verbose`)
twice within **300 seconds**, without passing the cursor from the first call,
is refused:

```jsonc
{ "error": "CURSOR_REQUIRED",
  "message": "Identical query within 300s — pass the cursor you received on the previous response, or refine the query." }
```

Rationale: a blind retry on a 100-row capped query just burns context. Either
advance with the cursor, refine the query, or wait out the 300s TTL. The
refusal is **per-owner** (keyed by tool + queryHash + active owner identity),
so concurrent agents don't poison each other's refusal buckets.

### Cursor error codes

| Code | Meaning |
|------|---------|
| `CURSOR_REQUIRED` | Identical-query refusal — pass the prior cursor or refine the query. |
| `CURSOR_EXPIRED` | Cursor is past its 300s TTL. Restart pagination from page 1. |
| `CURSOR_WRONG_TOOL` | Cursor was issued for a different tool. |
| `CURSOR_INVALID_SIGNATURE` | Cursor is malformed, server-restarted (HMAC rotated), or its `queryHash` doesn't match the current args. Restart pagination. |
| `INVALID_ARG` | `limit` / `verbose` / args contain unhashable values (NaN, BigInt, etc.) or `verbose` is the wrong type/length. |

### `query_graph` — server-paginated SELECT

Unlike the other read tools where pagination wraps a fixed query, `query_graph`
runs **arbitrary SELECT/WITH** SQL written by the agent. The server wraps the
caller's query as:

```sql
SELECT * FROM (<your_sql>) AS user_query LIMIT N OFFSET M
```

where `N` defaults to 100, caps at 1000, and `M` advances via cursor. **The
caller's own `LIMIT` / `ORDER BY` / `OFFSET` inside the SQL are respected
verbatim** — `SELECT * FROM docs LIMIT 5` still returns exactly 5 rows (no
cursor, since 5 < 100). This is a **behavior change** from the pre-rollout
contract where unbounded SELECTs would return every row. See `CHANGELOG.md`
under "BREAKING: `query_graph`" for migration guidance.

Only `SELECT` and `WITH` are allowed; mutation keywords are rejected with
`INVALID_SQL`.

### Reason-string verbose

`search_memory` and `get_context` accept an optional `verbose: "<reason>"`
input that unlocks a richer response:

- **`search_memory`** — default returns a 200-code-point snippet of `content`
  per entry. Pass `verbose: "diagnosing flaky test in foo_spec"` (≥12 code
  points after trim) to receive the full content field instead.
- **`get_context`** — `depth: "full"` only computes `tagCloud` when a valid
  verbose reason is supplied. Without it, the server silently downgrades to
  `depth: "standard"` and the response carries a `verboseNote` hint.

Verbose reasons are written to **server stderr** as audit lines:

```
[verbose] search_memory by "yibei": "diagnosing flaky test in foo_spec"
```

Frequent repetition of the same reason on the same (tool, owner) pair within
60 seconds adds `verboseNote: "reason pattern is frequent — consider sampling
at operator level"` to the response. The threshold is 30 calls/minute per
(tool, owner, sha256(reason)) bucket.

**Wrong types are rejected hard** (no silent fallback): `verbose: true` or
`verbose: 0` returns `INVALID_ARG`. Strings shorter than 12 trimmed code
points also return `INVALID_ARG` (matches the spec's "commit to a real
reason" intent). Whitespace-only and omitted are treated as "no verbose
intent" — `get_context` downgrades; `search_memory` returns snippets.

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
