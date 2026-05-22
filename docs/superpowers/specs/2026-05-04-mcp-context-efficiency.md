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

Encoded as `base64url(payload) + "." + base64url(hmac_sha256(secret, payload))`,
where `base64url` is the unpadded RFC 4648 §5 variant (no trailing `=`).
Unpadded keeps cursors compact and URL-safe in any context.

The HMAC secret is **per-process, regenerated on every server start**
(locked decision). Cursors do not survive `mcp-server` restarts;
agents hitting an invalid cursor get one of the four distinct error
codes documented below ("Cursor error codes") and restart from page
1. This is acceptable because cursor TTL is 300s anyway. See
"Multi-process cursor scope" below for the implications when more
than one MCP server runs against the same vault.

### queryHash canonicalization

To make the hash stable across argument-order variations and JSON
formatting, every tool computes:

```
queryHash = sha256(
  JSON.stringify({
    args:  sortKeys(normalize(callerArgs)),
    owner: ownerOrEmptyString
  })
)
```

Note: `args` and `owner` live in **disjoint top-level keys**, not a
merged object. This forecloses two failure modes: (a) a future tool
adding an `_owner` arg colliding with a flat `_owner` injection, and
(b) prototype pollution from a malicious arg key like `__proto__` if
the canonicalizer used `Object.assign`. The canonicalizer must use
`Object.create(null)` (or an equivalent prototype-free dict) for any
intermediate object.

Where `normalize(callerArgs)`:
- excludes the `cursor` field (meta, not part of the query identity);
- excludes the `verbose` field (verbose changes the *response shape*
  for the current call only; see "Reason-string verbose" for how
  newly-set verbose interacts with identical-query refusal);
- treats `undefined`, `null`, and missing keys as identical (collapse
  to missing);
- treats empty string `""` as missing for optional string fields;
- normalizes number ranges (`limit: 0` ↔ `limit: undefined`);
- **rejects unhashable JS values** before normalization with a
  `INVALID_ARG` tool error (not a silent collision): `NaN`, `+Infinity`,
  `-Infinity`, `BigInt`, functions, symbols, circular references. These
  either crash `JSON.stringify` or collapse to `null`, causing hash
  collisions across genuinely distinct queries;
- **NFC-normalizes** every string value via `String.prototype.normalize("NFC")`
  before hashing, so visually-identical Unicode forms hash identically
  (e.g. `"café"` precomposed vs `"café"` combining-accent).

`sortKeys` is a recursive deep-sort over object keys (arrays preserve
their order — array element ordering is part of the query identity).

### Server-side identical-query refusal

When a tool returns a cursor (results were capped), the server stores
`(tool, queryHash, owner, issuedAt)` in an in-memory LRU (size 256).
On a subsequent call:

| Condition | Behavior |
|-----------|----------|
| Same `(tool, queryHash, owner)` within `ttlSeconds`, NO cursor passed, NO newly-set `verbose` | Return `{ error: "CURSOR_REQUIRED", message: "Identical query within ${ttl}s — pass the cursor you received on the previous response, or refine the query." }`. **No cursor is reissued in the error** — see the rationale below. |
| Same `(tool, queryHash, owner)`, NO cursor passed, BUT `verbose` is newly set | Treat as a new query and serve page 1 with the verbose-mode response shape. Rationale: the agent went from "give me snippets" to "now I need full content for these results"; refusing that would create a refusal loop. The `verbose` flag itself is **not** part of `queryHash` (so a subsequent identical+verbose retry IS still refused). |
| Cursor passed and HMAC-validates | Serve next page; HMAC-signed `offset` is the source of truth. LRU is consulted only to update `lastCursor` for the refusal-detection path; missing LRU entry is fine. |
| Different `queryHash` (refined query) | Treat as a new query; previous LRU entry expires naturally. |
| TTL expired | Distinct error code: `CURSOR_EXPIRED` (see "Cursor error codes" below). |

**No cursor reissue on `CURSOR_REQUIRED`.** Embedding a fresh cursor in
the refusal response would defeat the "structurally impossible to
retry" property: the agent could ignore the original cursor, hit
`CURSOR_REQUIRED`, and use the *reissued* cursor without ever having
inspected the data — same blind-retry pattern this protocol exists to
prevent. The agent must either echo back the cursor it already
received on the previous response, or change at least one query
argument.

