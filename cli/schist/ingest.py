#!/usr/bin/env python3
"""schist ingestion — parse markdown vault into SQLite."""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import frontmatter

try:
    from .index_contract import INDEX_SCHEMA_VERSION
except ImportError:  # pragma: no cover — running as a bare script (a legacy
    # `.schist/ingest.py` hook copy, see sync.py's POST_COMMIT_HOOK). The
    # script's own directory is on sys.path, so a sibling index_contract.py
    # copy resolves — exactly like the sibling schema.sql this script already
    # requires. No literal fallback here: a bare-script copy old enough to
    # lack the sibling module would stamp a version its schema.sql doesn't
    # match, and failing loudly beats silently re-minting the #339 drift.
    from index_contract import INDEX_SCHEMA_VERSION

SKIP_DIRS = {'.git', '.schist'}
HASHTAG_AT_START_RE = re.compile(r'^#[^\s,\]\}]+')
CONNECTION_RE = re.compile(
    r'^-\s+(\S+):\s+(\S+)(?:\s+"([^"]*)")?(?:\s+—\s+(.*))?$'
)

# Frontmatter fields whose presence marks a note as a paper (copied into
# paper_metadata). Pinned to schema/frontmatter-contract.json's
# `appliesTo: papers` set by cli/tests/test_frontmatter_contract.py —
# update the contract when this set changes.
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


# Explicit whitespace set shared verbatim with mcp-server's
# normalizeConceptSlug (tools.ts). Python's \s and JS's \s disagree at the
# edges — Python adds U+001C–U+001F (C0 separators) and U+0085 (NEL), JS adds
# U+FEFF (ZWNBSP) — exactly the cross-language drift family that caused #303.
# This is the UNION of both engines' sets (30 codepoints), so either
# language's notion of whitespace becomes a slug separator.
# schema/concept-slug-parity.json pins both implementations to the same
# table. #318.
_SLUG_WS_CHARS = (
    '\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680'
    '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007'
    '\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
)
# Edge-stripping uses str.strip(chars) — LINEAR — not a `^[ws]+|[ws]+$`
# regex, which backtracks quadratically over interior whitespace runs
# (20s on a 100k-space concept string; reachable from agent-supplied
# frontmatter, and ingest runs after every write).
_SLUG_WS_RUN = re.compile(f'[{re.escape(_SLUG_WS_CHARS)}]+')


