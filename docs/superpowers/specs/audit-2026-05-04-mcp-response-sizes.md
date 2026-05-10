# MCP Tool Response Size Audit — 2026-05-04 Baseline

**Vault audited:** `~/schist-vault` (HPC spoke, post-flatten)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:** `cd mcp-server && npm run audit -- --vault <path> [--search-query <q>]`

This file is the canonical historical record of MCP tool response sizes.
**Append new dated tables, do not overwrite.** Each implementation PR (3–7
in the rollout plan) re-runs the audit and appends its post-change numbers
so the rollout's effect is visible in one diff.

## 2026-05-10 — pre-rollout baseline

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="session") | 8,346 | 2,357 | 20 | FTS5 snippet, default `limit: 20` (cap hit) |
| `list_concepts` (no opts) | 3,946 | 965 | 25 | Default `limit: 50`; corpus has 25 concepts so all fit |
| `list_domains` (no opts) | 2 | 1 | 0 | **No limit.** Returned `[]` — vault.yaml has no `domains:` block |
| `query_graph` (`SELECT * FROM docs`) | **241,756** | **64,284** | 1 (array of rows) | **No default LIMIT.** 70-doc vault → ~245KB blob; ≈64K tokens |
| `get_context` (minimal) | 49 | 16 | 1 | Counts only |
| `get_context` (standard) | 1,864 | 596 | 1 | + `recent` + `hotConcepts` |
| `get_context` (full) | 2,788 | 879 | 1 | + `tagCloud` (top 30 tags) |
| `search_memory` (limit=50) | **41,755** | **10,620** | 50 | **Returns full `content` field**, ~835 B/entry on this corpus |

Tokens are counted via `gpt-tokenizer` (`o200k_base`, GPT-4o's BPE).
Anthropic's tokenizer isn't available as a local library; `o200k_base`
is a close proxy — typically within ±10% of Claude's count for English /
JSON text. The audit's purpose is relative sizing across tools, where
this proxy is more than accurate enough.

## Per-tool observations

- **`query_graph` is the worst-case footgun.** A 70-doc vault produces a
  ~245KB response (~64K tokens — about 32% of an Opus 200K context
  window in one tool call). At 700 docs the same query would clear
  ~2.4MB / ~640K tokens, exceeding any current LLM context. PR 4 must
  default-LIMIT this to 100 and cap caller `LIMIT` at 1000.

- **`search_memory` is the second-highest target.** 50 entries × ~835 B
  = 42KB / ~10.6K tokens per call. The full `content` field is the
  bulk — most query patterns only need a snippet to triage relevance.
  PR 3 switches default to a snippet (~200 chars) and gates full
  content behind a `verbose: "<reason>"` opt-in.

- **`search_notes` is already in good shape** at the corpus level
  (`limit: 20` + FTS5 snippets keep one call to ~8KB / ~2.4K tokens).
  PR 5's job is cursor pagination + identical-query refusal; no shape
  change. PR 5 must also add a stable ORDER BY (today the query has none —
  FTS5 rank ordering isn't deterministic enough for OFFSET pagination).

- **`list_concepts` is fine** at 4KB / ~1K tokens for 25 entries. The
  50-cap is comfortable headroom; cursor support arrives in PR 6 for
  vaults that grow past 50 concepts. Current ORDER BY is `edgeCount DESC`
  (not `slug ASC`); the spec's cursor-adoption table records that.

- **`list_domains` returned an empty array** (null observation). The
  2-byte response (`[]`) is real data, not an error envelope —
  `~/schist-vault/vault.yaml` simply has no top-level `domains:` block.
  PR 6 still adds a default `limit: 100` so the unbounded-by-default
  case can't bite a vault that grows domains later.

- **`get_context` is healthy across all depths.** `minimal` (49 B) is
  the obvious agent-default; `standard` (1.9KB) is reasonable; `full`
  (2.8KB) adds tagCloud. PR 7 gates `full` behind a reason string —
  the cost isn't the byte size, it's the tagCloud computation itself,
  which we don't want defaulted on.

## Reproduction notes

- **Vault corpus at audit time:** `docs: 70 · concepts: 25 · edges: 21`
  (from `SELECT (SELECT COUNT(*) FROM docs), (SELECT COUNT(*) FROM concepts), (SELECT COUNT(*) FROM edges)` against the live `.schist/schist.db`).
- **Memory DB at audit time:** ≥50 entries (default `limit: 50` was hit).
- **Branch HEAD (mcp-server):** baseline finalized at the tokenizer-swap
  commit; `search_memory` bytes will vary slightly across sessions if
  memory entries are written between runs (this audit's 41,755 B
  reflects four entries added on 2026-05-10 by the session that
  produced this doc).
- **Tokenizer:** `gpt-tokenizer` 3.4.0 (`o200k_base`), pure-JS, pinned
  in `mcp-server/devDependencies`.
- **Node version:** v22.16.0.
- **Platform:** RHEL 8 ORCD HPC node. Required env override:
  `LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH`
  to load `better-sqlite3`'s prebuilt binary (system libstdc++ only ships
  through `GLIBCXX_3.4.25`; binding needs `GLIBCXX_3.4.29`). CI on stock
  Linux x86_64 doesn't need this.
- **Search query for `search_notes`:** `"session"` — chosen because it
  hits ≥20 notes in this vault and exercises the FTS5 cap.

## Re-audit checklist (for PRs 3–7)

After each implementation PR, append a dated section to this file:

```
## YYYY-MM-DD — after PR <N> (<scope>)
```

Re-run the same command (same vault, same `--search-query`) and tabulate
the deltas next to the 2026-05-10 baseline. Highlight any tool that grew
unexpectedly — efficiency PRs should never make response sizes worse.