**Refusal is best-effort, not a guarantee.** The LRU is sized at 256
entries with eviction on the standard read-or-write LRU rule (every
access bumps an entry to most-recently-used). A server under load
that issues 257+ distinct cursors within `ttlSeconds` will evict the
oldest entry; an identical re-query at that point will *not* be
refused. The HMAC-signed cursor TTL remains the binding security
constraint — refusal is a best-effort guard against agent bugs, not a
load-bearing security boundary.

### Cursor error codes

The previous "everything is `CURSOR_REQUIRED`" rule collapsed four
distinct failure modes into one error, leaving agents unable to
distinguish "I'm doing this wrong" from "the server restarted." PR 2
must surface them as separate codes:

| Code | Meaning | Agent action |
|------|---------|--------------|
| `CURSOR_REQUIRED` | Identical-query refusal: same `(tool, queryHash, owner)` within TTL, no cursor passed. | Echo back the cursor from the previous response, or refine the query. |
| `CURSOR_EXPIRED` | Cursor TTL exceeded. | Restart pagination from page 1; the underlying data may have changed. |
| `CURSOR_INVALID_SIGNATURE` | HMAC verification failed (forged cursor, or cursor from a different process — see "Multi-process" below). | Restart pagination from page 1. Repeated failures indicate a configuration issue. |
| `CURSOR_WRONG_TOOL` | Cursor presented to a tool that didn't issue it (cross-tool replay). | Restart pagination from page 1. Likely an agent bug. |

All four are returned as `{ error: "<code>", message: "<human-readable>" }`
tool envelopes — no cursor field on any of them.

### Cursor binding to queryHash

When an agent presents a cursor on a follow-up call, the tool computes the
current call's `queryHash` from the current args + active owner (excluding
`cursor` and `verbose` as per the canonicalization rule) and compares it
to the cursor's encoded `queryHash`. **Mismatch → `CURSOR_INVALID_SIGNATURE`**
with message `"cursor was issued for a different query — restart pagination
from page 1"`.

Rationale: the cursor's `queryHash` is part of the signed payload so the
tool can verify the cursor binds to *this* query, not some other query
whose pagination state happens to be valid. Allowing the cursor's payload
to override args silently turns a query-refinement attempt into a
continuation of the prior query — which weakens the structural property
the protocol exists to enforce.

Mismatch is folded into `CURSOR_INVALID_SIGNATURE` rather than a new error
code because the agent action is identical to the existing
`CURSOR_INVALID_SIGNATURE` path: drop the cursor, restart from page 1. The
specific root cause (queryHash mismatch vs HMAC mismatch vs payload-schema
mismatch) is communicated through the `message` string, not the error code
constant.

This policy is normative for PRs 3–7 (every cursor-adopting tool).

### Multi-process cursor scope

Cursors are bound to the **specific MCP server process** that issued
them: the HMAC secret is per-process (see "Token shape" above), so a
cursor from process A presented to process B fails HMAC verification
and returns `CURSOR_INVALID_SIGNATURE`. Clients that front multiple
MCP servers against the same vault (e.g. Claude desktop + Claude Code
running side-by-side, or a load-balanced setup described in
`docs/hub-spoke-setup.md`) must route follow-up cursor-bearing calls
to the same process.

This rollout does **not** provide a cross-process cursor sharing
mechanism. A future rollout could derive a vault-level HMAC secret
stored under `.schist/` to allow cross-process validity; the cost is
rotation complexity and a persisted secret on disk. Out of scope here.

### Tool-specific cursor adoption

The table below lists **current** primary `ORDER BY` (verified against
`mcp-server/src/sqlite-reader.ts` on 2026-05-10) plus the **stable
tiebreaker** each implementation PR must add. OFFSET-based pagination
is non-deterministic without a tiebreaker — duplicate primary-sort
values cause skipped or repeated rows across pages. Adding a
secondary `id ASC` (or equivalent) is **not** a breaking change to the
primary sort.

