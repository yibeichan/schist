# Schema and Frontmatter Roadmap

Date: 2026-06-08

Related issues: #151, #133, #142, #138, #147

## Goal

Land the open schema/frontmatter work in a sequence that keeps the vault format
understandable, keeps SQLite changes additive, and avoids mixing citation
database semantics with generic attachment indexing.

## Current Decision

Use three separate tracks:

1. Paper-note convention and paper metadata indexing.
2. Generic external file references.
3. Graph semantics for `concepts:` frontmatter.

Do not implement a broad `docs` table expansion that tries to solve all three
tracks at once. Each track has a different stability level and consumer.

## Track 1: Paper Notes and Citation Metadata

Issues: #133, #151

First PR should be documentation-only:

- Add a citation-grade `papers/` frontmatter section to `schema/SCHEMA.md`.
- If a conventions document is introduced, make it the user-facing guide and
  keep `SCHEMA.md` as the canonical field reference.
- Define verification fields, source ranking, body sections, and bibtex-key
  collision guidance.

Second PR should add indexing:

- Add a `paper_metadata` side table keyed by `docs.id`.
- Populate it from paper frontmatter during ingest.
- Keep the generic `docs` table unchanged except for fields that apply to all
  documents.
- Add query examples for unverified papers, missing DOI, author/year/venue, and
  papers cited by a project.

Reasoning: citation fields are paper-specific. A side table keeps non-paper
documents from accumulating sparse columns while making paper verification
queryable.

## Track 2: External File References

Issues: #142, #138

Start with the simple convention:

- Add optional `file_ref: <path>` to `schema/SCHEMA.md`.
- Define it as an informative pointer only. schist does not store, sync,
  validate, or manage the referenced file.
- Index `file_ref` as a nullable `docs.file_ref` column with
  `idx_docs_file_ref`.
- Add optional `file_ref` support to note-creation entry points that construct
  frontmatter, including `create_note` and the CLI path if it exists for the
  same operation.

Defer `attachments` until real multi-file use cases require it:

- `attachments` needs a JSON shape, path normalization policy, and query
  helpers.
- Adding both `file_ref` and `attachments` in the first implementation creates
  two ways to represent one-file notes.

Reasoning: `file_ref` is generic across notes, papers, datasets, images, and
ops docs, so it belongs on `docs`. The more complex attachment model should
wait for evidence.

## Track 3: Concept Frontmatter as Graph Edges

Issue: #147

Before implementing, choose one implicit edge type and document it:

- Recommended type: `references`.
- Source: document id.
- Target: concept slug/path resolved from `concepts: [...]`.
- Context: `NULL`.

Implementation PR:

- Update ingest to emit `INSERT OR IGNORE` edges for frontmatter concepts.
- Add tests for concept stubs plus implicit edges.
- Document in `schema/SCHEMA.md` that `concepts: [...]` creates graph edges.

Reasoning: `concepts:` is already a schema-level relationship. Making it
traversable restores the graph value without requiring agents to duplicate the
same relationship in `## Connections`.

## Suggested PR Order

1. Documentation: citation-grade paper frontmatter (#133) and schema roadmap.
2. Code: implicit concept edges (#147). This is small and fixes graph semantics
   without adding new frontmatter fields.
3. Documentation plus code: `file_ref` convention and index (#142/#138).
4. Code: `paper_metadata` side table and ingest parsing (#151).
5. Later: tag normalization, `attachments`, automated DOI verification, and
   BibTeX export.

## Explicit Non-Goals For The First Pass

- No automated DOI/Crossref/PubMed verification tool.
- No tag normalization tables.
- No `attachments` JSON field.
- No migration that rewrites existing vault notes.
- No read-side authorization changes; see `SECURITY.md` for the current trust
  model.