def _normalize_concept_slug(value: str) -> str:
    """Mirror mcp-server normalizeConceptSlug: strip edge whitespace,
    lowercase, collapse each whitespace RUN to one dash (not just single
    spaces). The run-collapse matters: delete_note's cascade compares slugs
    it normalizes in TS against slugs this function stored in the index, and
    a `foo--bar` / `foo-bar` skew silently leaves dangling refs. See #303."""
    return _SLUG_WS_RUN.sub('-', value.strip(_SLUG_WS_CHARS).lower())


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
    # bool is an int subclass: `year: true` must coerce to NULL, not store 1.
    if isinstance(value, bool):
        return None
    # isdecimal, NOT isdigit: superscripts like "²" are isdigit()-True but
    # int() rejects them (ValueError aborted the whole rebuild pre-#351);
    # isdecimal() is False for them and True for exactly the digits int()
    # parses (including e.g. Arabic-Indic "٢٠٢٠").
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, int):
        # SQLite INTEGER is signed 64-bit; a wider Python int raises
        # OverflowError at bind time, which would savepoint-drop the WHOLE
        # note — coerce out-of-range values to NULL like every other
        # invalid year instead.
        return value if -(2**63) <= value < 2**63 else None
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
    # WAL lets the MCP server keep reading while ingest rebuilds the tables;
    # rollback-journal mode holds an EXCLUSIVE lock through the DDL phase and
    # concurrent readers fail with SQLITE_BUSY. But WAL is unsafe when the
    # vault lives on a network filesystem — SQLite requires all processes
    # using a WAL DB to be on the same host, and HPC spokes (e.g. orcd) may
    # serve the vault over NFS/Lustre to login and compute nodes at once.
    # SCHIST_NO_WAL=1 keeps the pre-#254 rollback-journal behavior there.
    # Must run BEFORE executescript so the DROP/CREATE phase never touches
    # WAL mode on such deployments; journal_mode requires autocommit, which
    # holds here (fresh connection, no open transaction). See #254.
    mode = 'DELETE' if os.environ.get('SCHIST_NO_WAL') else 'WAL'
    conn.execute(f'PRAGMA journal_mode={mode}')

    # Completion marker AND schema version in one value (#244, #130 D3):
    # clear it before the schema DDL commits, stamp INDEX_SCHEMA_VERSION back
    # atomically with the data commit at the end — "complete at schema
    # version N". A SIGKILL between the two (OOM killer, CI timeout, kill -9)
    # leaves user_version=0 with empty committed tables, which get_db()
    # detects and heals by re-ingesting — the on-failure unlink in
    # _run_ingest only covers Python exceptions.
    conn.execute('PRAGMA user_version = 0')

    conn.executescript(schema_path.read_text())

    # All data INSERTs run in ONE explicit transaction (see the docstring
    # above: rollback-on-failure + _run_ingest's partial-DB unlink). The
    # explicit BEGIN matters now that each file opens a SAVEPOINT: a
    # savepoint issued in autocommit mode starts its own transaction, so
    # its RELEASE would COMMIT after every file instead of nesting.
    conn.execute('BEGIN')

    doc_count = 0
    concept_count = 0
    edge_count = 0
    # WARN-skipped files (surfaced in the summary line). WARNs themselves go
    # to STDERR: every agent-driven ingest channel discards stdout
    # (triggerIngestion spawns with stdio "ignore", runIngestSync pipes only
    # stderr, the post-commit hook's stdout is dropped by git-writer), so a
    # stdout-only WARN vanishes without trace.
    skipped_count = 0

    # rglob('*.md') follows symlinks — a *.md FILE symlink is yielded on
    # every Python version, and on 3.12 patch releases predating the glob
    # reimplementation backport rglob also RECURSES INTO symlinked
    # directories (project floor is >=3.12, any patch) — so anything whose
    # real path escapes the vault would be indexed (#342). is_symlink()
    # alone can't catch the symlinked-dir case (files inside it are not
    # themselves symlinks), hence the containment check on the resolved
    # path.
    vault_real = vault.resolve()

    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        # Skip hidden/excluded dirs
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        try:
            resolved = md_file.resolve()
        except OSError as e:
            print(f'  WARN: skipping {rel} — unresolvable path: {e}', file=sys.stderr)
            skipped_count += 1
            continue
        if not resolved.is_relative_to(vault_real):
            print(f'  WARN: skipping {rel} — resolves outside the vault (symlink)', file=sys.stderr)
            skipped_count += 1
            continue
        # The SKIP_DIRS filter above checks the symlink's OWN path, so a
        # symlink like notes/leak.md -> ../.git/config would pass both it and
        # the containment check while exposing excluded content (e.g. remote
        # URLs with credentials) to the index — re-check the RESOLVED path.
        if any(part in SKIP_DIRS for part in resolved.relative_to(vault_real).parts):
            print(f'  WARN: skipping {rel} — resolves into an excluded directory (symlink)', file=sys.stderr)
            skipped_count += 1
            continue

        # One SAVEPOINT per file (#351): any exception past this point —
        # read, YAML parse, type-guarding, sqlite binding, paper/concept/
        # edge processing — rolls back just this file's writes and the loop
        # moves on. Previously only the read+parse phase was guarded, so an
        # INSERT-phase exception rolled back the single transaction and
        # _run_ingest deleted the partial DB: one bad note = a vault-wide
        # read outage (#296 family).
        conn.execute('SAVEPOINT file_sp')
        try:
            raw_text = md_file.read_text(encoding='utf-8')
            patched_text = patch_frontmatter_flow_hashtags(raw_text)
            post = frontmatter.loads(patched_text)
            meta = post.metadata
            body = post.content

            # Per-file counters, folded into the totals only after RELEASE —
            # a rolled-back file must not inflate the printed counts.
            file_concepts = 0
            file_edges = 0

            # Frontmatter reads (here and in paper_metadata_from_frontmatter) must
            # stay literal meta.get('<field>') / verification.get('<field>')
            # expressions in THIS file — cli/tests/test_frontmatter_contract.py
            # scans this source to pin the read set to
            # schema/frontmatter-contract.json, and indirect or variable-key reads
            # evade that check. Update the contract when adding a field here.

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

            # Title: explicit title > topic > concept key > derive from filename.
            # Each candidate must be a NON-EMPTY STRING to be picked: a truthy
            # non-string (title: [a, b], title: {k: v}) previously flowed into the
            # docs INSERT and aborted the ENTIRE rebuild with a sqlite3 binding
            # error — one bad note must never take down the index (#296 family) —
            # while title: 42 landed with native affinity like the status case
            # (#278). Non-string candidates fall through the chain instead.
            title = next(
                (
                    v
                    for v in (meta.get('title'), meta.get('topic'), meta.get('concept'))
                    if isinstance(v, str) and v
                ),
                title_from_filename(rel.name),
            )

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
                file_concepts += 1

            # Also insert concepts referenced in frontmatter
            for c in concepts:
                conn.execute(
                    'INSERT OR IGNORE INTO concepts (slug, title) VALUES (?, ?)',
                    (c, c.replace('-', ' ').title()),
                )
                # Don't double-count if already counted above
                if not (is_concept and concept_slug is not None and c == concept_slug):
                    file_concepts += 1
                if not is_concept:
                    result = conn.execute(
                        'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                        (doc_id, c, 'references', None),
                    )
                    file_edges += result.rowcount

            # Parse and insert edges
            for edge in parse_connections(body):
                try:
                    conn.execute(
                        'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                        (doc_id, edge['target'], edge['type'], edge['context']),
                    )
                    file_edges += 1
                except sqlite3.IntegrityError:
                    pass

            conn.execute('RELEASE SAVEPOINT file_sp')
            doc_count += 1
            concept_count += file_concepts
            edge_count += file_edges
        except Exception as e:
            # Only CONTENT-shaped failures may be skipped per-file: bad
            # YAML, invalid UTF-8 (#296 — a single corrupt note must never
            # leave the vault in a permanent read outage), type/binding
            # errors (ValueError/OverflowError/InterfaceError), constraint
            # violations (IntegrityError), and unsupported-type binds
            # (ProgrammingError on 3.12+, InterfaceError historically).
            # ENVIRONMENTAL DB failures (disk full, I/O error, corruption —
            # OperationalError and the rest of the DatabaseError family)
            # must fail LOUD instead: WARN-skipping them would stamp a
            # mostly-empty index as complete (user_version set), and
            # readers would serve that truncation as authoritative long
            # after the disk recovers. Re-raising keeps the pre-existing
            # fail-and-unlink path in _run_ingest. This also keeps
            # SQLITE_FULL/IOERR-class errors — whose automatic rollback can
            # destroy the savepoint — away from the ROLLBACK below.
            if isinstance(e, sqlite3.DatabaseError) and not isinstance(
                e, (sqlite3.IntegrityError, sqlite3.ProgrammingError)
            ):
                raise
            try:
                conn.execute('ROLLBACK TO SAVEPOINT file_sp')
                conn.execute('RELEASE SAVEPOINT file_sp')
            except sqlite3.Error:
                # The failure already destroyed the savepoint (automatic
                # rollback). The transaction is gone; propagate the ORIGINAL
                # error — not the cleanup's — so _run_ingest unlinks the DB.
                raise e
            if isinstance(e, UnicodeDecodeError):
                print(f'  WARN: skipping {rel} — invalid UTF-8: {e}', file=sys.stderr)
            else:
                print(f'  WARN: skipping {rel} — {type(e).__name__}: {e}', file=sys.stderr)
            skipped_count += 1
            continue

    conn.execute(
        """
        DELETE FROM concept_aliases
        WHERE duplicate_slug NOT IN (SELECT slug FROM concepts)
           OR canonical_slug NOT IN (SELECT slug FROM concepts)
        """
    )
    # user_version lives in the DB header and is transactional, so the
    # completion marker lands atomically with the data commit (#244). The
    # stamped value is the index schema version (schema/index-contract.json;
    # bumped only on DDL changes to schema.sql) — readers that find a
    # different non-zero version force a rebuild, which IS the migration
    # path (#130 D3). PRAGMA takes no bound parameters; the :d format spec
    # guarantees an int lands in the statement.
    conn.execute(f'PRAGMA user_version = {INDEX_SCHEMA_VERSION:d}')
    conn.commit()
    summary = f'Ingested: {doc_count} docs, {concept_count} concepts, {edge_count} edges'
    if skipped_count:
        summary += f' ({skipped_count} skipped)'
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description='Ingest markdown vault into SQLite')
    parser.add_argument('--vault', required=True, help='Path to vault root')
    parser.add_argument('--db', required=True, help='Path to SQLite database')
    args = parser.parse_args()
    ingest(args.vault, args.db)


if __name__ == '__main__':
    main()
