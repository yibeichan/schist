# MCP Tool Response Size Audit — 2026-05-20 (PR 6: list_concepts + list_domains cursor adoption)

**Vault audited:** `~/schist-vault` (HPC spoke)
**Memory DB:** `~/.openclaw/memory/agent-state.db` (via `SCHIST_MEMORY_DB`)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:**
```bash
cd /orcd/home/002/yibei/schist && \
  SCHIST_MEMORY_DB="$HOME/.openclaw/memory/agent-state.db" \
  npm run audit --prefix mcp-server -- \
    --vault "$HOME/schist-vault" \
    --search-query "schist"
```

PR 6 post-change measurement. Compare against the 2026-05-17 PR 5
snapshot in `audit-2026-05-17-mcp-response-sizes-pr5.md`.

---

## 2026-05-20 — after PR 6 (list_concepts + list_domains cursor adoption)

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="schist") | 7,578 | 2,254 | 20 | Unchanged from PR 5 (corpus churn). |
| `list_concepts` (no opts) | **5,521** | **1,374** | **39** | **Wrapped as `{concepts, cursor?}` (PR 6). Capped at default `limit: 50`; vault has 39 concepts so no cursor.** |
| `list_domains` (no opts) | 14 | 4 | 0 | **Wrapped as `{domains, cursor?}` (PR 6). Vault.yaml has no `domains:` block — empty array is normal.** |
| `query_graph` (`SELECT * FROM docs`) | 313,341 | 82,854 | 100 | Corpus grown since PR 5 (173→~218 docs); cap at 100 still holds. |
| `get_context` (minimal) | 50 | 16 | 1 | Healthy. |
| `get_context` (standard) | 1,913 | 623 | 1 | Healthy. |
| `get_context` (full) | 2,840 | 907 | 1 | Healthy. |
| `search_memory` (limit=50) | 23,914 | 6,826 | 50 | HPC memory DB has 50+ entries; cap hit. |

Tokens counted via `gpt-tokenizer` (`o200k_base`, GPT-4o BPE). See the
2026-05-10 baseline doc for proxy accuracy notes.

---

## list_concepts delta analysis

| Metric | PR 5 (2026-05-17) | PR 6 (2026-05-20) | Notes |
|--------|-------------------|-------------------|-------|
| Bytes  | 8,416 | 5,521 | Corpus shrank from 50→39 concepts between runs — byte drop is corpus-driven. |
| Tokens | 2,141 | 1,374 | Same — corpus-driven. |
| Entries | 50 | 39 | Cap at 50 no longer hit (vault has exactly 39 concepts). |
| Response shape | `Concept[]` (bare array) | `{ concepts: Concept[], cursor? }` (wrapped) | **Breaking change.** Callers must destructure. |

The response shape change is the load-bearing PR 6 delivery for `list_concepts`.
The byte delta between audits is dominated by corpus churn, not the wrapper overhead.
The wrapper adds a small constant cost (≈ 14 B for `{"concepts":…}`) but unlocks
pagination — at 1000 concepts, the un-capped bare-array response would be ~217 KB;
PR 6 caps it at ≈ 11 KB by default (limit 50).

---

## list_domains delta analysis

| Metric | PR 5 (2026-05-17) | PR 6 (2026-05-20) | Notes |
|--------|-------------------|-------------------|-------|
| Bytes  | 2 | 14 | Empty bare `[]` → empty `{"domains":[]}` wrapper. |
| Tokens | 1 | 4 | Same. |
| Entries | 0 | 0 | Vault.yaml has no `domains:` block in either run. |
| Response shape | `Domain[]` (bare array) | `{ domains: Domain[], cursor? }` (wrapped) | **Breaking change.** Callers must destructure. |
| Limit behavior | Unbounded | Default `limit: 100`, cap 500 | **Footgun fixed.** Vaults with hundreds of domains were unbounded. |

The vault used for this audit has no domains, so the measurement doesn't
show the pagination benefit directly. On a vault with 300 domains, the
un-capped bare array would return all 300; PR 6 caps at 100 by default
with cursor-based pagination for the rest.

---

## Spec coverage walkthrough (PR 6)

Every PR 6 row in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
traced to its landing implementation. Verified via `grep` against
`mcp-server/src/` at HEAD `feat/issue-50-list-tools-cursor`.

| Spec requirement | File | Evidence |
|-----------------|------|---------|
| `listConcepts` gains `limit`, `offset` args with stable tiebreaker `ORDER BY slug ASC` | `mcp-server/src/sqlite-reader.ts` | `ORDER BY slug ASC` in `listConcepts`; `offset` param threaded through |
| `listDomains` gains `limit`, `offset` args | `mcp-server/src/sqlite-reader.ts` | `listDomains` accepts `{ limit?, offset? }` |
| `ListConceptsResponse` / `ListDomainsResponse` types added | `mcp-server/src/types.ts` | `export type ListConceptsResponse` + `export type ListDomainsResponse` |
| `list_concepts` handler wraps return as `{ concepts, cursor? }` | `mcp-server/src/tools.ts` (list_concepts) | `const response: ListConceptsResponse = { concepts };` |
| `list_domains` handler wraps return as `{ domains, cursor? }` | `mcp-server/src/tools.ts` (list_domains) | `const response: ListDomainsResponse = { domains };` |
| Default limit 50 / cap 200 for `list_concepts` | `mcp-server/src/tools.ts` (list_concepts) | `Math.min(requested, 200)` with default `50` |
| Default limit 100 / cap 500 for `list_domains` | `mcp-server/src/tools.ts` (list_domains) | `Math.min(requested, 500)` with default `100` |
| Tool-registry schemas gain `cursor` + `limit` inputs for both tools | `mcp-server/src/tool-registry.ts` | `cursor: { type: "string", … }` + `limit: { type: "number", … }` in both entries |
| `{ concepts }` shape recognized by audit script | `scripts/audit_mcp_response_sizes.ts` | `else if (Array.isArray(obj.concepts))` branch |
| `{ domains }` shape recognized by audit script | `scripts/audit_mcp_response_sizes.ts` | `else if (Array.isArray(obj.domains))` branch |
| Audit-script test probes both wrapped shapes | `mcp-server/tests/audit-script.test.ts` | `isListConceptsShape` + `isListDomainsShape` probes in regression test |

All 11 spec rows confirmed present. No gaps found.

---

## Reproduction notes

- **Vault corpus at audit time:** HPC spoke at `~/schist-vault`, 39 concepts,
  `search_notes` returns 20 results for query "schist".
- **Memory DB at audit time:** `~/.openclaw/memory/agent-state.db` on HPC
  (50 entries at cap).
- **Branch HEAD:** `feat/issue-50-list-tools-cursor`.
- **Tokenizer:** `gpt-tokenizer` (`o200k_base`), same as baseline.
- **Node version:** v22.16.0 (HPC nvm default).
- **Platform:** Linux (HPC, ORCD).
- **Search query:** `"schist"` (same as PR 3/4/5 audits).
