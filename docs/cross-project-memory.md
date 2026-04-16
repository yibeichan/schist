# Cross-Project Agent Memory

Schist ships a small memory subsystem — a single SQLite database at
`~/.openclaw/memory/agent-state.db` — that stores lessons, decisions, and
keyed state for AI agents. It is **user-home-level, not per-vault**: every
Claude project on the same machine that talks to the schist MCP server
shares the same memory DB, scoped by agent identity, not by project.

This doc explains how to wire schist into any Claude project (not just the
project that contains the research vault), and the tag convention we use to
scope memories by project without changing the schema.

## Why this exists

Lessons learned in one project ("the reason FTS5 MATCH sanitization needs
to drop hyphens", "how this team prefers PR reviews to be structured") are
usually useful in other projects too. Writing them to a per-project memory
file means they stay trapped there. Schist's memory tables are indexed,
searchable, and shared by default — so the same `add_memory` /
`search_memory` calls work from any project, and results flow across
project boundaries.

## The DB location

| | |
|---|---|
| Default path | `~/.openclaw/memory/agent-state.db` |
| Override env var | `SCHIST_MEMORY_DB=/custom/path.db` |
| Ownership | one file per user, shared across every schist MCP invocation |
| Relation to vault DB | independent — the vault SQLite at `<vault>/.schist/schist.db` is a separate file and is rebuilt on every commit; the memory DB is never touched by ingestion |

To silo a single project's memory, set `SCHIST_MEMORY_DB` to a
project-specific path in that project's MCP config. The default is shared
because that is the valuable case — silo-by-default would defeat the
purpose.

## Wiring schist MCP into any Claude project

The schist MCP server only requires a vault path; nothing else binds it to
a specific project. Point two or more Claude projects at the same schist
build and the same agent identity, and they will share memory.

**Minimal `.mcp.json` fragment for a non-schist project:**

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/absolute/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/absolute/path/to/your/schist/vault",
        "SCHIST_AGENT_ID": "your-agent-id"
      }
    }
  }
}
```

`SCHIST_VAULT_PATH` is still required — the MCP server refuses to start
without a vault, even for memory-only use. Point it at any valid schist
vault (an empty one works). `SCHIST_AGENT_ID` is the identity used to own
new memory entries; see the Identity section below.

## Tool surface

The memory subsystem exposes six MCP tools. All tools are listed by
`ListTools`; write tools require `request_capabilities({capability:
"write"})` before calls succeed. The gate is at invocation time, not
listing time — see `docs/mcp-setup.md` "Tool exposure model" for the
full rationale.

**Read (callable immediately):**

- `search_memory` — full-text search with optional `owner`, `entry_type`,
  `date_from`, `date_to`, `limit` filters. FTS5 indexes the `content` and
  `tags` columns, so tag-scoped queries work via the `query` field.
- `get_agent_state` — fetch a keyed state value (`{key}`).
- `list_domains` — list the research domain taxonomy (separate from memory,
  included here because it shares the read surface).

**Write (callable after `request_capabilities`):**

- `add_memory` — store a new entry. Required: `owner`, `entry_type` (one of
  `decision | lesson | blocker | completion | observation`), `content`.
  Optional: `date`, `tags` (string array), `related_doc`, `source_ref`,
  `confidence` (`low | medium | high`).
- `set_agent_state` — upsert a keyed state value with optional `ttl_hours`.
  Key prefix must match `owner` (e.g. `sansan.*`); the `team.*` prefix
  requires `owner=eleven`.
- `delete_agent_state` — remove a keyed state value.

The `owner` field on write is enforced to equal `SCHIST_AGENT_ID`. Writes
without `SCHIST_AGENT_ID` set fail with `CONFIG_ERROR`; writes with a
mismatched owner fail with `VALIDATION_ERROR`. Reads are unrestricted.

The `request_capabilities` success response now lists every write tool
that becomes callable — both vault and memory — in its `message` and
`tools` fields.

## Identity: `SCHIST_AGENT_ID`

Every memory row carries an `owner` column. Set `SCHIST_AGENT_ID` once per
project (same value across projects if you want a shared identity) and all
writes from that project will be attributed to it. The ID is free-form —
we recommend matching the agent identity you use for vault writes
(`sansan`, `eleven`, `claude`, etc.).

`search_memory` can filter by owner, so cross-identity retrieval works if
you need it.

## Project scoping: the `project:<slug>` tag convention

The memory schema has no project / scope / namespace column. Instead,
**stamp each entry with a `project:<slug>` tag in the `tags` array**, and
query by matching that tag in the `search_memory` `query` field (FTS5
indexes tags alongside content).

| | |
|---|---|
| Slug source | git repo name (`basename $(git rev-parse --show-toplevel)`) |
| Outside a git repo | prompt the user rather than guessing from `$PWD` |
| Extra tags | anything else — `topic:*`, `severity:*`, freeform — stack them in the same array |

**Why a tag, not a column:** FTS5 already indexes `tags`, the schema is
stable for v0.1.0, and the convention is trivially reversible — if tag
matching proves noisy at scale, we can add an indexed `scope` column later
without breaking existing entries. (Migration would set `scope` by
parsing `project:*` out of `tags`.)

**Match precision — read this carefully.** `search_memory` sanitizes its
`query` by wrapping each whitespace-separated token in FTS5 phrase
quotes (see `sanitizeFtsQuery` in `mcp-server/src/sqlite-reader.ts`).
The default `unicode61` tokenizer then strips punctuation (including
`:` and `-`) and matches the resulting token sequence as an adjacent
phrase against the JSON-serialized `tags` column.

Practical consequences:
- `query: "my-other-project"` tokenizes to the phrase `my other project`,
  which matches a tag array containing `project:my-other-project` because
  the JSON string tokenizes to `project my other project` (the `my other
  project` sub-phrase is present). This is the simplest and most robust
  way to scope recall — **use the slug alone as the query**, not a
  `project:<slug>` compound.
- `query: "project:my-other-project"` also works in practice (same
  tokens, just with an extra leading `project` token), but the match is
  a phrase match on token order, not a structured key/value lookup. Do
  not rely on the colon having any semantic meaning to FTS5.
- Two slugs that are substring-prefixes of each other (`schist` vs.
  `schist-vault`) cannot be distinguished by FTS alone. Pair with an
  `owner` filter, or choose distinctive slugs.

If precise tag equality matters, query the DB directly with SQL (or a
future CLI flag) — FTS is for recall, not for structured lookups.

## Example flows

**Capturing a lesson from within a non-schist project:**

```
> SCHIST_AGENT_ID=sansan
> cwd: /home/user/code/my-other-project  (git repo)
> project slug: my-other-project

