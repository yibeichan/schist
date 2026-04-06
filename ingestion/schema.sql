-- schist.db — query layer, rebuilt from markdown on every commit
-- NEVER the source of truth. Disposable. Delete and re-ingest anytime.

DROP TABLE IF EXISTS docs_fts;
DROP TABLE IF EXISTS edges;
DROP TABLE IF EXISTS concepts;
DROP TABLE IF EXISTS docs;

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
    scope,
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

-- ── Memory V2 Extension ────────────────────────────────────────────────────
-- These tables are NOT rebuilt on re-ingest. They are persistent state.

CREATE TABLE IF NOT EXISTS agent_memory (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  owner        TEXT NOT NULL,
  date         TEXT NOT NULL,
  entry_type   TEXT NOT NULL CHECK(entry_type IN ('decision','lesson','blocker','completion','observation')),
  content      TEXT NOT NULL,
  tags         TEXT,
  related_doc  TEXT,
  source_ref   TEXT,
  confidence   TEXT NOT NULL DEFAULT 'medium' CHECK(confidence IN ('low','medium','high')),
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(owner, entry_type, content, tags, content='agent_memory', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS agent_memory_ai AFTER INSERT ON agent_memory BEGIN
  INSERT INTO agent_memory_fts(rowid, owner, entry_type, content, tags)
  VALUES (new.id, new.owner, new.entry_type, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS agent_memory_ad AFTER DELETE ON agent_memory BEGIN
  INSERT INTO agent_memory_fts(agent_memory_fts, rowid, owner, entry_type, content, tags)
  VALUES ('delete', old.id, old.owner, old.entry_type, old.content, old.tags);
END;

CREATE TABLE IF NOT EXISTS agent_state (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  owner      TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  ttl_hours  INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS domains (
  slug        TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  description TEXT,
  parent_slug TEXT REFERENCES domains(slug)
);

CREATE TABLE IF NOT EXISTS concept_aliases (
  duplicate_slug TEXT NOT NULL REFERENCES concepts(slug),
  canonical_slug TEXT NOT NULL REFERENCES concepts(slug),
  reason         TEXT,
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (duplicate_slug, canonical_slug)
);
