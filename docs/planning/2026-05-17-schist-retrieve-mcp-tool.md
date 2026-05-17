# `schist__retrieve` MCP Tool — Specification

**Filed:** 2026-05-17
**Status:** planned
**Depends on:** `schist-rag` Python companion service (Phase 1)

---

## Summary

Add a new MCP tool `schist__retrieve(query)` to the schist MCP server that provides
RAG-enhanced retrieval: query rewriting, multi-source search (notes + concepts + memory),
reranking via a cross-encoder, deduplication, and formatted context output.

This is Phase 2 of the schist RAG initiative. Phase 1 (`schist-rag` Python service)
must be deployed first — the MCP tool delegates reranking/embedding to it via localhost HTTP.

---

## Tool Definition

### Name

`schist__retrieve`

### Description

Perform RAG-enhanced retrieval across notes, concepts, and agent memory.
Semantic search + FTS5 hybrid, reranked by a cross-encoder, with deduplication
and formatted output. Designed for agents needing rich context without
manually orchestrating multiple schist calls.

### Input Schema

```typescript
{
  query: string;              // The search query (natural language)
  limit?: number;             // Max results to return (default 10, max 30)
  tags?: string[];            // Optional: filter by tags (AND logic)
  status?: string;            // Optional: filter by note status
  scope?: string;             // Optional: scope filter (inherit, global, etc.)
  include_memory?: boolean;   // Include agent memory entries? (default false)
  min_score?: number;         // Minimum relevance score 0-1 (default 0.3)
}
```

### Output

```typescript
{
  query: string;
  results: Array<{
    id: string;               // Note path or memory entry id
    title: string;
    snippet: string;          // Highlighted, truncated excerpt
    score: number;            // Reranker relevance score 0-1
    source: "note" | "concept" | "memory";
    tags: string[];
    status?: string;
    date?: string;
  }>;
  total: number;
  took_ms: number;
}
```

---

## Implementation Plan

### Step 1: Add tool definition to `tool-registry.ts`

New entry in `makeReadTools()` — this is a READ tool. (As of #72 the
capability gate has been removed entirely; all tools are callable
without any opt-in. Write authorization is enforced at the data layer
by `validateOwner`.) All agents can call it at session start.

### Step 2: Implement handler in `tools.ts`

New async function `retrieve(vaultRoot, args)`:

1. **Multi-source fan-out** (parallel):
   - FTS5 `search_notes` with the raw query → note candidates
   - `search_memory` if `include_memory=true` → memory candidates
   - SQL `LIKE` fallback if FTS5 returns < 3 results (handles single-word queries that FTS5 choke on)

2. **Deduplicate** by note id (same note from FTS5 and LIKE)

3. **Rerank** (Phase 2a — inline scoring; Phase 2b — cross-encoder):
   - **Phase 2a (minimum viable):** Score = FTS5 rank * content-length bonus. Fast, no external call.
   - **Phase 2b (after cross-encoder deployed):** `POST localhost:8788/rerank` with query + candidates → get scores back. Fall back to 2a if the Python service is down.

4. **Format** results with snippet extraction from note body
   (reuse existing `markdown-parser.ts` logic)

5. **Return** structured response

### Step 3: Wire into `index.ts`

Add one case to the switch statement:

```typescript
case "retrieve":
  result = await retrieve(vaultRoot, toolArgs as Parameters<typeof retrieve>[1]);
  break;
```

---

## Edge Cases

| Case | Handling |
|------|----------|
| RAG service down | Fall back to FTS5-only ranking, log warning, return results with degraded flag |
| Empty query | Return `VALIDATION_ERROR` |
| No candidates found | Return empty results array, `total: 0` |
| FTS5 returns 0 results | SQL `LIKE` fallback for partial word / single-word queries |
| Same note from multiple sources | Deduplicate by ID, keep highest score |
| Very long notes (>10K chars) | Truncate snippet to first 500 chars of matching section |

---

## Migration

- After deployment, update the **startup protocol** in each agent's `AGENTS.md`:
  Replace the current `schist__search_notes(query=ADMIN,task topic,limit=5)` with
  `schist__retrieve(query=...)` for richer session-start context.
- No breaking changes — `search_notes` remains available for tools/scripts that depend on it.

---

## Future (Phase 3)

- Embedding-based ANN search (new SQLite `note_chunks` table)
- Hybrid retrieval: FTS5 + ANN → merged → reranked
- Automatic chunking of long notes during vault ingestion
