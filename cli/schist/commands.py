"""schist CLI commands."""

import json
import os
import sys
from datetime import date
from pathlib import Path

from . import git_ops, markdown_io, sqlite_query


def add(args, vault_path: str, db_path: str):
    """Create a new note in the vault."""
    title = args.title
    body = args.body
    if body is None and not sys.stdin.isatty():
        body = sys.stdin.read()
    if body is None:
        body = ''

    slug = markdown_io.slugify(title)
    filename = f'{date.today().isoformat()}-{slug}.md'
    dest_dir = os.path.join(vault_path, args.directory)
    os.makedirs(dest_dir, exist_ok=True)
    filepath = os.path.join(dest_dir, filename)

    fm = {'title': title, 'date': date.today().isoformat(), 'status': args.status}
    if args.tags:
        fm['tags'] = [t.strip() for t in args.tags.split(',')]
    if args.concepts:
        fm['concepts'] = [c.strip() for c in args.concepts.split(',')]

    markdown_io.write_note(filepath, fm, body)

    rel_path = os.path.relpath(filepath, vault_path)
    ok, output = git_ops.commit(vault_path, f'add: {rel_path}', [rel_path])
    if not ok:
        print(f'Warning: git commit failed: {output}', file=sys.stderr)

    print(rel_path)


def link(args, vault_path: str, db_path: str):
    """Add a connection between two documents."""
    source_path = os.path.join(vault_path, args.source)
    if not os.path.exists(source_path):
        print(f'Error: source not found: {args.source}', file=sys.stderr)
        sys.exit(1)

    markdown_io.append_connection(source_path, args.link_type, args.target, args.context)

    ok, output = git_ops.commit(vault_path, f'link: {args.source} -{args.link_type}-> {args.target}', [args.source])
    if not ok:
        print(f'Warning: git commit failed: {output}', file=sys.stderr)

    print(f'Linked: {args.source} -{args.link_type}-> {args.target}')


def assign_domain(args, vault_path: str, db_path: str):
    """Assign a research domain to a note."""
    import yaml

    note_path = os.path.join(vault_path, args.id)
    if not os.path.exists(note_path):
        print(f'Error: note not found: {args.id}', file=sys.stderr)
        sys.exit(1)

    # Validate domain exists in vault.yaml
    vault_yaml = os.path.join(vault_path, 'vault.yaml')
    if os.path.exists(vault_yaml):
        with open(vault_yaml, encoding='utf-8') as f:
            vault_config = yaml.safe_load(f) or {}
        valid_domains = vault_config.get('domains', [])
        if valid_domains and args.domain not in valid_domains:
            print(f'Error: domain "{args.domain}" not in vault.yaml. Valid domains: {valid_domains}', file=sys.stderr)
            sys.exit(1)

    # Read, update, and write back
    note = markdown_io.read_note(note_path)
    fm = note['frontmatter']
    fm['domain'] = args.domain
    markdown_io.write_note(note_path, fm, note['body'])

    ok, output = git_ops.commit(vault_path, f'domain: {args.id} -> {args.domain}', [args.id])
    if not ok:
        print(f'Warning: git commit failed: {output}', file=sys.stderr)

    print(f'Assigned domain "{args.domain}" to {args.id}')


def search(args, vault_path: str, db_path: str):
    """Full-text search across vault documents."""
    db = sqlite_query.get_db(vault_path, db_path)
    tags = [t.strip() for t in args.tags.split(',')] if args.tags else None
    results = sqlite_query.fts_search(db, args.query, limit=args.limit, status=args.status, tags=tags)
    db.close()

    if not results:
        print('No results.')
        return

    for r in results:
        parts = [r['id'], r['title'] or '', r['date'] or '', r['status'] or '', r['snippet'] or '']
        print(' | '.join(parts))


def query(args, vault_path: str, db_path: str):
    """Run a raw SELECT query."""
    db = sqlite_query.get_db(vault_path, db_path)
    result = sqlite_query.raw_query(db, args.sql)
    db.close()

    if args.as_json:
        rows = [dict(zip(result['columns'], row)) for row in result['rows']]
        print(json.dumps(rows, indent=2))
        return

    if not result['rows']:
        print('(no rows)')
        return

    # Pretty-print as table
    cols = result['columns']
    widths = [len(c) for c in cols]
    for row in result['rows']:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    header = ' | '.join(c.ljust(widths[i]) for i, c in enumerate(cols))
    sep = '-+-'.join('-' * w for w in widths)
    print(header)
    print(sep)
    for row in result['rows']:
        print(' | '.join(str(v).ljust(widths[i]) for i, v in enumerate(row)))


