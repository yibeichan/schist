-- schist.db — query layer, rebuilt from markdown on every commit
-- NEVER the source of truth. Disposable. Delete and re-ingest anytime.

DROP TABLE IF EXISTS docs_fts;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS concepts;
DROP TABLE IF EXISTS docs;
DROP TABLE IF EXISTS domains;

CREATE TABLE docs (
    id          TEXT PRIMARY KEY,       -- relative path: "notes/2026-03-26-attention.md"
    title       TEXT NOT NULL,
    date        TEXT,                   -- ISO 8601: "2026-03-26"
    status      TEXT DEFAULT 'draft',   -- draft | review | final | archived
    tags        TEXT,                   -- JSON array: '["attention", "transformer"]'
    concepts    TEXT,                   -- JSON array of concept slugs
    body        TEXT NOT NULL,          -- full markdown body (sans frontmatter)
    scope       TEXT DEFAULT 'global', -- derived from directory or frontmatter
    source      TEXT,                  -- "human" | "agent" | null
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE concepts (
    slug        TEXT PRIMARY KEY,       -- stable slug: "backpropagation"
    title       TEXT NOT NULL,          -- display name: "Backpropagation"
    description TEXT,                   -- one-liner from concept file
    tags        TEXT,                   -- JSON array
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,          -- doc id or concept slug
    target      TEXT NOT NULL,          -- doc id or concept slug
    type        TEXT NOT NULL,          -- extends | contradicts | supports | etc.
    context     TEXT,                   -- optional annotation
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(source, target, type)
);

-- Full-text search over docs
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title,
    body,
    tags,
    scope UNINDEXED,
    content='docs',
    content_rowid='rowid'
);

-- Triggers to keep FTS in sync during ingestion
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, body, tags, scope)
    VALUES (new.rowid, new.title, new.body, new.tags, new.scope);
END;

CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, body, tags, scope)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags, old.scope);
END;

-- Indexes
CREATE INDEX idx_edges_source ON edges(source);
CREATE INDEX idx_edges_target ON edges(target);
CREATE INDEX idx_edges_type ON edges(type);
CREATE INDEX idx_docs_status ON docs(status);
CREATE INDEX idx_docs_date ON docs(date);

-- ── Derived from vault.yaml, rebuilt every ingest ─────────────────────────
-- `domains` mirrors the top-level `domains:` list in vault.yaml (the
-- source of truth per schema/vault-yaml.md). It's in the DROP list above
-- and is rebuilt by `_populate_domains()` in ingest.py on every ingest,
-- so entries added to or removed from vault.yaml propagate automatically.

CREATE TABLE domains (
  slug        TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  description TEXT,
  parent_slug TEXT REFERENCES domains(slug)
);

-- ── MCP-written side table (survives commit-path rebuilds) ────────────────
-- `concept_aliases` is written by the MCP `add_concept_alias` tool. It uses
-- CREATE TABLE IF NOT EXISTS and is NOT in the DROP list above, so on the
-- commit-path rebuild (post-commit hook re-runs ingest.py on the existing
-- DB) its rows survive. On the spoke-pull path (`_rebuild_index` in
-- `cli/schist/sync.py`), rows are copied forward from the backup by
-- `_preserve_side_tables` — see PR #24.
--
-- `agent_memory` and `agent_state` intentionally do NOT live here. They use
-- a separate database (`~/.openclaw/memory/agent-state.db` by default, or
-- `SCHIST_MEMORY_DB`) whose schema is defined inline in
-- `mcp-server/src/sqlite-reader.ts` as the `MEMORY_SCHEMA` constant.

CREATE TABLE IF NOT EXISTS concept_aliases (
  duplicate_slug TEXT NOT NULL REFERENCES concepts(slug),
  canonical_slug TEXT NOT NULL REFERENCES concepts(slug),
  reason         TEXT,
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (duplicate_slug, canonical_slug)
);