MCP: request_capabilities({capability: "write"})
MCP: add_memory({
  owner: "sansan",
  entry_type: "lesson",
  content: "FTS5 MATCH rejects hyphens inside unquoted queries — wrap in double quotes or strip them before querying.",
  tags: ["project:my-other-project", "topic:fts5"],
  confidence: "high"
})
```

**Recalling lessons relevant to the current project:**

```
> cwd: /home/user/code/my-other-project

MCP: search_memory({
  query: "my-other-project fts5",
  entry_type: "lesson",
  limit: 10
})
```

Note the query uses the slug directly (`my-other-project`), not the
compound `project:my-other-project`. See the "Match precision" note
above.

**Cross-project recall (no project filter):**

```
MCP: search_memory({
  query: "fts5 MATCH hyphens",
  entry_type: "lesson"
})
```

## Fallback: the `schist-memory` CLI

If an agent cannot speak MCP (e.g. a shell script running outside any
Claude host), use the compiled CLI that ships with the MCP server. It
registers as `schist-memory` via the package `bin`, but the binary is
only on `$PATH` if you install the package globally — run
`npm install -g` inside `mcp-server/` (after `npm run build`), or invoke
`node dist/cli/memory-cli.js` directly:

```bash
# After `npm run build && npm install -g` in mcp-server/:
schist-memory add-memory \
  --agent sansan --type lesson \
  --tags project:my-other-project,topic:fts5 \
  "FTS5 MATCH rejects hyphens inside unquoted queries..."

# Or without a global install:
node /path/to/schist/mcp-server/dist/cli/memory-cli.js add-memory \
  --agent sansan --type lesson \
  --tags project:my-other-project,topic:fts5 \
  "..."
```

Subcommands: `add-memory`, `search`, `state get`, `state set`. Content for
`add-memory` and `state set` is a positional argument. `--agent` is
mandatory on writes (the CLI does not fall back to `SCHIST_AGENT_ID` for
the flag itself, but `SCHIST_AGENT_ID` is still required in the
environment for write validation; `SCHIST_MEMORY_DB` drives the DB path
as it does for the MCP server).

## Privacy notes

- **One DB per user across all projects.** By default, lessons captured
  from a work project are visible from a personal project on the same
  machine. Silo with `SCHIST_MEMORY_DB` per project if that matters.
- **No remote sync.** Memory does not flow through the hub/spoke sync
  path — `agent-state.db` is strictly local. Moving memory between
  machines is an explicit copy.
- **Append-only is not enforced.** Unlike vault notes, memory entries can
  be deleted via direct SQLite access (not via MCP — there is no
  `delete_memory` tool). Retention policy is the user's responsibility.