| Tool | `queryHash` includes | Current primary ORDER BY | Required secondary tiebreaker (PR adds) | Next-page SQL shape |
|------|----------------------|---------------------------|------------------------------------------|----------------------|
| `search_notes` | `query`, `limit`, `tags?`, `status?`, `scope?`, owner | **None today** (line 111). FTS5 natural relevance order. | `ORDER BY bm25(docs_fts), docs.id ASC` (FTS5 rank surfaced via `bm25(docs_fts)` for stability). | Existing SQL + new `ORDER BY` clause + `LIMIT :limit OFFSET :offset`. |
| `search_memory` (FTS path) | `query?`, `owner?`, `entry_type?`, `date_from?`, `date_to?`, `limit`, owner | **None today** (line 429). FTS5 natural relevance order. | `ORDER BY bm25(agent_memory_fts), m.id ASC`. | Existing FTS SQL + new `ORDER BY` + `LIMIT :limit OFFSET :offset`. |
| `search_memory` (non-FTS path) | (same as FTS path) | `ORDER BY created_at DESC` (line 439). | `ORDER BY created_at DESC, id ASC`. | Existing SQL + `, id ASC` tiebreaker + `LIMIT :limit OFFSET :offset`. |
| `query_graph` | normalized SQL string, `params?`, owner | Whatever caller's SQL specifies. | Server wraps as subquery — see "query_graph cursor wrapping" below. | `SELECT * FROM (<caller_sql>) AS user_query LIMIT :limit OFFSET :offset`. |
| `list_concepts` | `tags?`, `search?`, `limit`, owner | `ORDER BY edgeCount DESC` (line 204 in sqlite-reader.ts). | `ORDER BY edgeCount DESC, c.slug ASC`. | Existing SQL + `, slug ASC` tiebreaker + `LIMIT :limit OFFSET :offset`. |
| `list_domains` | `limit`, owner | `ORDER BY parent_slug NULLS FIRST, slug` (line 565 in sqlite-reader.ts). | (Already deterministic — `slug` is unique. No change needed.) | Existing SQL + `LIMIT :limit OFFSET :offset`. |

`get_note` does NOT use cursors — single-doc fetch by ID.
`get_context` does NOT use cursors — fixed-shape summary, not a list.

#### Concurrent-ingest limitation (OFFSET pagination)

All vault-DB-backed cursor tools (`search_notes`, `query_graph`,
`list_concepts`, `list_domains`) paginate via SQL `OFFSET` over tables
that schist's post-commit hook **drops and rebuilds** on every commit
(per `CLAUDE.md` — `docs`/`docs_fts`/`concepts`/`edges`/`domains`).
The memory-DB tools (`search_memory`) are unaffected — `agent_memory`
is never touched by ingest.

If a commit lands between page N and page N+1 of a cursor pagination,
the rebuilt table's row ordering can shift relative to the cursor's
encoded `offset`:

- **Skip**: a new doc whose `bm25` rank places it before the previous
  page boundary pushes later rows past the offset; the agent never
  sees them on page N+1.
- **Duplicate**: a delete (via `git rm` + commit) ahead of the offset
  pulls page N+1 rows back into positions already returned on page N.

Mitigations baked into the protocol:

- Cursor TTL is 300 s, bounding the corruption window.
- Single-agent pagination typically completes in seconds.
- The `bm25(...) + id ASC` tiebreaker keeps ordering deterministic
  *within* a single ingest snapshot — the issue is strictly cross-snapshot.

Not mitigated in this rollout:

- A keyset cursor (`id > last_id` instead of `OFFSET n`) would be
  rebuild-stable for tools whose primary ORDER BY is `id`-monotonic.
  It does NOT trivially compose with `bm25` ranking — page boundaries
  computed on stale bm25 scores would still misorder against new rows.
  Tracked as a follow-up issue, out of scope for the current rollout.

Callers should treat cursor pagination as snapshot-at-best-effort —
adequate for reading the current state of the vault, not for building
total-ordered exports across long pagination sessions. Long-pagination
exporters should run `query_graph` with a caller-specified LIMIT large
enough to one-shot the result, or use the CLI / direct SQLite path.

This limitation is shared by every vault-DB cursor tool — call it out
once here so future PRs (5–7) can reference this section without
repeating the rationale.

#### `query_graph` cursor wrapping

To make pagination implementable safely over arbitrary user SQL — no
regex-on-raw-SQL pitfalls (comment fake-outs, string literals, CTEs,
UNIONs, multi-statement) — the server **wraps** the caller's SQL as a
subquery:

```sql
SELECT * FROM (<caller_sql>) AS user_query LIMIT :limit OFFSET :offset
```

- `:limit` is server-controlled: the caller's `limit` arg (capped at
  1000) or the default 100. The cursor's `offset` field rides on this
  outer LIMIT.
- The caller's own `LIMIT` / `OFFSET` / `ORDER BY` clauses live inside
  the subquery and are respected verbatim. A caller passing `SELECT *
  FROM docs LIMIT 5` gets exactly 5 rows (no pagination needed since
  `5 < limit`). A caller passing `SELECT * FROM docs ORDER BY date DESC`
  gets server-paginated results in date-descending order.
- The existing `query_graph` guards (SELECT/WITH-only, no
  INSERT/UPDATE/etc., enforced via `sqlite-reader.ts:208-211`) still
  reject mutating statements before wrapping.
