# MCP Context Efficiency — Design Spec

**Tracks:** [yibeichan/schist#50](https://github.com/yibeichan/schist/issues/50)
**Implementation plan:** `docs/superpowers/plans/2026-05-04-mcp-context-efficiency.md`
**Audit baseline:** `docs/superpowers/specs/audit-2026-05-04-mcp-response-sizes.md`

This spec is the contract every implementation PR (2–8 in the rollout
plan) references during review. It locks in protocol decisions before
any tool changes.

---

## Goal

Reduce agent context burn from MCP tool responses, especially as the
vault grows. Adopt cursor-based pagination and reason-string opt-ins so
agents can't fall back to default-bloat behavior.

The 2026-05-10 audit established the targets. `query_graph` and
`search_memory` together account for >270KB / >70K tokens of single-call
worst case on a 70-doc vault — both will scale linearly with the corpus
and clear typical context limits within a few hundred docs.

---

## Core principles

> **Enforcement belongs in the protocol, not the prompt.** Agents take
> the path of least resistance — passive hints get ignored, boolean
> opt-ins drift toward always-on. (m13v on #50, schist memory #67.)

Two operational consequences:

1. **Cursor tokens, not truncation flags.** When results are capped,
   return a structured cursor that must be consumed to advance. The
   server stores recent `(queryHash, owner)` pairs and refuses to
   re-serve identical queries without cursor consumption — blind retries
   become *structurally* impossible, not just discouraged.

2. **Reason strings, not boolean opt-ins.** Where verbose / full-body
   access is needed, gate it behind `verbose: "<reason string>"` instead
   of `verbose: true`. Adds friction against lazy default-creep and
   produces an auditable log of when expensive paths are actually used.

Everything below is the concrete protocol implementing these two
principles.

---

## Cursor protocol

### Token shape

A cursor is an opaque-to-the-agent, HMAC-signed JSON payload. The
server emits it as a single base64url string in the response. Agents
echo it back verbatim on the follow-up call.

The signed payload (before encoding) is:

```json
{
  "tool": "search_notes",
  "queryHash": "<sha256 hex of canonicalized (args + owner)>",
  "offset": 20,
  "issuedAt": 1717459200,
  "ttlSeconds": 300
}
```

- **`tool`**: the tool that issued the cursor. Refused if presented to
  any other tool — prevents cross-tool cursor reuse.
- **`queryHash`**: SHA-256 of the canonical-JSON serialization of the
  call's normalized arguments plus the active owner. Tool-specific
  details below.
- **`offset`**: zero-indexed position the cursor advances to (i.e. the
  first row of the *next* page).
- **`issuedAt`**: unix seconds; cursor age is checked against
  `ttlSeconds` on every redemption.
- **`ttlSeconds`**: fixed at **300** for all cursors (locked decision).

Encoded as `base64url(payload) + "." + base64url(hmac_sha256(secret, payload))`.

The HMAC secret is **per-process, regenerated on every server start**
(locked decision). Cursors do not survive `mcp-server` restarts; agents
hitting an expired/invalid cursor get a structured error and start over
from page 1. This is acceptable because cursor TTL is 300s anyway.

### queryHash canonicalization

To make the hash stable across argument-order variations and JSON
formatting, every tool computes:

```
queryHash = sha256(
  JSON.stringify(
    sortKeys({ ...normalizedArgs, _owner: ownerOrEmptyString })
  )
)
```

Where `normalizedArgs`:
- excludes the `cursor` field (which is meta, not part of the query
  identity);
- excludes any `verbose` field (verbose changes the *response shape*,
  not the *query identity* — we want both verbose and non-verbose
  re-issues of the same query to count as identical);
- normalizes string-vs-undefined for optional fields (omitted ↔ empty
  ↔ null all collapse to omitted);
- normalizes number ranges where applicable (e.g. `limit: 0` ↔
  `limit: undefined`).

`sortKeys` is a recursive deep-sort so `{a:1,b:2}` and `{b:2,a:1}` hash
identically.

### Server-side identical-query refusal

When a tool returns a cursor (i.e. results were capped), the server
stores `(tool, queryHash, owner, issuedAt, lastCursor)` in an in-memory
LRU (size 256). On a subsequent call:

| Condition | Behavior |
|-----------|----------|
| Same `(tool, queryHash, owner)` within `ttlSeconds`, NO cursor passed | Return `{ error: "CURSOR_REQUIRED", message: "Identical query within ${ttl}s — pass cursor or refine.", cursor: <reissued token> }`. Agent must consume the cursor to advance, OR change at least one query argument. |
| Same `(tool, queryHash, owner)`, cursor passed and validated | Serve next page; advance LRU entry's `offset`. |
| Different `queryHash` (refined query) | Treat as a new query; previous cursor expires from LRU naturally. |
| TTL expired | Treat as new query; emit a soft `cursorNote: "previous cursor expired after Xs"` field in the response so agents can debug. |

LRU is in-process Map with size cap 256 and explicit per-call expiry
sweep. Cleared on process restart (acceptable — cursors are TTL-bound
to 300s anyway).

### Tool-specific cursor adoption

| Tool | `queryHash` includes | `offset` semantics | Next-page SQL shape |
|------|----------------------|--------------------|----------------------|
| `search_notes` | `query`, `limit`, `tags?`, `status?`, `owner` | Row offset into the FTS5 result set, ordered by FTS5 rank then `date DESC`. | Existing `search_notes` SQL + `LIMIT :limit OFFSET :offset`. |
| `search_memory` | `query?`, `owner?`, `entry_type?`, `date_from?`, `date_to?`, `limit`, ownerOrEmpty | Row offset into the memory result set, ordered by `date DESC, id DESC`. | Existing memory query + `LIMIT :limit OFFSET :offset`. |
| `query_graph` | normalized SQL string, `params?`, ownerOrEmpty | Row offset; SQL gets `LIMIT :limit OFFSET :offset` *injected* when not present (see "Default limits" below). | Caller's SQL with server-managed `LIMIT/OFFSET` appended. |
| `list_concepts` | `q?`, `domain?`, `limit`, ownerOrEmpty | Row offset, ordered by `slug ASC`. | Existing `list_concepts` SQL + `LIMIT :limit OFFSET :offset`. |
| `list_domains` | `limit`, ownerOrEmpty | Row offset, ordered by `slug ASC`. | Domains query + `LIMIT :limit OFFSET :offset`. |

`get_note` does NOT use cursors — it's an explicit single-doc fetch.
`get_context` does NOT use cursors — its response is a fixed-shape
summary, not a paginated list.

---

## Reason-string verbose

Tools that have a "give me more" mode replace `verbose: boolean` with
`verbose: string`.

**Validation rules (locked):**

- The string must be **≥ 12 characters** after trimming whitespace.
  Rejects lazy `verbose: "x"` workarounds while still allowing a short
  but meaningful reason like `"need full body for code review"`.
- The string is logged to the MCP server's stderr at INFO level:
  `[verbose] <tool> by <owner|anonymous>: <reason>`. Provides an
  auditable trail of when expensive paths actually fire.
- Empty string, whitespace-only string, or omitted field → not verbose.
  No silent fallback; `verbose: true` (boolean) is rejected as a type
  error so callers can't accidentally re-introduce the boolean pattern.

**Tools adopting reason-string verbose (locked):**

- **`get_context`** for `depth: "full"` — currently `full` triggers
  `tagCloud` computation. Without a reason, the server downgrades to
  `standard` and emits a soft hint. Gating is cheap and the cost is
  computational, not just bytes.
- **`search_memory`** for full-content return. Default response carries
  a content snippet (first ~200 chars). Full `content` field requires
  `verbose: "<reason ≥12 chars>"`.

**Tools deliberately excluded:**

- **`search_notes`** does not need verbose. Full bodies are obtained
  via `get_note`, which is already an explicit two-step protocol — the
  better pattern than a verbose flag.
- All `list_*` and `query_graph` tools — their response shape is the
  natural unit; "verbose mode" doesn't make sense semantically.

---

## Default limits

| Tool | Current default | Proposed default | Caller cap | Reasoning |
|------|-----------------|------------------|------------|-----------|
| `search_notes` | `limit: 20` | `limit: 20` (unchanged) | 100 | Already healthy at the audit-baseline corpus (8KB/2K tokens). |
| `list_concepts` | `limit: 50` | `limit: 50` (unchanged) | 200 | Already healthy (4KB/1K tokens at 25 concepts). |
| `list_domains` | **no limit** | **`limit: 100`** | 500 | Domains tend to be tiny but the unbounded default is a footgun for a vault that grows them. |
| `query_graph` | **no default LIMIT** | **`LIMIT 100` injected when caller omits LIMIT** | **caller cap 1000** | Today `SELECT * FROM docs` is unbounded. Audit showed 241KB / 60K tokens on a 70-doc vault. Linear scaling makes this the highest-priority change. **⚠️ This is a behavior change** — see Compatibility below. |
| `get_context` | tiered (minimal / standard / full) | tiered, `full` requires verbose reason | n/a | Tiering is fine; the cost of `full` is the tagCloud computation, gated via reason-string. |
| `search_memory` | `limit: 50`, full content | `limit: 50`, snippet content; full content requires verbose reason | 200 | 41KB/10K tokens dropped to ~12KB/3K tokens estimated under snippet mode. |

`get_note` has no `limit` (single doc fetch by ID) and is unchanged.

---

## Compatibility / migration

### What stays the same

- Existing MCP clients call these tools without `cursor` and without
  `verbose`. Both fields are added as **optional inputs** in
  tool-registry schemas, so unaware callers continue working.
- All response shapes get a new optional `cursor` field. Clients that
  ignore it get the first page only — same as today's truncated-by-limit
  behavior.

### The breaking change

There is exactly **one** user-visible breaking change:

- **`query_graph` injecting `LIMIT 100` when caller omits LIMIT.**
  Today, a caller running `SELECT * FROM docs` on a 1000-doc vault
  gets 1000 rows. Post-PR-4, the same call gets 100 rows + a cursor.
  Power users who included `LIMIT 5000` get capped at 1000.

The `CURSOR_REQUIRED` error is *technically* also new behavior — but
it only triggers when an agent retries an identical query within 300s
without consuming the cursor. That pattern is the agent bug we want to
fail loudly, not graceful behavior we want to preserve.

### Migration steps (PR 8)

- Update every tool's `description` string in `tool-registry.ts` to
  mention pagination and (where relevant) reason-string verbose, so
  agents read the new contract from the input schema itself.
- Update `docs/mcp-setup.md` with the new convention, including a
  worked example of cursor consumption and a callout for the
  `query_graph` LIMIT change.
- Call out the `query_graph` change explicitly in the PR 4 commit
  message and in release notes.

---

## Out-of-scope for this rollout

Calling these out so future readers know they were considered and
deliberately excluded:

- **Authentication / authorization changes.** The existing capability
  gate already covers writes; cursors are read-side only.
- **Streaming responses.** Would require MCP protocol-level changes.
- **Cross-session caching.** Cursors are per-process by design (locked
  decision #2). Persistent caching is orthogonal to context efficiency.
- **Cursor durability across server restart.** TTL is 300s and the
  HMAC secret rotates per process; agents handle restart by re-running
  page 1.
- **Modifying `get_note`'s contract.** Single-doc fetches are
  intentionally explicit.

---

## Self-review checklist

Used to validate the spec before opening PR 1:

- [x] Every implementation PR (2–8) has a section it can point at:
  - PR 2 → "Cursor protocol" + "Reason-string verbose" (defines what
    the shared modules expose).
  - PR 3 → "search_memory" rows in cursor-adoption table + Default
    limits + Reason-string adopters.
  - PR 4 → "query_graph" rows + Default limits + Compatibility
    breaking change.
  - PR 5 → "search_notes" rows in cursor-adoption table.
  - PR 6 → "list_concepts" + "list_domains" rows + Default limits.
  - PR 7 → "get_context" reason-string adopters.
  - PR 8 → "Migration steps" section.
- [x] Cursor format specified concretely enough for PR 2 to implement
  from the spec alone.
- [x] Reason-string rules unambiguous: ≥12 chars after trim, logged
  to stderr, no silent fallback, boolean rejected.
- [x] Compatibility section names the breaking change (query_graph
  default LIMIT) explicitly and proposes mitigation in PR 8.
- [x] No `TBD` / `<fill>` placeholders left in this document.
