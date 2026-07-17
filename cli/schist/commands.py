"""schist CLI commands."""

import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import git_ops, markdown_io, sqlite_query
from .ingest import _normalize_concept_slug, _normalize_tag


def _reject_escaping_relpath(value: str, what: str):
    """Reject an absolute or `..`-carrying path argument before it is joined
    onto the vault root (#411). os.path.join DISCARDS the vault prefix when
    the second argument is absolute, so `--dir /tmp/x` wrote wherever it
    pointed with only a later git-commit warning as a symptom. Same textual
    rule as MCP create_note's directory guard (`includes("..") ||
    path.isAbsolute`) so the two writers reject the same shapes; the
    substring `..` test is deliberately broad for that parity.
    """
    if not value or '..' in value or os.path.isabs(value):
        print(
            f'Error: invalid {what}: must be relative and not contain ..',
            file=sys.stderr,
        )
        sys.exit(1)


def _assert_resolves_inside_vault(vault_path: str, candidate_path: str, what: str):
    """Fail unless candidate_path physically resolves inside the vault (#411).

    The textual pre-check above catches `..`/absolute escapes; this catches
    the physical one — a symlink inside the vault pointing outside it, which
    no string check can see. The CLI sibling of MCP's resolvesInsideVault
    (add_connection) and withWriteLock's assertResolvesInside (#323).
    os.path.realpath, never Path.resolve() — resolve() raises RuntimeError
    (not OSError) on a symlink loop on Python ≤3.12 (#401) — and any error
    resolving counts as OUTSIDE, never as safe.
    """
    try:
        real_root = os.path.realpath(vault_path)
        real_candidate = os.path.realpath(candidate_path)
    except (OSError, RuntimeError) as e:
        print(f'Error: could not resolve {what} path: {e}', file=sys.stderr)
        sys.exit(1)
    if real_candidate != real_root and not real_candidate.startswith(real_root + os.sep):
        print(
            f'Error: {what} resolves outside the vault (symlink?)',
            file=sys.stderr,
        )
        sys.exit(1)