- Caller's SQL with a trailing `;` or multi-statement input is
  rejected by `better-sqlite3.prepare()` as it is today — no change.

This approach removes the spec's prior "detect caller LIMIT and cap"
requirement entirely. The subquery wrap is a single deterministic
rewrite; no parser, no regex, no edge cases.

---

## Reason-string verbose

Tools that have a "give me more" mode replace `verbose: boolean` with
`verbose: string`.

**Validation rules (locked):**

- The string must be **≥ 12 Unicode code points** after trimming
  whitespace. Measured via `[...str.trim()].length`, **not**
  `str.length` (which counts UTF-16 code units — `"🔍🔍🔍🔍🔍🔍"` is 12
  UTF-16 units but only 6 code points). Code points are the right unit:
  graphemes are over-permissive (zero-width-joiners) and bytes are
  over-restrictive (CJK reasons would need to be artificially padded).
- "Whitespace" means anything matching `/^\s*$/u`. This catches NBSP
  (` `), zero-width space (`​`), BOM (`﻿`), and any
  other Unicode whitespace class.
- `verbose: true` (boolean) is rejected as a type error — callers
  can't accidentally re-introduce the boolean pattern.
- Empty string, whitespace-only string, or omitted field → not
  verbose. No silent fallback.

**Logging (locked):**

The accepted reason is logged to the MCP server's stderr at INFO level:

```
[verbose] <tool> by <owner|anonymous>: <JSON.stringify(reason)>
```

The reason is passed through `JSON.stringify` (which escapes newlines,
control chars, and any non-printable bytes) before writing to stderr.
Logging `reason` raw is a log-injection vector: an agent-controlled
string like `"benign\n[error] root pwned"` would inject a fake stderr
line. `JSON.stringify` is sufficient escape — it produces a quoted,
single-line string in all cases. The same escape applies to `owner`
if it's caller-controlled.

**Rate-limit note (PR 2 + PR 3 + PR 7):**

A determined agent will discover the magic 12-code-point pad and pass
the same reason on every call, turning the audit log into noise. PR 2
adds a counter per `(tool, owner, sha256(reason))` over the last 60
seconds; if a single triple exceeds 30 hits/min, subsequent calls
within that window get a soft warning in the response (`verboseNote:
"reason pattern is frequent — consider sampling at operator level"`)
but are not refused. Hard rate-limiting is out of scope for this
rollout.

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
| `search_notes` | `limit: 20` | `limit: 20` (unchanged) | 100 | Already healthy at the 2026-05-10 baseline (8 KB / ~2.4K tokens). |
| `list_concepts` | `limit: 50` | `limit: 50` (unchanged) | 200 | Already healthy (4 KB / ~1K tokens at 25 concepts). |
| `list_domains` | **no limit** | **`limit: 100`** | 500 | Domains tend to be tiny but the unbounded default is a footgun for a vault that grows them. |
| `query_graph` | **no default LIMIT** | **Server-wrapped subquery: outer `LIMIT 100`** | **outer `LIMIT` 1000** via caller `limit` arg | Today `SELECT * FROM docs` is unbounded; baseline showed 242 KB / ~64K tokens on a 70-doc vault. Server wraps as `SELECT * FROM (<caller_sql>) LIMIT :limit OFFSET :offset` so the outer LIMIT is the cap regardless of what's inside. Caller's own inner LIMIT/ORDER BY/OFFSET are respected verbatim. **⚠️ This is a behavior change** — see Compatibility below. |
| `get_context` | tiered (minimal / standard / full) | tiered, `full` requires verbose reason | n/a | Tiering is fine; cost of `full` is the tagCloud computation, gated via reason-string. |
| `search_memory` | `limit: 50`, full content | `limit: 50`, snippet content; full content requires verbose reason | 200 | 42 KB / ~10.6K tokens baseline drops to ~12 KB / ~3K tokens estimated under snippet mode. |

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

- **`query_graph` server-wraps every caller query as a paginated
  subquery.** Today, a caller running `SELECT * FROM docs` on a
  1000-doc vault gets all 1000 rows. Post-PR-4, the same call gets
  100 rows + a cursor; the outer LIMIT (caller's `limit` arg, default
  100, max 1000) caps the result regardless of what the caller's SQL
  itself says. A caller who wants ≥1000 rows must paginate via the
  cursor.

