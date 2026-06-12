# Schist Conventions

This document describes recommended authoring patterns on top of the canonical
vault schema in `schema/SCHEMA.md`.

## External Briefs And Issue Filing

When an agent notices a schist gap or needs to file an external report, use the
MCP `compose_brief` tool to pack vault context first. The tool is a read-only
composer: it searches indexed notes, follows nearby graph edges, includes
optional pinned refs, and can list recent git-added paths. It returns markdown
plus suggested tags and cross-references.

After reviewing and editing the generated markdown, file it with the appropriate
external tool (`gh`, Linear, Jira, a lab notebook, or similar). Do not use
schist as the filing client for those systems; schist's role is to supply
vault-grounded context.

## Paper Notes: Citation-Grade Frontmatter

Use citation-grade paper notes when a `papers/` entry should act as an
agent-readable citation record, not just reading notes. The goal is to let
future agents resolve citation questions from the vault without re-verifying
basic bibliographic metadata.

Recommended frontmatter:

```yaml
---
title: >-
  Full canonical title as it appears in the publication
date: 2026-05-23
authors:
  - Lastname, Firstname
  - Lastname, Firstname
year: 2023
venue: Nature Human Behaviour
type: journal
doi: 10.1038/s41562-022-01516-2
arxiv_id: ""
pubmed_pmid: ""
bibtex_key: caucheteux2023predictive
url: https://doi.org/10.1038/s41562-022-01516-2
verification:
  verified_on: 2026-05-23
  verified_by: agent-hpc
  verified_against:
    - crossref:10.1038/s41562-022-01516-2
    - pubmed:36864133
  notes: ""
tags:
  - paper
  - domain-tag
status: review
source_agent: agent-hpc
file_ref: /mnt/data/papers/caucheteux-2023.pdf
---
```

Use empty strings for unavailable optional identifiers when preserving a stable
template is more useful than omitting fields.

Recommended body structure:

```markdown
## Bibliographic Summary

Two to four sentences describing the paper's central claim and evidence.

## Claims Supported By This Paper

- Specific claim this paper is cited for.
- Another claim, scoped narrowly enough that a future agent can reuse it safely.

## Where Cited In Vault

- Relative path to a note that cites or uses this paper, with short context.

## Caveats And Audit Notes

- Metadata disagreements, version caveats, verification caveats, or suspected duplicate records.

## Connections

- supports: concepts/example-concept.md "Why this paper supports the concept"
```

### Verification Sources

Rank `verification.verified_against` sources in this order:

1. `crossref:<doi>` for DOI-backed records.
2. `pubmed:<pmid>` for biomedical literature.
3. `arxiv:<arxiv_id>` for preprints and ML literature.
4. `semantic-scholar:<paperId>` for citation-graph support, not authoritative metadata.
5. `publisher-doi-page:<url>` when no stronger structured source is available.

### BibTeX Keys

Use `<first-author-lastname><year><short-topic>` when a plain
`<first-author-lastname><year>` key would collide. Example:
`caucheteux2023predictive` rather than `caucheteux2023` when the vault may hold
multiple Caucheteux 2023 papers.

### Versioning

When a preprint later becomes a journal article, prefer a new paper note or a
clear audit note over silently rewriting citation claims in place. Link records
with explicit connections so the graph preserves the version history.