@contextmanager
def _vault_write_lock(vault_path: str):
    """Serialize CLI vault writes across processes (#412).

    Exclusive advisory flock on <vault>/.schist/cli-write.lock, held across
    a read-modify-write: append_connection is read → insert → whole-file
    rewrite, so two unserialized `schist link` calls on the same source both
    read the same base content and the loser's edge silently vanishes. The
    lock file lives under .schist/ (gitignored runtime state, like
    schist.db) and is never deleted — unlinking a flock'd path is a classic
    unlock-bypass race. flock is POSIX-only, like the rest of the deployment
    (Mac/Linux spokes, Pi hub). NOTE: this serializes CLI↔CLI only; the MCP
    server serializes its own writes in-process and does not take this lock
    — the cross-writer CLI↔MCP window is the remaining gap noted in #412.
    """
    import fcntl

    lock_dir = os.path.join(vault_path, '.schist')
    os.makedirs(lock_dir, exist_ok=True)
    fd = os.open(os.path.join(lock_dir, 'cli-write.lock'), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_default_config() -> dict:
    """Load the packaged default.yaml (the canonical default `schist init`
    copies into new vaults)."""
    import yaml

    default_schema = Path(__file__).resolve().parent / 'default.yaml'
    if not default_schema.exists():
        return {}
    return yaml.safe_load(default_schema.read_text()) or {}


def _load_schema_config(vault_path: str) -> dict:
    """Load the vault's schist.yaml, falling back to the packaged default.

    Used by link() (via _connection_types) so the CLI's write-side vocabulary
    check reads the same config the MCP server and ingest validate against.
    """
    import yaml

    vault_schema = Path(vault_path) / 'schist.yaml'
    if vault_schema.exists():
        # A syntactically invalid file (stray tab, unclosed quote — the same
        # hand-edited scenario as the non-mapping case below) or an unreadable
        # one must die with a clean message, not a raw traceback. Do NOT fall
        # back to the packaged default here: a corrupt config silently
        # widening the vocabulary is worse than stopping.
        try:
            parsed = yaml.safe_load(vault_schema.read_text())
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
            print(f'Error: could not read schist.yaml: {e}', file=sys.stderr)
            sys.exit(1)
        # A non-mapping top level (hand-edited list/scalar) must degrade to
        # "no keys" — MCP's loadVaultConfig reads raw[key] as undefined on a
        # non-mapping and falls back to defaults; .get() on a list would
        # instead crash every command that resolves a vocabulary.
        return parsed if isinstance(parsed, dict) else {}
    return _load_default_config()


def _connection_types(vault_path: str) -> list:
    """Resolve the connection-type vocabulary for a vault, mirroring the MCP
    server's loadVaultConfig/getStringList semantics (mcp-server/src/tools.ts)
    so `schist link` and `add_connection` accept exactly the same set:

    - a `connection_types` LIST in the vault's schist.yaml is used verbatim,
      including an explicit empty list — which therefore rejects every type,
      exactly as MCP's `connectionTypes.includes()` does;
    - an ABSENT or non-list `connection_types` (e.g. a partial hand-edited
      schist.yaml) falls back to the packaged default vocabulary rather than
      silently disabling the check — a missing key must not reopen #363.

    Empty/falsy entries are dropped to match getStringList's .filter(Boolean).
    """
    ct = _load_schema_config(vault_path).get('connection_types')
    if not isinstance(ct, list):
        ct = _load_default_config().get('connection_types') or []
    # Coerce BEFORE filtering, like getStringList's .map(String).filter(Boolean)
    # — filtering raw values drops falsy-but-stringifiable entries (an unquoted
    # 0 in YAML) that MCP accepts, skewing the two writers' vocabularies.
    # CAVEAT: str() only matches String() for strings and integers. Non-string
    # scalars diverge (None→'None' vs 'null', True→'True' vs 'true', a YAML
    # float 1.0→'1.0' vs '1') — but any such junk with whitespace is dropped
    # by the token filter below, and whitespace-free junk is at worst a
    # harmless extra token.
    # #413: an entry serializes into `- {type}: target`, so it must satisfy
    # the same single-token rule as a target — a whitespace-carrying entry
    # passes the membership check and then writes a line that never
    # round-trips (dead edge) or splits on read (forged entry): the
    # config-reachable version of the #398/#408 target holes. Warn-and-drop,
    # not hard-error — a junk entry must not brick every command; a dropped
    # type is simply rejected at write time by the membership check. The
    # empty string is dropped SILENTLY (getStringList .filter(Boolean)
    # parity). Pinned against MCP by schema/vocab-token-parity.json.
    kept = []
    for x in ct:
        s = str(x)
        if not s:
            continue
        if not markdown_io.is_round_trippable_token(s):
            print(
                f'Warning: ignoring connection_types entry {s!r}: not a '
                f'single whitespace-free token — a connection line using it '
                f'could never round-trip through the parser',
                file=sys.stderr,
            )
            continue
        kept.append(s)
    return kept


def _statuses(vault_path: str) -> list:
    """Resolve the status vocabulary for a vault, with the same fallback
    semantics as _connection_types (see its docstring): a `statuses` LIST in
    the vault's schist.yaml is used verbatim (including an explicit empty
    list, which rejects every status); an absent or non-list key falls back
    to the packaged default rather than silently disabling the check.
    """
    st = _load_schema_config(vault_path).get('statuses')
    if not isinstance(st, list):
        st = _load_default_config().get('statuses') or []
    return [s for x in st if (s := str(x))]


def add(args, vault_path: str, db_path: str):
    """Create a new note in the vault."""
    # Validate the status against the configured vocabulary before writing
    # (#407). MCP create_note has enforced this server-side since #276; an
    # unchecked status is indexed verbatim by ingest, hidden from every
    # --status filter, and blocks later MCP-side repair via update_note.
    # Like MCP, the bare default resolves to 'draft' only when the vault's
    # vocabulary includes it, else to the first configured status — so a
    # default-status `schist add` can't slip an out-of-vocabulary value
    # onto disk in a vault whose custom statuses exclude 'draft'.
    statuses = _statuses(vault_path)
    status = args.status
    if status is None:
        status = 'draft' if 'draft' in statuses else (statuses[0] if statuses else 'draft')
    if status not in statuses:
        allowed = ', '.join(statuses) if statuses else '(none configured)'
        print(
            f"Error: status '{status}' is not in the configured statuses: {allowed}",
            file=sys.stderr,
        )
        sys.exit(1)

    title = args.title
    body = args.body
    if body is None and not sys.stdin.isatty():
        body = sys.stdin.read()
    if body is None:
        body = ''

    slug = markdown_io.slugify(title)
    # An all-symbol title slugs to '' and would mint `<date>-.md` — a note id
    # MCP create_note refuses ("Title must contain at least one alphanumeric
    # character", the guard right beside the #118 one below). Same wording,
    # same accept-set.
    if not slug:
        print(
            'Error: title must contain at least one alphanumeric character',
            file=sys.stderr,
        )
        sys.exit(1)
    # Reject a title that starts with a YYYY-MM-DD date prefix, like MCP
    # create_note (#118): the filename already prefixes the date, so accepting
    # it mints a doubled-date note id the MCP write path refuses — a
    # cross-writer accept-set skew. Regex kept byte-identical to tools.ts for
    # #338 parity; the `^-*` branch is unreachable HERE (slugify strips edge
    # dashes) and live in TS (trim() leaves the dash a whitespace-leading
    # title slugs to).
    if re.match(r'^-*\d{4}-\d{2}-\d{2}(-|$)', slug):
        print(
            'Error: title must not start with a YYYY-MM-DD date prefix — '
            'the filename already prefixes the date.',
            file=sys.stderr,
        )
        sys.exit(1)

    # #411: containment for --dir. Textual check BEFORE the join (an absolute
    # value makes os.path.join discard the vault prefix entirely), physical
    # check AFTER makedirs (an in-vault symlink pointing outside is invisible
    # to any string test — resolve what's actually on disk). MCP has enforced
    # both halves since #323; the CLI had neither.
    directory = str(args.directory)
    _reject_escaping_relpath(directory, 'directory')
    today_str = date.today().isoformat()
    dest_dir = os.path.join(vault_path, directory)
    os.makedirs(dest_dir, exist_ok=True)
    _assert_resolves_inside_vault(vault_path, dest_dir, 'directory')

    # Same-day same-title collision guard (#406), race-safe like the MCP
    # sibling (#408): candidate names in priority order — base, then an
    # HH-MM-SS suffix (local time, consistent with the local date.today() in
    # the filename), then a counter. The lexists pre-probe below only picks a
    # nice starting name (and steps AROUND a dangling symlink instead of
    # writing the note through it); the AUTHORITATIVE guard is the O_EXCL
    # ('x') write in the loop after it — a probe-then-open('w') is a TOCTOU,
    # so a concurrent `schist add` (or the MCP server) taking the same name
    # between probe and write would silently truncate the winner. open('x')
    # also refuses to follow any symlink, dangling included, atomically.
    def _candidates():
        yield f'{today_str}-{slug}.md'
        time_suffix = datetime.now().strftime('%H-%M-%S')
        yield f'{today_str}-{slug}-{time_suffix}.md'
        counter = 2
        while True:
            yield f'{today_str}-{slug}-{time_suffix}-{counter}.md'
            counter += 1

    candidates = _candidates()

    def _next_free():
        return next(
            c for c in candidates
            if not os.path.lexists(os.path.join(dest_dir, c))
        )

    filename = _next_free()

    fm = {'title': title, 'date': today_str, 'status': status}
    # Normalize on the write side so on-disk frontmatter matches what ingest
    # indexes (#399). MCP create_note already does this (#289 tags, #302
    # concepts); the CLI is the human-facing write path and must not diverge.
    # Reuse ingest's own normalizers so the two paths can never drift.
    if args.tags:
        fm['tags'] = [tag for t in args.tags.split(',') if (tag := _normalize_tag(t))]
    if args.concepts:
        fm['concepts'] = [
            slug for c in args.concepts.split(',') if (slug := _normalize_concept_slug(c))
        ]
    if args.file_ref:
        fm['file_ref'] = args.file_ref

    # O_EXCL write with retry — the authoritative collision guard (see the
    # candidate-generator comment above; mirrors MCP create_note's EEXIST
    # loop). A concurrent writer taking the probed name between probe and
    # write makes open('x') raise FileExistsError; advance to the next free
    # candidate instead of truncating. Capped like MCP's
    # MAX_COLLISION_RETRIES so a wedged filesystem can't spin forever.
    max_collision_retries = 50
    for attempt in range(max_collision_retries + 1):
        filepath = os.path.join(dest_dir, filename)
        try:
            markdown_io.write_note(filepath, fm, body, exclusive=True)
            break
        except FileExistsError:
            if attempt == max_collision_retries:
                print(
                    f"Error: could not find a free filename for "
                    f"'{today_str}-{slug}*.md' after {max_collision_retries} "
                    f"collision retries — extreme concurrent add contention "
                    f"on this title; retry, or vary the title.",
                    file=sys.stderr,
                )
                sys.exit(1)
            filename = _next_free()

    rel_path = os.path.relpath(filepath, vault_path)
    ok, output = git_ops.commit(vault_path, f'add: {rel_path}', [rel_path])
    if not ok:
        print(f'Warning: git commit failed: {output}', file=sys.stderr)
    elif output.startswith(git_ops.HOOK_STALL_WARNING_PREFIX):
        # The commit landed (#364) — succeed, but tell the user the index
        # may lag the write.
        print(f'Warning: {output}', file=sys.stderr)

    print(rel_path)


def link(args, vault_path: str, db_path: str):
    """Add a connection between two documents."""
    # Validate the connection type against the configured vocabulary before
    # writing (#397). MCP add_connection has enforced this since #304; the CLI
    # writing unchecked types was the root cause of #363 (notes made
    # un-editable via update_note by out-of-vocabulary edges). _connection_types
    # mirrors the MCP server's resolution so both accept the same set.
    connection_types = _connection_types(vault_path)
    if args.link_type not in connection_types:
        allowed = ', '.join(connection_types) if connection_types else '(none configured)'
        print(
            f"Error: connection type '{args.link_type}' is not in the configured "
            f"connection_types: {allowed}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Reject a target carrying any str.splitlines() boundary before writing
    # (#405) — the CLI sibling of MCP add_connection's #398 guard. Such a
    # target serializes into `- type: target` and splits back into MORE than
    # one line on read, so ingest indexes a forged extra edge nobody wrote.
    # The context field is the parallel vector; it is flattened (not
    # rejected) by sanitize_context inside append_connection, matching the
    # MCP split of reject-target / sanitize-context.
    if markdown_io.contains_line_boundary(str(args.target)):
        print(
            'Error: target must not contain line-break characters '
            '(it would forge extra connection entries on read)',
            file=sys.stderr,
        )
        sys.exit(1)

    # A target must round-trip through CONNECTION_RE, whose target group is
    # ONE non-whitespace run (the pinned SLUG_WS_CHARS class). An empty
    # target lets the quoted context slide into the target slot on read —
    # indexing an edge whose target is caller-chosen context text — and a
    # whitespace-carrying target (space, tab, NBSP) writes a line the parser
    # never matches: `link` prints success and commits, but no edge is ever
    # indexed. Reject both instead of writing a dead or forgeable line.
    if not markdown_io.is_round_trippable_token(str(args.target)):
        print(
            'Error: target must be a non-empty token without whitespace '
            '(the connection line could not round-trip through the parser)',
            file=sys.stderr,
        )
        sys.exit(1)

    # A '['-leading target ([moltbook], [[wiki-note]]) passes both guards
    # above and matches CONNECTION_RE, but ingest.parse_connections SKIPS
    # bracket references — `link` would print success and commit while the
    # edge is never indexed, the same silent-no-op the round-trip guard
    # closes. Mirrors MCP validateConnectionTarget; the read-side TS/Python
    # divergence on pre-existing bracket lines is tracked in #415.
    if str(args.target).startswith('['):
        print(
            "Error: target must not start with '[' (bracket references are "
            'skipped by the indexer, so the edge would never be indexed)',
            file=sys.stderr,
        )
        sys.exit(1)

    # #411: same two-layer containment as add --dir — textual check before
    # the join, physical (realpath) check on the existing note so an in-vault
    # symlink pointing outside can't route the append through itself. MCP
    # add_connection has enforced both since #323.
    source_rel = str(args.source)
    _reject_escaping_relpath(source_rel, 'source')
    source_path = os.path.join(vault_path, source_rel)
    if not os.path.exists(source_path):
        print(f'Error: source not found: {args.source}', file=sys.stderr)
        sys.exit(1)
    _assert_resolves_inside_vault(vault_path, source_path, 'source')

    # #412: append_connection is read → insert → whole-file rewrite; two
    # unserialized links on the same source both read the same base content
    # and the loser's edge silently vanishes. Hold the vault write lock
    # across the RMW AND the commit so each commit stages exactly its own
    # append (mirrors MCP holding its write mutex across the git commit).
    with _vault_write_lock(vault_path):
        markdown_io.append_connection(source_path, args.link_type, args.target, args.context)

        ok, output = git_ops.commit(vault_path, f'link: {args.source} -{args.link_type}-> {args.target}', [args.source])
    if not ok:
        print(f'Warning: git commit failed: {output}', file=sys.stderr)
    elif output.startswith(git_ops.HOOK_STALL_WARNING_PREFIX):
        print(f'Warning: {output}', file=sys.stderr)

    print(f'Linked: {args.source} -{args.link_type}-> {args.target}')


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
    # Canonical default ships inside the package — see cli/schist/default.yaml.
    # Path(__file__).parent works for both editable and wheel installs;
    # the legacy <repo>/schema/default.yaml path was removed in the
    # flatten-spoke-dirs refactor (single source of truth).
    default_schema = str(Path(__file__).resolve().parent / 'default.yaml')

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
    # rglob follows file symlinks (any Python) and directory symlinks (<=3.12
    # patch releases before the glob reimplementation), so a symlink escaping
    # the vault would otherwise be read and validated — mixing external state
    # into the vault's health report. Follows the intent of ingest.py's #342
    # containment guard: skip anything whose real path escapes the vault or
    # resolves into a hidden dir (the startswith('.') filter below only
    # inspects the symlink's OWN path, not its target). NOTE: this uses the
    # startswith('.') hidden-dir rule, which is broader than ingest.py's
    # SKIP_DIRS={'.git','.schist'}; the difference is in the safe direction
    # (validate skips more than ingest indexes).
    vault_real = vault.resolve()
    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        if any(part.startswith('.') for part in rel.parts):
            continue
        # Skip escaping/excluded symlinks with a stderr WARN rather than
        # counting them as violations — this keeps the validate exit code
        # clean for legitimate symlinked content (e.g. shared skills).
        # resolve() raises RuntimeError (not OSError) on a symlink LOOP on
        # Python <=3.12 — the project floor — so both must be caught.
        try:
            resolved = md_file.resolve()
        except (OSError, RuntimeError) as e:
            print(f'  WARN: skipping {rel} — unresolvable path: {e}', file=sys.stderr)
            continue
        if not resolved.is_relative_to(vault_real):
            print(f'  WARN: skipping {rel} — resolves outside the vault (symlink)', file=sys.stderr)
            continue
        if any(part.startswith('.') for part in resolved.relative_to(vault_real).parts):
            print(f'  WARN: skipping {rel} — resolves into an excluded directory (symlink)', file=sys.stderr)
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
