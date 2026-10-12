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

The MCP server assumes every caller that can speak to its stdio transport is a
trusted first-party agent. Do not expose it to untrusted local processes or to a
network without an external authentication and authorization proxy. The full
threat model is in [`../SECURITY.md`](../SECURITY.md).

Read tools:

- `get_context`, `sync_status`, `search_notes`, `get_note`, `list_concepts`, `query_graph` — vault/sync read
- `search_memory`, `get_agent_state` — memory read

Write tools:

- `create_note`, `create_concept`, `update_note`, `delete_note`, `add_connection`, `sync_retry` — vault/sync write
- `add_concept_alias` — vault DB side-table write
- `add_memory`, `set_agent_state`, `delete_agent_state` — memory write

**Where authorization lives.** Writes are authorized at the data layer by
`validateOwner` (see `mcp-server/src/agent-identity.ts`), which checks the
incoming caller identity (`owner`, or `created_by` for `add_concept_alias`)
against `SCHIST_AGENT_ID` / `SCHIST_ALLOWED_AGENTS`. A mismatched or missing
identity produces `CONFIG_ERROR` or `VALIDATION_ERROR`.
Markdown writes (`create_note`, `create_concept`, `update_note`, `delete_note`,
and `add_connection`) are additionally
checked against `vault.yaml` write grants when that file is present and locally
parseable, with the hub pre-receive hook as the authoritative enforcement point
for git pushes. Reads are unrestricted: `vault.yaml` read grants are not
enforced by local MCP read tools.

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

`query_graph` is a trusted-agent tool, not a SQL sandbox. Callers can read any
table in the vault SQLite database, including schema tables and side tables.
Access control belongs at the MCP process boundary (`SCHIST_ALLOWED_AGENTS`,
stdio process isolation, or an external auth proxy for any non-stdio
transport). Do not expose the MCP server directly to a network.

Two resource caps protect the MCP process from accidental or adversarial
resource burn:

- `SCHIST_QUERY_GRAPH_TIMEOUT_MS` — wall-clock timeout for the SQLite worker;
  default `5000`.
- `SCHIST_QUERY_GRAPH_BYTE_BUDGET` — maximum JSON response size before
  `QUERY_RESPONSE_TOO_LARGE`; default `10485760` (10 MiB).

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

## Vault write lock

Every process that touches the vault's git index or rewrites a note holds one
exclusive lock, `<vault>/.schist/write.lock`, for exactly its own work: the
CLI's `add`, `add-concept`, `link`, `sync push` and `sync pull` commands and each MCP server's writes. Without
it, two writers' `git add` and `git commit` interleave, one commit sweeps the
other's staged file in under the wrong message, and a read-modify-write such as
`add_connection` loses the other writer's edge. A holder never keeps the lock
while it spawns a child that takes it (that would deadlock). No network call
runs under the lock: `schist sync pull` fetches first, without it, and then
takes it only to clean up and rebase onto the fetched branch, so an unreachable
hub never holds up writers. A failed fetch is reported (unreachable, refused,
and so on) without the lock ever being taken. The fetch names its refspec,
`+refs/heads/<branch>:refs/remotes/origin/<branch>`, because a spoke is a
single-branch clone whose configured refspec covers only the hub's default
branch. If the vault's branch changed while the fetch ran, the pull rebases
nothing and asks to be run again. `schist sync pull --lock-timeout <seconds>`
gives up waiting for the lock after that long, changes nothing, and exits 75;
the server's pull before a read passes `--lock-timeout 1`, so a pull that
cannot get the lock soon is skipped rather than killed mid-rebase by that
pull's 5 s limit.
The lock is re-entrant within a process.

Node has no portable `flock`, so the server holds the lock through a helper,
`schist lock hold`, for the span of one write. Closing the helper's stdin
releases the lock; so does either process dying. Run by hand, the helper
defaults to `--timeout 10` and `--max-hold 120`.

| Helper output | Exit code | Meaning |
|---|---|---|
| `LOCKED` | 0 once stdin reaches EOF | Lock acquired; held until EOF |
| `TIMEOUT holder=<host:pid>` | 75 | Not acquired within `--timeout`; the holder note runs to the end of the line |
| `EXPIRED` | 76 | Held past `--max-hold`; the helper released it |
| (usage error) | 2 | A CLI too old to have `lock hold`, or a value that is not a finite number of seconds |

Inside the server the order is always the in-process `writeMutex` first, then
the vault lock. The server starts the helper in its own process group and
kills the whole group on a timeout, so a wrapper script that does not `exec`
the helper cannot leave it running with the lock. The write is checked as
still held before it touches the file and again before the commit; a commit
that finished after the lock was lost succeeds with a `commitWarning` that
says so.

