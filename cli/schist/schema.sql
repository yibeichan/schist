-- schist.db — query layer, rebuilt from markdown on every commit
-- NEVER the source of truth. Disposable. Delete and re-ingest anytime.

-- Journal mode (WAL, or rollback under SCHIST_NO_WAL) is set by
-- _ingest_into() BEFORE this script runs, so the DROP/CREATE phase below
-- executes in the mode the deployment asked for. See #254.

DROP TABLE IF EXISTS docs_fts;
DROP TABLE IF EXISTS paper_metadata;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS concepts;
DROP TABLE IF EXISTS docs;
-- Drop the retired `domains` table on upgrade-day ingest. Idempotent on
-- fresh installs (table never existed); cleans up the orphan on pre-#146
-- deployments where the table was created by the older schema.sql.
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
    confidence  TEXT CHECK(confidence IS NULL OR confidence IN ('low','medium','high')),  -- NULL = agent did not declare
    file_ref    TEXT,                  -- optional external file pointer; schist does not manage the file
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

CREATE TABLE paper_metadata (
    doc_id              TEXT PRIMARY KEY REFERENCES docs(id),
    authors             TEXT,               -- JSON array
    year                INTEGER,
    venue               TEXT,
    paper_type          TEXT,
    doi                 TEXT,
    arxiv_id            TEXT,
    pubmed_pmid         TEXT,
    bibtex_key          TEXT,
    verified            INTEGER DEFAULT 0,
    verified_by         TEXT,
    verified_date       TEXT,
    verification_sources TEXT,              -- JSON array of verification sources
    url                 TEXT
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
CREATE INDEX idx_docs_file_ref ON docs(file_ref);
CREATE INDEX idx_pm_verified ON paper_metadata(verified);
CREATE INDEX idx_pm_year ON paper_metadata(year);
CREATE INDEX idx_pm_doi ON paper_metadata(doi);
CREATE INDEX idx_pm_bibtex ON paper_metadata(bibtex_key);

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