The new cursor error codes (`CURSOR_REQUIRED`, `CURSOR_EXPIRED`,
`CURSOR_INVALID_SIGNATURE`, `CURSOR_WRONG_TOOL`) are *technically* also
new behavior — but they only trigger on agent retry patterns this
protocol exists to surface. The previous behavior was "agent silently
re-queries forever"; the new behavior is "agent gets a labelled error."
That's a strict improvement, not a regression.

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
  intentionally explicit. **Caveat:** `get_note` returns the full body
  with no size cap. A vault containing a multi-megabyte note will
  return the entire body in one tool call — a worse blast radius than
  pre-rollout `query_graph` for that one note. The 2026-05-10 baseline
  didn't measure `get_note` because there's no obvious "worst case"
  note ID to probe. If multi-MB notes become real, a follow-up
  rollout could add a body-length cap with reason-string opt-in for
  full retrieval; that's out of scope for this rollout.

---

## Self-review checklist

Used to validate the spec before opening PR 1, refined after the
2026-05-10 PR-#56 review pass that surfaced 10 ambiguities. Re-validate
this checklist before starting each PR 2–7 plan.

- [x] Every implementation PR (2–8) has a section it can point at:
  - PR 2 → "Cursor protocol" + "queryHash canonicalization" + "Cursor
    error codes" + "Reason-string verbose" + rate-limit note.
  - PR 3 → "search_memory" rows in cursor-adoption table + Default
    limits + Reason-string adopters + verbose-newly-set bypass +
    "Cursor binding to queryHash".
  - PR 4 → "search_notes" rows + tiebreaker requirement. **Reordered**
    from the original "PR 4 = query_graph" plan: `search_notes` is
    structurally `search_memory`'s twin (no breaking change, no
    verbose, no subquery wrap) and lands as the second cursor consumer
    before the breaking change in PR 5. Audit:
    `audit-2026-05-17-mcp-response-sizes-pr4.md`.
  - PR 5 → "query_graph" rows + "query_graph cursor wrapping" subsection
    + Default limits + Compatibility breaking change. (Swapped with PR 4
    above — same scope, different integer.) Audit:
    `audit-2026-05-17-mcp-response-sizes-pr5.md`.
  - PR 6 → "list_concepts" + "list_domains" rows + Default limits. (spec row corrected: list_concepts inputs are tags?/search? not q?/domain?)
  - PR 7 → "get_context" reason-string adopter (soft-downgrade for missing/whitespace verbose; INVALID_ARG for type/length misuse). Audit: `audit-2026-05-22-mcp-response-sizes-pr7.md`.
  - PR 8 → "Migration steps" section.
- [x] Cursor token shape specified concretely (unpadded base64url, JWT-like
  dot separator, per-process HMAC) for PR 2 to implement from spec alone.
- [x] `queryHash` canonicalization addresses NaN/±Infinity/BigInt
  rejection, prototype-pollution defense, NFC Unicode normalization,
  disjoint `args`/`owner` namespacing.
- [x] Reason-string rules unambiguous: ≥12 Unicode code points
  (`[...str].length`, not `str.length`), `JSON.stringify`-escaped on
  stderr log, boolean rejected, whitespace via `/^\s*$/u`.
- [x] Cursor error codes split into `CURSOR_REQUIRED` /
  `CURSOR_EXPIRED` / `CURSOR_INVALID_SIGNATURE` / `CURSOR_WRONG_TOOL`
  with distinct agent actions.
- [x] Cursor binding to queryHash: current call's computed queryHash must
  equal cursor's encoded queryHash; mismatch returns `CURSOR_INVALID_SIGNATURE`
  with explanatory message.
- [x] No cursor reissue on `CURSOR_REQUIRED` — agent must echo the
  original cursor or refine the query.
- [x] LRU refusal labelled as best-effort (not a security boundary);
  HMAC TTL is the binding constraint.
- [x] Multi-process cursor scope addressed (per-process secret;
  future cross-process derivation out of scope).
- [x] `verbose`-newly-set bypasses identical-query refusal so agents
  can upgrade snippet → full content without a refusal loop.
- [x] `query_graph` cursor wrapping uses subquery rewrite, not regex
  on caller SQL — no comment/CTE/UNION edge cases.
- [x] Tool-specific ORDER BY rows match current code as of 2026-05-10
  (verified against `mcp-server/src/sqlite-reader.ts`); each cursor
  PR adds an explicit `id ASC` tiebreaker.
- [x] Compatibility section names the breaking change
  (`query_graph` subquery wrap) and proposes mitigation in PR 8.
- [x] Out-of-scope acknowledges `get_note`'s unbounded body return.
- [x] No `TBD` / `<fill>` placeholders left in this document.
