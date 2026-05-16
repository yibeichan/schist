# MCP Tool Response Size Audit — 2026-05-14 (PR 3: search_memory adoption)

**Vault audited:** `~/schist-vault` (HPC spoke, post-flatten)
**Memory DB:** `~/.openclaw/memory/agent-state.db` (via `SCHIST_MEMORY_DB`)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:**
```bash
cd /orcd/home/002/yibei/schist && \
  SCHIST_MEMORY_DB="$HOME/.openclaw/memory/agent-state.db" \
  LD_LIBRARY_PATH=/orcd/software/core/001/spack/pkg/gcc/12.2.0/yt6vabm/lib64:$LD_LIBRARY_PATH \
  NODE_PATH=./mcp-server/node_modules \
  ./mcp-server/node_modules/.bin/tsx scripts/audit_mcp_response_sizes.ts \
    --vault "$HOME/schist-vault" \
    --search-query "schist"
```

This is the PR 3 post-change measurement. Compare against the
2026-05-10 baseline in `audit-2026-05-04-mcp-response-sizes.md`.

---

## 2026-05-14 — after PR 3 (search_memory adoption)

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="schist") | 8,317 | 2,319 | 20 | FTS5 snippet, default `limit: 20` (cap hit) |
| `list_concepts` (no opts) | 5,508 | 1,370 | 39 | Corpus grown to 39 concepts (was 25) |
| `list_domains` (no opts) | 2 | 1 | 0 | Unchanged — vault.yaml still has no `domains:` block |
| `query_graph` (`SELECT * FROM docs`) | 378,054 | 99,883 | 1 | Corpus grown to ~100 docs (was 70); no limit change in PR 3 |
| `get_context` (minimal) | 185 | 56 | 1 | Healthy |
| `get_context` (standard) | 1,913 | 623 | 1 | Healthy |
| `get_context` (full) | 2,840 | 907 | 1 | Healthy |
| `search_memory` (limit=50) | **23,529** | **6,728** | 50 | **Snippet mode (PR 3). Down from 41,755 B / 10,620 tokens at baseline.** |

Tokens counted via `gpt-tokenizer` (`o200k_base`, GPT-4o BPE). See baseline
doc for proxy accuracy notes.

---

## search_memory delta analysis

| Metric | Baseline (2026-05-10) | PR 3 (2026-05-14) | Delta |
|--------|-----------------------|-------------------|-------|
| Bytes | 41,755 | 23,529 | **−43.6%** |
| Tokens | 10,620 | 6,728 | **−36.6%** |
| Entries | 50 | 50 | 0 |
| Bytes/entry | ~835 | ~471 | **−44%** |

The snippet path was exercised: `entryCount = 50` confirms the limit
was hit and entries were returned. The 44% per-entry byte drop reflects
`content` being replaced by a `~200-cp` snippet across all 50 entries.
The memory DB at audit time contained **267 entries** with an average
raw content length of **607 bytes** and max of **1,747 bytes**.

The spec target was "~12 KB / ~3K tokens estimated under snippet mode"
(`audit-2026-05-04-mcp-response-sizes.md`, per-tool observations). The
actual result (23.5 KB / 6.7K tokens) is above that estimate because:
1. The snippet field is `~200 cp` but the JSON envelope (id, slug,
   entry_type, tags, created_at, snippet, nextCursor, etc.) adds
   fixed overhead per entry that the estimate didn't account for.
2. Entry count in this vault grew (267 vs. ≥50 at baseline) — the
   richer metadata on newer entries adds per-row overhead.

Even so, the absolute reduction is **18.2 KB / 3.9K tokens** — roughly
equivalent to freeing one page of dense English text per call.

---

## Spec coverage walkthrough (PR 3)

Every PR 3 row in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
traced to its landing implementation below. Verified via `grep` against
`mcp-server/src/` at HEAD `feat/issue-50-search-memory-adoption`.

| Spec requirement | File | Evidence |
|-----------------|------|---------|
| FTS path: `ORDER BY bm25(agent_memory_fts), m.id ASC` | `mcp-server/src/sqlite-reader.ts:432` | `sql += " ORDER BY bm25(agent_memory_fts), m.id ASC LIMIT ? OFFSET ?"` |
| Non-FTS path: `ORDER BY created_at DESC, id ASC` | `mcp-server/src/sqlite-reader.ts:443` | `sql += " ORDER BY created_at DESC, id ASC LIMIT ? OFFSET ?"` |
| Default limit 50, hard cap 200 | `mcp-server/src/tools.ts:597` | `Math.min(requested, 200)` |
| `parseVerbose` adoption in handler | `mcp-server/src/tools.ts:544` | `const v = parseVerbose(args.verbose)` |
| Verbose-newly-set bypass via `checkRefusal` | `mcp-server/src/tools.ts:579` | `const refusal = checkRefusal({...})` |
| Cursor binding to `queryHash` | `mcp-server/src/tools.ts:567` | `"cursor was issued for a different query — restart pagination from page 1"` |
| Issue #60 fix: `ZERO_WIDTH_FORMATTERS` strip in verbose | `mcp-server/src/protocol/verbose.ts:15,66` | `const ZERO_WIDTH_FORMATTERS = /[​‌‍⁠﻿]/gu` |

All 7 spec rows confirmed present. No gaps found.

---

## Reproduction notes

- **Vault corpus at audit time:** docs, concepts, and edges grew since the
  baseline (vault is a live spoke). The HPC spoke at `~/schist-vault` is
  synced periodically from the hub.
- **Memory DB at audit time:** 267 entries. `limit: 50` was hit (entryCount=50).
  `SCHIST_MEMORY_DB` env var was required to override the default DB path.
- **Branch HEAD:** `feat/issue-50-search-memory-adoption` (Task 3.10 commit).
- **Tokenizer:** `gpt-tokenizer` (`o200k_base`), same as baseline.
- **Node version:** v22.16.0.
- **Platform:** RHEL 8 ORCD HPC node. Same `LD_LIBRARY_PATH` override
  required as baseline (`GLIBCXX_3.4.29` needed, system ships only through
  `GLIBCXX_3.4.25`). `NODE_PATH=./mcp-server/node_modules` required to
  resolve `gpt-tokenizer` when running the script from the repo root.
- **Search query:** `"schist"` (hits notes in this vault; replaces
  `"session"` from baseline).

## Known limitation: verbose-mode control not measured

The audit script measures only the default (snippet) path for
`search_memory`. A verbose-mode control (full content) was not added to
the script in PR 3 — that would require a `verbose: "<reason>"` argument
wired through `runAudit`. Adding a `search_memory_verbose` row to
`runAudit` is left as a future audit-script enhancement (see Issue #57
or the PR 4 audit). The spec comparison (44% byte drop) is unambiguous
from the snippet vs. baseline numbers alone.