def build(args, vault_path: str, db_path: str):
    """Build graph.json and search-index.json from SQLite."""
    db = sqlite_query.get_db(vault_path, db_path)
    out_dir = args.out or os.path.join(vault_path, '.schist', 'data')
    os.makedirs(out_dir, exist_ok=True)

    # Build graph
    nodes = []
    for row in db.execute('SELECT id, title, status, tags FROM docs').fetchall():
        tags = json.loads(row['tags']) if row['tags'] else []
        nodes.append({'id': row['id'], 'title': row['title'], 'type': 'doc', 'tags': tags})
    for row in db.execute('SELECT slug, title, tags FROM concepts').fetchall():
        tags = json.loads(row['tags']) if row['tags'] else []
        nodes.append({'id': row['slug'], 'title': row['title'], 'type': 'concept', 'tags': tags})

    edges = []
    for row in db.execute('SELECT source, target, type, context FROM edges').fetchall():
        edges.append({'source': row['source'], 'target': row['target'], 'type': row['type'], 'context': row['context']})

    graph_path = os.path.join(out_dir, 'graph.json')
    with open(graph_path, 'w') as f:
        json.dump({'nodes': nodes, 'edges': edges}, f, indent=2)

    # Build search index
    documents = []
    for row in db.execute('SELECT id, title, body, tags FROM docs').fetchall():
        tags = json.loads(row['tags']) if row['tags'] else []
        documents.append({'id': row['id'], 'title': row['title'], 'body': row['body'][:500], 'tags': tags})

    index_path = os.path.join(out_dir, 'search-index.json')
    with open(index_path, 'w') as f:
        json.dump({'fields': ['id', 'title', 'body', 'tags'], 'documents': documents}, f, indent=2)

    db.close()

    g_size = os.path.getsize(graph_path)
    i_size = os.path.getsize(index_path)
    print(f'graph.json: {g_size} bytes')
    print(f'search-index.json: {i_size} bytes')


def context(args, vault_path: str, db_path: str):
    """Print vault context summary."""
    db = sqlite_query.get_db(vault_path, db_path)

    doc_count = db.execute('SELECT count(*) FROM docs').fetchone()[0]
    concept_count = db.execute('SELECT count(*) FROM concepts').fetchone()[0]
    edge_count = db.execute('SELECT count(*) FROM edges').fetchone()[0]

    print(f'Documents: {doc_count}')
    print(f'Concepts:  {concept_count}')
    print(f'Edges:     {edge_count}')

    if args.depth in ('standard', 'full'):
        print('\n--- Last 10 documents ---')
        for row in db.execute('SELECT id, title, date, status FROM docs ORDER BY date DESC LIMIT 10').fetchall():
            print(f'  {row["date"] or "no-date"} | {row["title"]} [{row["status"]}]')

        print('\n--- Top 10 concepts by edge count ---')
        rows = db.execute("""
            SELECT c.slug, c.title, count(e.id) AS cnt
            FROM concepts c
            LEFT JOIN edges e ON e.source = c.slug OR e.target = c.slug
            GROUP BY c.slug
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        for row in rows:
            print(f'  {row["slug"]}: {row["title"]} ({row["cnt"]} edges)')

    if args.depth == 'full':
        print('\n--- Tag frequency ---')
        tag_freq: dict[str, int] = {}
        for row in db.execute('SELECT tags FROM docs WHERE tags IS NOT NULL').fetchall():
            for tag in json.loads(row['tags']):
                tag_freq[tag] = tag_freq.get(tag, 0) + 1
        for tag, count in sorted(tag_freq.items(), key=lambda x: -x[1]):
            print(f'  {tag}: {count}')

        print('\n--- Last 10 edges ---')
        for row in db.execute('SELECT source, target, type, context FROM edges ORDER BY created_at DESC LIMIT 10').fetchall():
            ctx = f' "{row["context"]}"' if row['context'] else ''
            print(f'  {row["source"]} -{row["type"]}-> {row["target"]}{ctx}')

    db.close()


def schema(args, vault_path: str, db_path: str):
    """Print or validate schema."""
    import yaml

    # Find schema config
    vault_schema = os.path.join(vault_path, 'schist.yaml')
    default_schema = os.path.join(Path(__file__).resolve().parent.parent.parent, 'schema', 'default.yaml')

    schema_path = vault_schema if os.path.exists(vault_schema) else default_schema
    if not os.path.exists(schema_path):
        print('Error: no schema found', file=sys.stderr)
        sys.exit(1)

    with open(schema_path) as f:
        cfg = yaml.safe_load(f)

    if not args.validate:
        print(yaml.dump(cfg, default_flow_style=False).rstrip())
        return

    # Validate: check every .md in vault for title in frontmatter
    violations = []
    vault = Path(vault_path)
    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        if any(part.startswith('.') for part in rel.parts):
            continue
        # Skip non-note files (README, SCHEMA, TAGS, etc. at root level without frontmatter)
        try:
            note = markdown_io.read_note(str(md_file))
        except Exception:
            violations.append(f'{rel}: failed to parse')
            continue
        fm = note['frontmatter']
        if not fm.get('title') and not fm.get('topic') and not fm.get('concept'):
            violations.append(f'{rel}: missing title/topic/concept in frontmatter')

    if violations:
        print(f'{len(violations)} violation(s):')
        for v in violations:
            print(f'  {v}')
        sys.exit(1)
    else:
        print('All documents valid.')
