# MCP Tool Response Size Audit — 2026-05-17 (PR 4: search_notes adoption)

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

PR 4 post-change measurement. Compare against the 2026-05-14 PR 3
snapshot in `audit-2026-05-14-mcp-response-sizes-pr3.md`.

---

## 2026-05-17 — after PR 4 (search_notes adoption)

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="schist") | **7,723** | **2,301** | 20 | **Wrapped as `{results, cursor}` (PR 4). Cursor present (cap hit at default `limit: 20`).** |
| `list_concepts` (no opts) | 8,416 | 2,141 | 50 | Corpus grown to 50+ concepts (PR 3 saw 39). |
| `list_domains` (no opts) | 2 | 1 | 0 | Unchanged — vault.yaml still has no `domains:` block. |
| `query_graph` (`SELECT * FROM docs`) | 498,765 | 128,994 | 1 | Corpus grown again; query_graph is still unbounded — that's PR 5. |
| `get_context` (minimal) | 186 | 56 | 1 | Healthy. |
| `get_context` (standard) | 1,919 | 623 | 1 | Healthy. |
| `get_context` (full) | 2,842 | 898 | 1 | Healthy. |
| `search_memory` (limit=50) | 14 | 4 | 0 | Local mac memory DB is empty (HPC spoke holds the 267-entry corpus); the figure isn't comparable to the PR 3 baseline. |

Tokens counted via `gpt-tokenizer` (`o200k_base`, GPT-4o BPE). See the
2026-05-10 baseline doc for proxy accuracy notes.

---

## search_notes delta analysis

| Metric | PR 3 (2026-05-14) | PR 4 (2026-05-17) | Notes |
|--------|-------------------|-------------------|-------|
| Bytes  | 8,317 | 7,723 | Slight drop driven by corpus churn — see below. |
| Tokens | 2,319 | 2,301 | ~ equivalent. |
| Entries | 20 | 20 | Default `limit: 20` cap hit on both runs. |
| Response shape | `SearchResult[]` (bare array) | `{ results: SearchResult[], cursor }` (wrapped) | The cursor field carries one signed token (~200 B) — included in the byte count above. |

The PR 4 byte count includes the new wrapper + cursor token (~+220 B
combined), so the underlying per-row payload actually shrank by ~810 B
between the two snapshots. That delta reflects vault content churn
(notes added/removed/edited between 2026-05-14 and 2026-05-17), not the
cursor change itself.

**Primary PR 4 win is correctness, not byte savings.** `search_notes` was
already healthy at baseline (~8 KB / ~2.3 K tokens). The point of this
PR is deterministic OFFSET pagination via the bm25/id-ASC tiebreaker,
queryHash-bound cursors, and identical-query refusal — properties that
the bare-array return couldn't express.

---

## Spec coverage walkthrough (PR 4)

Every PR 4 row in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
traced to its landing implementation. Verified via `grep` against
`mcp-server/src/` at HEAD `feat/search-notes-cursor`.

| Spec requirement | File | Evidence |
|-----------------|------|---------|
| Stable tiebreaker `ORDER BY bm25(docs_fts), docs.id ASC` | `mcp-server/src/sqlite-reader.ts` (searchNotes) | `orderClauses.push("bm25(docs_fts)"); orderClauses.push("docs.id ASC");` |
| scope=inherit CASE preserved as primary ORDER BY layer | `mcp-server/src/sqlite-reader.ts` (searchNotes) | `orderClauses.push("CASE WHEN docs.scope = ? THEN 0 ELSE 1 END")` runs before bm25 row |
| Default limit 20, hard cap 100 | `mcp-server/src/tools.ts` (search_notes) | `Math.min(requested, 100)` with default `20` |
| `verbose` excluded (full bodies via `get_note`) | `mcp-server/src/tools.ts` (search_notes) | No `parseVerbose` call; handler always passes `verboseEnabled: false` to `checkRefusal` / `recordIssued` |
| Cursor binding to `queryHash` | `mcp-server/src/tools.ts` (search_notes) | `"cursor was issued for a different query — restart pagination from page 1"` |
| Tool-registry schema gains `cursor` input + paginated description | `mcp-server/src/tool-registry.ts` (search_notes entry) | `cursor: { type: "string", description: "Opaque pagination cursor returned by a prior call…" }` |

All 6 spec rows confirmed present. No gaps found.

---

## Reproduction notes

- **Vault corpus at audit time:** local mac spoke at `~/schist-vault`,
  173 docs / 79 concepts / 133 edges per `schist doctor`.
- **Memory DB at audit time:** local mac memory DB is empty (267-entry
  corpus lives on the HPC spoke). Memory numbers in this report are
  therefore not comparable to PR 3's audit; only the `search_notes`
  row is the load-bearing PR-4 measurement.
- **Branch HEAD:** `feat/search-notes-cursor`.
- **Tokenizer:** `gpt-tokenizer` (`o200k_base`), same as baseline.
- **Node version:** v25.9.0.
- **Platform:** macOS (Darwin 25.4.0).
- **Search query:** `"schist"` (same as PR 3).

## Note on the rollout order

The spec checklist names this PR as "PR 5" (`search_notes` row) because the
original rollout plan listed `query_graph` first. We landed `search_notes`
first because it's structurally `search_memory`'s twin (no breaking change,
no verbose, no subquery wrap) — a cleaner second consumer for the cursor
protocol before introducing the rollout's only breaking change. `query_graph`
becomes the next PR; the breaking-change call-out and release notes remain
on that PR.