Only two helper failures fall back to serializing within the server's own
process: a missing binary, and a CLI too old to have `lock hold` (usage error
`invalid choice: 'lock'`). The server then logs once
`cross-process vault lock unavailable: <reason>`; upgrade the CLI to restore
cross-process safety. Any other failure before `LOCKED` (a mount without lock
support, a permission error on `.schist/`, a crash) fails the write with
`IO_ERROR` and the helper's last stderr line, and nothing is written: writing
without the lock is what the lock prevents.

Once it holds the lock, a write also refuses, with `SYNC_DIRTY`, while a
rebase is unfinished (`rebase-merge` or `rebase-apply` in the git directory,
left by an interrupted `sync pull`). A commit made then would be undone when
the next sync aborts that rebase. `sync_retry`, or `schist sync pull` in the
vault, clears the state; then retry the write.

The server's background push (`sync push --no-commit` without `--force`)
usually takes no lock: it stages and commits nothing. The exception is leftover
rebase state (`.git/rebase-merge` or `.git/rebase-apply`). That state may
belong to a `sync pull` that is still rebasing under the lock, so the push takes
the lock, checks again, and only then aborts what is left. The server's own
`git rebase --abort` after a pull conflict also takes the lock; if the lock
stays busy past `SCHIST_VAULT_LOCK_WAIT_MS`, the abort is skipped and the error
names the holder.

Timing to expect:

- A write waiting on the cross-process lock can take up to
  `SCHIST_VAULT_LOCK_WAIT_MS` plus about 5 s. Writes queued behind it in the
  same server fail fast at the 10 s `writeMutex` timeout (`WRITE_TIMEOUT`);
  retry them.
- `sync_retry` holds the in-process write mutex around its CLI children for up
  to about 60 s, so concurrent MCP writes may return `WRITE_TIMEOUT` meanwhile
  and should be retried.
- The helper's `--max-hold` is the larger of 600 s and three times
  `SCHIST_GIT_COMMIT_TIMEOUT_MS`, enough for a whole write on a stalled NFS
  mount. It only matters for a server that is alive but hung: a server that
  exits closes the helper's stdin, which ends the hold at once.

On NFS, `flock` works across hosts under NFSv4. If a node crashes while
holding the lock, the NFS server reclaims it when the lease expires, about 90 s
later. There is one lock file per vault path; a symlinked or dual-mounted
path resolves to the same file, so that is fine.

Linux emulates `flock` on NFS with POSIX byte-range locks, which belong to the
process: closing any descriptor on the file releases the lock. schist keeps
exactly one descriptor on `write.lock` while it holds the lock, so do not add
code that opens or closes that file while holding it. Two setups make the lock
per-host instead of vault-wide, and then writers on different hosts are no
longer serialized: an NFS mount with `local_lock=flock` or `local_lock=all`,
and `.schist` symlinked to node-local disk. Use them only when a single host
writes to the vault.

### Upgrading: CLI first, then restart every server

CLIs from before this lock serialize on `.schist/cli-write.lock` only, and an
MCP server keeps the code it was started with, so a server started before the
upgrade takes no cross-process lock at all. To close that window:

1. Upgrade the CLI on every host that writes to the vault.
2. Restart every MCP server on every host that shares the vault (each Claude
   session's server, on every spoke that mounts it).

Until the next release the new CLI, and so every new server through its
helper, also takes `cli-write.lock` after `write.lock`, which keeps an older
CLI that is still installed somewhere excluded from new writers. That cannot
deadlock, because an older CLI only ever takes `cli-write.lock`. A server that
logs `cross-process vault lock unavailable: <reason>` is running against a CLI
too old for `schist lock hold` (or one it cannot find, see `SCHIST_LOCK_BIN`);
it keeps working but serializes only within its own process. Upgrade that CLI
and restart the server to clear it.

| Variable | Default | Effect |
|---|---|---|
| `SCHIST_LOCK_BIN` | `SCHIST_BIN`, then `schist` | Binary that provides `lock hold` |
| `SCHIST_VAULT_LOCK_WAIT_MS` | `10000` | How long a write waits for the lock, capped at 600000; read on each call |

The acceptance test `mcp-server/tests/vault-lock-acceptance.test.ts` runs two
server instances and a CLI writer against one vault and checks that every
commit holds exactly its own file.

## Path validation

The MCP server enforces that all file writes stay within `SCHIST_VAULT_PATH`.
Any relative path resolving outside the vault root is rejected with a
`PATH_TRAVERSAL` error (confirmed error code — see `assertPathSafe()` in
`mcp-server/src/git-writer.ts`).
