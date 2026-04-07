#!/usr/bin/env python3
"""schist ingestion — parse markdown vault into SQLite."""

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import frontmatter

SKIP_DIRS = {'.git', '.schist'}
# Matches #word inside YAML flow sequences — quote them so YAML doesn't treat # as comment
HASH_TAG_RE = re.compile(r'(#[\w-]+)')
CONNECTION_RE = re.compile(
    r'^-\s+(\S+):\s+(\S+)(?:\s+"([^"]*)")?(?:\s+—\s+(.*))?$'
)


def parse_connections(body: str) -> list[dict]:
    """Extract connections from ## Connections section."""
    edges = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('## Connections'):
            in_section = True
            continue
        if in_section and stripped.startswith('## '):
            break
        if not in_section or not stripped.startswith('- '):
            continue
        m = CONNECTION_RE.match(stripped)
        if m:
            conn_type, target, quoted_ctx, dash_ctx = m.groups()
            context = quoted_ctx or dash_ctx or None
            # Skip bracket references like [Moltbook, Jan 2026]
            if target.startswith('['):
                continue
            edges.append({
                'type': conn_type,
                'target': target,
                'context': context,
            })
    return edges


def title_from_filename(filename: str) -> str:
    """Derive title from filename: 2026-03-26-foo-bar.md -> foo bar."""
    stem = Path(filename).stem
    # Strip leading date pattern
    stem = re.sub(r'^\d{4}-\d{2}-\d{2}-?', '', stem)
    return stem.replace('-', ' ').strip() or stem


def ingest(vault_path: str, db_path: str):
    vault = Path(vault_path).resolve()
    schema_path = Path(__file__).parent / 'schema.sql'

    conn = sqlite3.connect(db_path)
    conn.executescript(schema_path.read_text())

    doc_count = 0
    concept_count = 0
    edge_count = 0

    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        # Skip hidden/excluded dirs
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        raw_text = md_file.read_text(encoding='utf-8')
        # Quote #hashtags in YAML flow sequences so YAML parser doesn't treat # as comment
        lines = raw_text.split('\n')
        in_fm = False
        patched_lines = []
        for i, ln in enumerate(lines):
            if i == 0 and ln.strip() == '---':
                in_fm = True
                patched_lines.append(ln)
                continue
            if in_fm and ln.strip() == '---':
                in_fm = False
                patched_lines.append(ln)
                continue
            if in_fm and '#' in ln and '[' in ln:
                ln = HASH_TAG_RE.sub(r'"\1"', ln)
            patched_lines.append(ln)
        patched_text = '\n'.join(patched_lines)

        try:
            post = frontmatter.loads(patched_text)
        except Exception as e:
            print(f'  WARN: skipping {rel} — frontmatter parse error: {e}')
            continue
        meta = post.metadata
        body = post.content

        # Normalize tags: strip # prefix
        raw_tags = meta.get('tags', [])
        if isinstance(raw_tags, list):
            tags = [t.lstrip('#') for t in raw_tags]
        else:
            tags = []

        tags_json = json.dumps(tags) if tags else None

        # Concepts from frontmatter
        raw_concepts = meta.get('concepts', [])
        concepts = raw_concepts if isinstance(raw_concepts, list) else []
        concepts_json = json.dumps(concepts) if concepts else None

        # Scope: explicit frontmatter > directory path > 'global'
        raw_scope = meta.get('scope')
        if isinstance(raw_scope, str) and raw_scope:
            scope = raw_scope
        elif len(rel.parts) > 1:
            scope = rel.parent.as_posix()
        else:
            scope = 'global'

        # Source: from frontmatter, defaults to None
        raw_source = meta.get('source')
        source = raw_source if raw_source in {"human", "agent"} else None

        doc_id = str(rel)

        # Determine if this is a concept file (in concepts/ dir or has 'concept' key)
        is_concept = 'concept' in meta or (rel.parts[0] == 'concepts' if len(rel.parts) > 1 else False)

        # Title: explicit title > concept key > derive from filename
        title = meta.get('title') or meta.get('topic') or meta.get('concept') or title_from_filename(rel.name)

        # Insert into docs
        date_val = meta.get('date')
        if date_val is not None:
            date_val = str(date_val)
        conn.execute(
            'INSERT INTO docs (id, title, date, status, tags, concepts, body, scope, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (doc_id, title, date_val, meta.get('status', 'draft'), tags_json, concepts_json, body, scope, source),
        )
        doc_count += 1

        # Insert concept record if this is a concept file
        if is_concept:
            slug = meta.get('concept', rel.stem)
            if isinstance(slug, str):
                slug = slug.lower().replace(' ', '-')
            desc = body.split('\n\n')[0].strip() if body else None
            concept_tags = json.dumps(tags) if tags else None
            conn.execute(
                'INSERT OR IGNORE INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, ?)',
                (slug, title, desc, concept_tags),
            )
            concept_count += 1

        # Also insert concepts referenced in frontmatter
        for c in concepts:
            conn.execute(
                'INSERT OR IGNORE INTO concepts (slug, title) VALUES (?, ?)',
                (c, c.replace('-', ' ').title()),
            )
            # Don't double-count if already counted above
            if not (is_concept and c == meta.get('concept', '').lower().replace(' ', '-')):
                concept_count += 1

        # Parse and insert edges
        for edge in parse_connections(body):
            try:
                conn.execute(
                    'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                    (doc_id, edge['target'], edge['type'], edge['context']),
                )
                edge_count += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    conn.close()
    print(f'Ingested: {doc_count} docs, {concept_count} concepts, {edge_count} edges')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Ingest markdown vault into SQLite')
    parser.add_argument('--vault', required=True, help='Path to vault root')
    parser.add_argument('--db', required=True, help='Path to SQLite database')
    args = parser.parse_args()
    ingest(args.vault, args.db)
