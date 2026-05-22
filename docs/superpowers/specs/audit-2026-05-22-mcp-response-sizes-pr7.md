# MCP Tool Response Size Audit — 2026-05-22 (PR 7: get_context reason-string verbose)

**Vault audited:** `~/schist-vault` (Mac local)
**Memory DB:** `~/.openclaw/memory/agent-state.db` (default path; not overridden)
**Audit script:** `scripts/audit_mcp_response_sizes.ts`
**Reproduce:**
```bash
cd /Users/yibeichen/github/schist && \
  npm run audit --prefix mcp-server -- \
    --vault "$HOME/schist-vault" \
    --search-query "schist"
```

PR 7 post-change measurement. Compare against the 2026-05-20 PR 6 snapshot in
`audit-2026-05-20-mcp-response-sizes-pr6.md`.

---

## 2026-05-22 — after PR 7 (get_context reason-string verbose adoption)

| Tool | Bytes | Tokens | Entries | Notes |
|------|-------|--------|---------|-------|
| `search_notes` (query="schist") | 8,672 | 2,297 | 20 | Unchanged from PR 6. |
| `list_concepts` (no opts) | 10,961 | 2,896 | 50 | Cap at default `limit: 50` (vault has more concepts). Cursor present. |
| `list_domains` (no opts) | 14 | 4 | 0 | Vault.yaml has no `domains:` block — empty array is normal. |
| `query_graph` (`SELECT * FROM docs`) | 251,009 | 64,091 | 100 | Cap at 100 — corpus has grown since PR 5/6 audits. |
| `get_context` (minimal) | 186 | 56 | 1 | Unchanged behavior. Counts only. |
| `get_context` (standard) | 1,919 | 623 | 1 | Unchanged behavior. recent + hotConcepts. |
| `get_context` (full, **no verbose**) | **2,021** | **650** | **1** | **NEW BEHAVIOR (PR 7): soft-downgrade.** Without a verbose reason ≥12 cp, the server runs at depth="standard" and attaches a `verboseNote` hint. Delta vs `standard` (102 bytes / 27 tokens) is the verboseNote field. NO `tagCloud` field. |
| `search_memory` (limit=50) | 5,332 | 1,467 | 13 | Mac memory DB; fewer entries than HPC. |

Tokens counted via `gpt-tokenizer` (`o200k_base`, GPT-4o BPE). See the
2026-05-10 baseline doc for proxy accuracy notes.

---

## get_context delta analysis

The headline measurement: **`get_context(depth: "full")` without verbose**
no longer pays the tagCloud computation cost. The audit script does not
supply a verbose reason, so this is the "lazy caller" path the spec is
designed to gate.

| Metric | PR 6 (2026-05-20) | PR 7 (2026-05-22) | Notes |
|--------|-------------------|-------------------|-------|
| `get_context (full)` bytes | 2,840 | 2,021 | **−819 bytes (−29%)** — the missing tagCloud field on a 39-concept vault. |
| `get_context (full)` tokens | 907 | 650 | **−257 tokens (−28%)** — same source. |
| Response shape | `{ vault, recent, hotConcepts, tagCloud }` | `{ vault, recent, hotConcepts, verboseNote }` | tagCloud absent on downgrade; verboseNote added. |
| Behavior | tagCloud always computed | tagCloud requires `verbose: "<reason ≥12 chars>"` | Spec §"Reason-string verbose" — soft-downgrade for missing/whitespace; INVALID_ARG for type/length misuse. |

To reproduce the tagCloud-on path (for comparison): the audit script
doesn't yet pass `verbose`. Re-measurement with `verbose: "audit run for
size snapshot"` would restore tagCloud and bring the byte count back to
~2,840-equivalent (it scales linearly with the tag-cloud size, which is
capped at 30 entries by sqlite-reader.getContext line 337).

The behavior change is the load-bearing PR 7 delivery for `get_context`.
The byte reduction is a downstream effect, not the primary goal — the
goal is gating expensive computation behind explicit intent.

---

## Spec coverage walkthrough (PR 7)

Every PR 7 row in `docs/superpowers/specs/2026-05-04-mcp-context-efficiency.md`
traced to its landing implementation. Verified via `grep` against
`mcp-server/src/` at HEAD `feat/issue-50-get-context-verbose`.

| Spec requirement | File | Evidence |
|-----------------|------|---------|
| `get_context` handler accepts `verbose?: string` arg | `mcp-server/src/tools.ts` (get_context) | Function signature `args: { depth?: ...; verbose?: string }` |
| Reject INVALID_ARG for boolean / non-string verbose | `mcp-server/src/tools.ts` (get_context) | `if ("error" in v) return v.error;` — parseVerbose handles type error |
| Soft-downgrade `depth="full"` without valid verbose | `mcp-server/src/tools.ts` (get_context) | `if (requestedDepth === "full" && !verboseEnabled) { effectiveDepth = "standard"; downgradeNote = ...; }` |
| Soft hint via `verboseNote` field | `mcp-server/src/tools.ts` (get_context) | `if (verboseNote !== undefined) context.verboseNote = verboseNote;` |
| Whitespace-only verbose treated as missing | `mcp-server/src/protocol/verbose.ts` | `parseVerbose` returns `{ enabled: false }` for whitespace-only (verbose.ts:55-60) — inherited by get_context |
| Stderr audit log on true `depth="full"` path | `mcp-server/src/tools.ts` (get_context) | `logVerbose({ tool: TOOL_NAME, owner: activeOwner, reason: verboseReason });` |
| Rate-limit `verboseNote` (high-frequency hint) | `mcp-server/src/tools.ts` (get_context) | `const note = noteHighFrequency(...); if (note !== null) freqNote = note;` |
| Tool-registry schema gains `verbose` input + description | `mcp-server/src/tool-registry.ts` | get_context entry's inputSchema now includes `verbose: { type: "string", description: ... }` |
| `GetContextResponse` type | `mcp-server/src/types.ts` | New `GetContextResponse` interface exported |
| No cursor pipeline | `mcp-server/src/tools.ts` (get_context) | Confirmed absent — no `canonicalizeQueryHash`, `decodeCursor`, `checkRefusal`, `recordIssued`, `issueCursor` in the function body |

All 10 spec rows confirmed present. No gaps found.

---

## Reproduction notes

- **Vault corpus at audit time:** local Mac vault at `~/schist-vault`, 39+ concepts (audit shows `list_concepts` capped at 50), `search_notes` returns 20 results for query "schist".
- **Memory DB at audit time:** local Mac `~/.openclaw/memory/agent-state.db`, 13 entries on a `search_memory(limit=50)` call.
- **Branch HEAD:** `feat/issue-50-get-context-verbose` (post-implementation, pre-PR commit).
- **Tokenizer:** `gpt-tokenizer` (`o200k_base`).
- **Node version:** v22 LTS (Mac local default).
- **Platform:** macOS (Darwin 25.4.0).
- **Search query:** `"schist"` (same as PR 3/4/5/6 audits).

---

## What this PR does NOT measure

- `get_context(depth: "full", verbose: "...")` — the audit script does not
  currently thread a verbose reason. Adding that would require an audit-script
  extension; deferred to PR 8 (migration docs). Manually verified that with a
  valid verbose reason the response shape includes `tagCloud` (see
  `mcp-server/tests/get-context-tool.test.ts` Case 1).
- Rate-limit-triggered `verboseNote` (the `"frequent"` concat path) —
  verified via test (`get-context-tool.test.ts` Case 7) but not part of
  the audit-script measurement set.
