# Schist Markdown Schema Specification

> Version 1.0 — This is the canonical spec for vault content format.

## Overview

All knowledge in a schist vault is stored as markdown files with YAML frontmatter. There are two node types: **documents** (notes, papers, etc.) and **concepts** (stable reference nodes). Edges between nodes are expressed as typed connections in the document's `## Connections` section.

## Document Notes

**Location:** Any directory under vault root except `concepts/` (typically `notes/`, `papers/`, `logs/`)
**Naming:** `YYYY-MM-DD-slug.md` (date prefix required for notes)

### Frontmatter Fields

| Field      | Type       | Required | Default   | Description |
|------------|------------|----------|-----------|-------------|
| `title`    | string     | yes      | —         | Human-readable title |
| `date`     | string     | yes      | —         | ISO 8601 date: `2026-03-26` |
| `tags`     | string[]   | no       | `[]`      | Lowercase, hyphenated tags |
| `status`   | string     | no       | `draft`   | One of: `draft`, `review`, `final`, `archived` |
| `concepts` | string[]   | no       | `[]`      | Concept slugs this note relates to |
| `related`  | string[]   | no       | `[]`      | Relative paths to related notes |
| `confidence` | string   | no       | `null`    | Agent-declared confidence: `low`, `medium`, or `high`. NULL = not declared (load-bearing distinction from `'medium'`) |
| `source_agent` | string | no | `null` | Agent identity that originally created the note through MCP. Preserved on later mutations; it is not a "last modified by" field. |

### Example

```yaml
---
title: "Sparse Attention Patterns in Long Sequences"
date: 2026-03-26
tags: [attention, sparse, efficiency]
status: draft
concepts: [self-attention, computational-complexity]
related: [notes/2026-03-20-dense-attention-limits.md]
source_agent: claude
---

The body of the note in standard markdown. Any valid markdown is accepted.

Code blocks, tables, images (local or URL), and LaTeX math ($e = mc^2$) are all fine.

## Connections

- extends: notes/2026-03-20-dense-attention-limits.md "Proposes sparse alternatives to the dense attention bottleneck"
- supports: concepts/self-attention "Demonstrates self-attention works with sparse patterns"
- contradicts: notes/2026-03-18-attention-must-be-dense.md "Shows dense attention is not required for quality"
```

### Attribution Fields

`source_agent` records the original authoring agent stamped by `create_note`.
Later note mutations such as `add_connection` preserve the existing
`source_agent` value. Those mutations are attributed to the mutating agent in
the git commit subject, not by rewriting frontmatter.

Agent memory uses a separate attribution surface: every memory row stores its
writer in the `agent_memory.owner` column.

## Concept Nodes

**Location:** `concepts/` directory only
**Naming:** `slug.md` (stable slug, never changes once created)

### Frontmatter Fields

| Field         | Type       | Required | Default | Description |
|---------------|------------|----------|---------|-------------|
| `title`       | string     | yes      | —       | Display name (can contain spaces, caps) |
| `tags`        | string[]   | no       | `[]`    | Lowercase, hyphenated tags |
| `aliases`     | string[]   | no       | `[]`    | Alternative names for this concept |

### Example

```yaml
---
title: "Self-Attention"
tags: [mechanism, neural-network, attention]
aliases: [self-attn, intra-attention, self-attention-mechanism]
---

A mechanism where each element in a sequence attends to all other elements to compute a weighted representation. Core component of the Transformer architecture.
```

### Rules

- Concept files have **no date** field (they are timeless reference nodes)
- Concept files have **no status** field (they are always active)
- Concept files have **no `## Connections` section** (connections point TO concepts, not FROM them)
- The filename slug IS the concept's stable identifier: `self-attention.md` → slug `self-attention`
- Slugs are lowercase, hyphen-separated, no special characters: `[a-z0-9-]+`

## Connection Types

Connections express typed, directed relationships between nodes.

| Type                | Semantics | Example |
|---------------------|-----------|---------|
| `extends`           | Builds upon, adds to | "This paper extends the original transformer with sparse patterns" |
| `contradicts`       | Disagrees with, refutes | "This finding contradicts the assumption that dense attention is required" |
| `supports`          | Provides evidence for | "Experimental results support the self-attention hypothesis" |
| `replicates`        | Reproduces results of | "Successfully replicates the original attention experiment" |
| `applies-method-of` | Uses methodology from | "Applies the training method of the original paper to a new domain" |
| `reinterprets`      | Offers new interpretation of | "Reinterprets attention weights as a form of memory retrieval" |
| `related`           | General association | "Related work in the same research area" |

