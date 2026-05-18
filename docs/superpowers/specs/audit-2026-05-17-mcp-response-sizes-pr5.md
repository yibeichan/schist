# MCP Tool Response Size Audit — 2026-05-17 (PR 5: query_graph adoption)

**Vault audited:** `~/schist-vault` (mac spoke)
**Memory DB:** `~/.openclaw/memory/agent-state.db` (via `SCHIST_MEMORY_DB`)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:**
```bash
cd /Users/yibeichen/github/schist && \
  SCHIST_MEMORY_DB="$HOME/.openclaw/memory/agent-state.db" \
  npm run audit --prefix mcp-server -- \
    --vault "$HOME/schist-vault" \
    --search-query "schist"
```

PR 5 post-change measurement. Compare against the 2026-05-17 PR 4
snapshot in `audit-2026-05-17-mcp-response-sizes-pr4.md`.

---

## 2026-05-17 — after PR 5 (query_graph adoption)

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="schist") | 7,723 | 2,301 | 20 | Unchanged from PR 4. |
| `list_concepts` (no opts) | 8,416 | 2,141 | 50 | Unchanged — PR 6 territory. |
| `list_domains` (no opts) | 2 | 1 | 0 | Unchanged. |
| `query_graph` (`SELECT * FROM docs`) | **248,862** | **64,590** | **100** | **Default outer LIMIT 100 capped the response. Down from 498,765 B / 128,994 tokens at the PR 4 snapshot.** |
| `get_context` (minimal) | 51 | 16 | 1 | Lower than PR 4 (186) — vault corpus changed (slimmer min payload). |
| `get_context` (standard) | 1,919 | 623 | 1 | Same. |
| `get_context` (full) | 2,842 | 898 | 1 | Same. |
| `search_memory` (limit=50) | 14 | 4 | 0 | Local mac memory DB empty; figure not comparable to HPC-spoke runs. |

Tokens counted via `gpt-tokenizer` (`o200k_base`, GPT-4o BPE). See the
2026-05-10 baseline doc for proxy accuracy notes.

---

## query_graph delta analysis

| Metric | PR 4 (2026-05-17) | PR 5 (2026-05-17, this audit) | Delta |
|--------|-------------------|-------------------------------|-------|
| Bytes  | 498,765 | 248,862 | **−50.1%** |
| Tokens | 128,994 | 64,590 | **−49.9%** |
| Rows returned | All (173 in vault) | 100 (capped) | Bounded by default outer LIMIT |
| Response shape | `{columns, rows, rowCount}` | `{columns, rows, rowCount, cursor?}` | Cursor field adds ~220 B when paged |

This is the rollout's headline win. `query_graph` on `SELECT * FROM docs`
was the single largest context-burn tool by a wide margin at every
baseline since 2026-05-10:

- 2026-05-10 baseline (70 docs): 242 KB / 64 K tokens — already 6× the
  next worst tool.
- 2026-05-14 (PR 3 audit, 100 docs): 378 KB / 100 K tokens — scaled
  linearly with corpus.
- 2026-05-17 (PR 4 audit, 173 docs): 499 KB / 129 K tokens — same scaling.
- 2026-05-17 (PR 5 audit, 173 docs): **249 KB / 65 K tokens** — bounded.

At today's 173-doc vault size, this is a ~50% reduction. The reduction
*grows* with vault size: at 1000 docs the un-capped response would be
~2.9 MB / 750 K tokens; PR 5 still caps at 249 KB / 65 K tokens.

The cap is configurable per-call via `limit` (max 1000), so callers
that legitimately want larger one-shot results can request them. The
default protects unaware callers.

---

## Spec coverage walkthrough (PR 5)

Every PR 5 row in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
traced to its landing implementation. Verified via `grep` against
`mcp-server/src/` at HEAD `feat/query-graph-cursor`.

| Spec requirement | File | Evidence |
|-----------------|------|---------|
| Subquery wrap `SELECT * FROM (<caller_sql>) AS user_query LIMIT ? OFFSET ?` | `mcp-server/src/sqlite-reader.ts` (queryGraph) | `const wrappedSql = `SELECT * FROM (${trimmed}) AS user_query LIMIT ? OFFSET ?`;` |
| Trailing-semicolon strip before wrap | `mcp-server/src/sqlite-reader.ts` (queryGraph) | `const trimmed = sql.trim().replace(/;+\s*$/, "");` |
| Default limit 100, hard cap 1000 | `mcp-server/src/tools.ts` (query_graph) | `Math.min(requested, 1000)` with default `100` |
| Existing SELECT/WITH-only + mutation-keyword guards preserved | `mcp-server/src/sqlite-reader.ts` (queryGraph) | `match(/^(SELECT\|WITH)\b/i)` + mutation-keyword reject — unchanged |
| `verbose` excluded (per-spec — query_graph has no verbose mode) | `mcp-server/src/tools.ts` (query_graph) | No `parseVerbose` call; handler always passes `verboseEnabled: false` |
| Cursor binding to `queryHash` | `mcp-server/src/tools.ts` (query_graph) | `"cursor was issued for a different query — restart pagination from page 1"` |
| Tool-registry: cursor + limit inputs + breaking-change description | `mcp-server/src/tool-registry.ts` (query_graph entry) | `cursor: { type: "string", ... }` + `limit: { type: "number", description: "...default 100, capped at 1000..." }` |
| Concurrent-ingest caveat | `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md` (Concurrent-ingest limitation subsection, landed in PR 4) | Spec subsection lands in PR 4; PR 5 references rather than duplicates. |

All 8 spec rows confirmed present. No gaps found.

---

## Reproduction notes

- **Vault corpus at audit time:** 173 docs / 79 concepts / 133 edges per
  `schist doctor`. Same vault as PR 4 audit run earlier today.
- **Memory DB at audit time:** local mac memory DB is empty; the
  search_memory figure is not comparable to HPC-spoke audits.
- **Branch HEAD:** `feat/query-graph-cursor`.
- **Tokenizer:** `gpt-tokenizer` (`o200k_base`), same as baseline.
- **Node version:** v25.9.0.
- **Platform:** macOS (Darwin 25.4.0).
- **Search query:** `"schist"` (same as PR 3/4 audits).
