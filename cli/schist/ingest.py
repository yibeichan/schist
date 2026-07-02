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
HASHTAG_AT_START_RE = re.compile(r'^#[^\s,\]\}]+')
CONNECTION_RE = re.compile(
    r'^-\s+(\S+):\s+(\S+)(?:\s+"([^"]*)")?(?:\s+—\s+(.*))?$'
)

PAPER_FIELDS = {
    'authors',
    'year',
    'venue',
    'type',
    'doi',
    'arxiv_id',
    'pubmed_pmid',
    'bibtex_key',
    'url',
    'verification',
}


def _normalize_concept_slug(value: str) -> str:
    return value.strip().lower().replace(' ', '-')


def _normalize_tag(value: str) -> str:
    return value.strip().lstrip('#').strip()


def _opens_quoted_scalar(last_significant: str) -> bool:
    return last_significant in {'', '[', '{', ',', ':'}


def _trailing_backslashes(line: str, i: int) -> int:
    count = 0
    j = i - 1
    while j >= 0 and line[j] == '\\':
        count += 1
        j -= 1
    return count


def _quote_flow_hashtags(line: str) -> str:
    """Quote unquoted #tags inside YAML flow collections before YAML parsing."""
    if '#' not in line or ('[' not in line and '{' not in line):
        return line

    result: list[str] = []
    flow_depth = 0
    in_single = False
    in_double = False
    last_significant = ''

    i = 0
    while i < len(line):
        ch = line[i]

        if in_single:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    result.append("''")
                    i += 2
                    continue
                in_single = False
                last_significant = "'"
            result.append(ch)
            i += 1
            continue

        if in_double:
            if ch == '"' and _trailing_backslashes(line, i) % 2 == 0:
                in_double = False
                last_significant = '"'
            result.append(ch)
            i += 1
            continue

        if ch == "'" and _opens_quoted_scalar(last_significant):
            in_single = True
            last_significant = "'"
            result.append(ch)
            i += 1
            continue
        if ch == '"' and _opens_quoted_scalar(last_significant):
            in_double = True
            last_significant = '"'
            result.append(ch)
            i += 1
            continue
        if ch in {'[', '{'}:
            flow_depth += 1
            last_significant = ch
            result.append(ch)
            i += 1
            continue
        if ch in {']', '}'}:
            if flow_depth > 0:
                flow_depth -= 1
            last_significant = ch
            result.append(ch)
            i += 1
            continue
        if ch == '#' and flow_depth > 0:
            match = HASHTAG_AT_START_RE.match(line[i:])
            if match:
                tag = match.group(0)
                result.append(f'"{tag}"')
                last_significant = tag[-1]
                i += len(tag)
                continue

        result.append(ch)
        if ch not in {' ', '\t'}:
            last_significant = ch
        i += 1

    return ''.join(result)


def patch_frontmatter_flow_hashtags(content: str) -> str:
    """Mirror mcp-server parseNote's pre-patch before frontmatter parsing."""
    lines = content.split('\n')
    in_fm = False
    patched = False
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
        if in_fm:
            next_ln = _quote_flow_hashtags(ln)
            patched = patched or next_ln != ln
            ln = next_ln
        patched_lines.append(ln)

    return '\n'.join(patched_lines) if patched else content


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


def _string_or_none(value) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if value is not None and not isinstance(value, (list, dict)):
        return str(value)
    return None