### Connection Syntax

In the `## Connections` section of a document:

```
- TYPE: TARGET "OPTIONAL CONTEXT"
```

- **TYPE**: One of the connection types above (lowercase)
- **TARGET**: Relative path to target note or concept (e.g., `notes/2026-03-20-foo.md` or `concepts/self-attention.md`)
- **CONTEXT**: Optional quoted string explaining the connection

Multiple connections per document are allowed. One connection per line.

## Slug Derivation

Given a title, derive the slug:

1. Convert to lowercase
2. Replace spaces and underscores with hyphens
3. Remove all characters except `[a-z0-9-]`
4. Collapse consecutive hyphens
5. Trim leading/trailing hyphens

Examples:
- `"Self-Attention"` → `self-attention`
- `"Backpropagation Through Time"` → `backpropagation-through-time`
- `"O(n²) Complexity"` → `on-complexity`

## Status Lifecycle

```
draft → review → final → archived
```

- **draft**: Work in progress. Agents create notes in this state.
- **review**: Ready for human review.
- **final**: Reviewed and accepted. Content is stable.
- **archived**: Superseded or no longer relevant. Never deleted.

Transitions are forward-only. A `final` note cannot go back to `draft`. To revise a final note, create a new note that `extends:` or `contradicts:` it.

## Append-Only Rule

**Never edit conclusions in-place.** If new information changes a finding:

1. Create a new note
2. Add a connection (`extends:`, `contradicts:`, or `reinterprets:`) to the original
3. The graph shows the evolution of understanding over time

This rule preserves the intellectual history of the knowledge graph.

## Directory Structure (Vault)

```
vault/
├── notes/          # Timestamped research notes
├── papers/         # Paper summaries and analyses
├── concepts/       # Concept node files (stable slugs)
├── research/       # Project-scoped research notes
├── decisions/      # ADRs / decision records
├── ops/            # Runbooks, ops notes
├── projects/       # Project-kickoff and tracking notes
├── logs/           # Session logs, meeting notes
├── shared/         # Cross-spoke shared content (e.g. shared/skills/)
├── vault.yaml      # ACL + scope config (read by pre-receive hook)
├── schist.yaml     # Schema/dir overrides (read by MCP server, optional)
└── .schist/
    └── schist.db   # SQLite database (auto-generated, gitignored)
```

The `.schist/` directory is auto-created and gitignored. It contains derived data only. The canonical default directory list is defined in `cli/schist/default.yaml` (shipped inside the schist Python package); a `schist.yaml` file at the vault root overrides it per-vault.

## Schema Configuration

Vaults can override the default schema by placing a `schist.yaml` file at the vault root. Both `mcp-server` (`loadVaultConfig` in `tools.ts`) and the `schist schema` CLI (`commands.py:schema`) read this file, falling back to `cli/schist/default.yaml` when fields are absent. `vault.yaml` (also at the vault root) is a separate file for ACL/scope configuration and is parsed by `cli/schist/acl.py`.

```yaml
# schist.yaml (at vault root)
connection_types:
  - extends
  - contradicts
  - supports
  - replicates
  - applies-method-of
  - reinterprets
  - related
  # Add custom types:
  - inspired-by
  - depends-on

statuses:
  - draft
  - review
  - final
  - archived

directories:
  notes: notes/
  papers: papers/
  concepts: concepts/
  research: research/
  decisions: decisions/
  ops: ops/
  projects: projects/
  logs: logs/
  # Add custom:
  experiments: experiments/

# Slug validation regex (default)
slug_pattern: "^[a-z0-9-]+$"
```

If no `schist.yaml` exists at the vault root, the canonical defaults are read from `cli/schist/default.yaml` (shipped inside the schist Python package). This is the single source of truth for the directory list — both the Python and TypeScript layers will load it at runtime (see Tasks 2 and 5 of the flatten-spoke-dirs refactor).

## Validation Rules

The `schist schema --validate` command checks:

1. All frontmatter fields match expected types
2. All `status` values are in the allowed set
3. All connection types are in the allowed set
4. All connection targets resolve to existing files
5. All concept slugs match the `slug_pattern`
6. All note filenames match `YYYY-MM-DD-slug.md` pattern
7. No concept files have `## Connections` sections
8. No orphaned concepts (concepts with zero incoming edges — warning, not error)