def _int_or_none(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _authors_json(value) -> str | None:
    if isinstance(value, list):
        authors = [a for a in value if isinstance(a, str) and a]
        return json.dumps(authors) if authors else None
    if isinstance(value, str) and value:
        return json.dumps([value])
    return None


def _verification_sources_json(value) -> str | None:
    sources: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                sources.append(item)
            if isinstance(item, dict) and item:
                for key, val in item.items():
                    sources.append(f'{key}:{val}' if val is not None else str(key))
        return json.dumps(sources) if sources else None
    if isinstance(value, str) and value:
        return json.dumps([value])
    return None


def paper_metadata_from_frontmatter(meta: dict, rel: Path) -> tuple | None:
    """Return a paper_metadata row tuple for paper-like frontmatter."""
    is_paper_path = len(rel.parts) > 1 and rel.parts[0] == 'papers'
    has_paper_fields = any(field in meta for field in PAPER_FIELDS)
    if not is_paper_path and not has_paper_fields:
        return None

    verification = meta.get('verification')
    if not isinstance(verification, dict):
        verification = {}
    verified_date = _string_or_none(verification.get('verified_on'))
    verified_by = _string_or_none(verification.get('verified_by'))
    verification_sources = _verification_sources_json(verification.get('verified_against'))
    verified = 1 if verified_date else 0

    return (
        _authors_json(meta.get('authors')),
        _int_or_none(meta.get('year')),
        _string_or_none(meta.get('venue')),
        _string_or_none(meta.get('type')),
        _string_or_none(meta.get('doi')),
        _string_or_none(meta.get('arxiv_id')),
        _string_or_none(meta.get('pubmed_pmid')),
        _string_or_none(meta.get('bibtex_key')),
        verified,
        verified_by,
        verified_date,
        verification_sources,
        _string_or_none(meta.get('url')),
    )


def ingest(vault_path: str, db_path: str):
    vault = Path(vault_path).resolve()
    schema_path = Path(__file__).parent / 'schema.sql'

    conn = sqlite3.connect(db_path)
    try:
        _ingest_into(conn, vault, schema_path)
    finally:
        conn.close()


def _ingest_into(conn: sqlite3.Connection, vault: Path, schema_path: Path) -> None:
    """Run the actual ingest against an open connection.

    Split out from `ingest()` so the connection close is guaranteed via
    try/finally even when the loop raises. The split also lets the
    schema executescript stay outside the transaction (it commits
    implicitly), while the data INSERTs are wrapped in a transaction
    that rolls back on failure — `_run_ingest` then deletes the
    schema-only DB so the next get_db() rebuilds from scratch instead
    of trusting an empty index.
    """
    conn.executescript(schema_path.read_text())

    # WAL (set by schema.sql, #254) is unsafe when the vault lives on a
    # network filesystem — SQLite requires all processes using a WAL DB to
    # be on the same host, and HPC spokes (e.g. orcd) may serve the vault
    # over NFS/Lustre to login and compute nodes at once. SCHIST_NO_WAL=1
    # keeps the pre-#254 rollback-journal behavior on such deployments.
    if os.environ.get('SCHIST_NO_WAL'):
        conn.execute('PRAGMA journal_mode=DELETE')

    doc_count = 0
    concept_count = 0
    edge_count = 0

    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        # Skip hidden/excluded dirs
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        try:
            raw_text = md_file.read_text(encoding='utf-8')
            patched_text = patch_frontmatter_flow_hashtags(raw_text)
            post = frontmatter.loads(patched_text)
        except UnicodeDecodeError as e:
            # A single non-UTF-8 .md file (binary attachment, non-UTF-8 editor,
            # corruption) must never abort the whole index — that leaves the
            # vault in a permanent read outage until the file is removed (#296).
            print(f'  WARN: skipping {rel} — invalid UTF-8: {e}')
            continue
        except Exception as e:
            print(f'  WARN: skipping {rel} — {type(e).__name__}: {e}')
            continue
        meta = post.metadata
        body = post.content

        # Normalize tags: strip # prefix
        raw_tags = meta.get('tags', [])
        if isinstance(raw_tags, list):
            tags = [
                tag
                for t in raw_tags
                if isinstance(t, str) and (tag := _normalize_tag(t))
            ]
        else:
            tags = []

        tags_json = json.dumps(tags) if tags else None

        # Concepts from frontmatter
        raw_concepts = meta.get('concepts', [])
        if isinstance(raw_concepts, list):
            concepts = [
                _normalize_concept_slug(c)
                for c in raw_concepts
                if isinstance(c, str) and c.strip()
            ]
        else:
            concepts = []
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

        # Confidence: from frontmatter, validated against enum.
        # NULL when not declared — distinguishes "agent didn't say" from
        # "agent said medium" (don't default to medium here, see issue #69).
        raw_confidence = meta.get('confidence')
        confidence = raw_confidence if raw_confidence in {"low", "medium", "high"} else None
        raw_file_ref = meta.get('file_ref')
        file_ref = raw_file_ref if isinstance(raw_file_ref, str) and raw_file_ref else None

        doc_id = str(rel)

        # Determine if this is a concept file (in concepts/ dir or has 'concept' key)
        is_concept = 'concept' in meta or (rel.parts[0] == 'concepts' if len(rel.parts) > 1 else False)

        # Title: explicit title > concept key > derive from filename
        title = meta.get('title') or meta.get('topic') or meta.get('concept') or title_from_filename(rel.name)

        # Status: type-guard like every other scalar field above. A non-string
        # value (status: 42, status: true, status: [draft]) would otherwise be
        # stored with SQLite's native affinity, so `WHERE status = 'draft'`
        # silently misses it and TS readers treating status as string break.
        # (#278; distinct from #276 which validates the string against
        # config.statuses in create_note.)
        raw_status = meta.get('status')
        status = raw_status if isinstance(raw_status, str) else None

        # Insert into docs
        date_val = meta.get('date')
        if date_val is not None:
            date_val = str(date_val)
        conn.execute(
            'INSERT INTO docs (id, title, date, status, tags, concepts, body, scope, source, confidence, file_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (doc_id, title, date_val, status, tags_json, concepts_json, body, scope, source, confidence, file_ref),
        )
        doc_count += 1

        paper_row = paper_metadata_from_frontmatter(meta, rel)
        if paper_row is not None:
            conn.execute(
                """
                INSERT INTO paper_metadata (
                    doc_id, authors, year, venue, paper_type, doi, arxiv_id,
                    pubmed_pmid, bibtex_key, verified, verified_by,
                    verified_date, verification_sources, url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (doc_id, *paper_row),
            )

        # Insert concept record if this is a concept file
        concept_slug = None
        if is_concept:
            slug = meta.get('concept', rel.stem)
            slug = _normalize_concept_slug(slug if isinstance(slug, str) else rel.stem)
            concept_slug = slug
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
            if not (is_concept and concept_slug is not None and c == concept_slug):
                concept_count += 1
            if not is_concept:
                result = conn.execute(
                    'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                    (doc_id, c, 'references', None),
                )
                edge_count += result.rowcount

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

    conn.execute(
        """
        DELETE FROM concept_aliases
        WHERE duplicate_slug NOT IN (SELECT slug FROM concepts)
           OR canonical_slug NOT IN (SELECT slug FROM concepts)
        """
    )
    conn.commit()
    print(f'Ingested: {doc_count} docs, {concept_count} concepts, {edge_count} edges')


def main() -> None:
    parser = argparse.ArgumentParser(description='Ingest markdown vault into SQLite')
    parser.add_argument('--vault', required=True, help='Path to vault root')
    parser.add_argument('--db', required=True, help='Path to SQLite database')
    args = parser.parse_args()
    ingest(args.vault, args.db)


if __name__ == '__main__':
    main()
