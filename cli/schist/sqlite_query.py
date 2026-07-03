"""SQLite query helpers — DB access, FTS search, raw query."""

import os
import re
import sqlite3
import sys


# Keep in sync with the read/queryable tables defined in schema.sql.
ALLOWED_TABLES = {'docs', 'concepts', 'edges', 'docs_fts', 'paper_metadata', 'concept_aliases'}
# Keep in sync with mcp-server/src/sqlite-reader.ts REQUIRED_TABLES.
# concept_aliases is included so vaults upgraded from a pre-concept_aliases
# schema (which still have docs + paper_metadata) re-trigger ingest instead of
# failing later with "no such table: concept_aliases". See #224.
REQUIRED_TABLES = {'docs', 'paper_metadata', 'concept_aliases'}


def get_db(vault_path: str, db_path: str | None = None) -> sqlite3.Connection:
    """Open SQLite connection, running ingest if DB missing or empty."""
    if db_path is None:
        db_path = os.path.join(vault_path, '.schist', 'schist.db')

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    needs_ingest = not os.path.exists(db_path) or os.path.getsize(db_path) == 0

    if not needs_ingest:
        conn = sqlite3.connect(db_path)
        try:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not REQUIRED_TABLES.issubset(table_names):
                needs_ingest = True
                conn.close()
        except sqlite3.DatabaseError:
            needs_ingest = True
            conn.close()

    if needs_ingest:
        _run_ingest(vault_path, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _run_ingest(vault_path: str, db_path: str):
    """Run ingestion in-process via schist.ingest.

    Deletes the partial DB file on failure so a subsequent get_db() call
    sees needs_ingest=True and rebuilds from scratch instead of trusting
    an empty schema-only DB. Without this, a failure mid-ingest after
    `executescript` has committed the schema would leave the docs table
    present-but-empty, causing get_db() to silently return an empty DB
    on the next call.
    """
    from .ingest import ingest
    try:
        ingest(vault_path, db_path)
    except Exception:
        # Delete the -wal/-shm siblings too: in WAL mode (#254) the failed
        # ingest's data can live entirely in the -wal, and a surviving -wal
        # would be silently replayed into whatever DB file next appears at
        # this path (e.g. the backup _rebuild_index restores).
        for p in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            try:
                os.unlink(p)
            except OSError:
                pass
        raise


def fts_search(db: sqlite3.Connection, query: str, limit: int = 20,
               status: str | None = None, tags: list[str] | None = None) -> list[dict]:
    """FTS5 search on docs_fts joined to docs."""
    sanitized_query = _sanitize_fts_query(query)
    if not sanitized_query:
        return []

    sql = """
        SELECT d.id, d.title, d.date, d.status,
               snippet(docs_fts, 1, '[', ']', '...', 32) AS snippet
        FROM docs_fts fts
        JOIN docs d ON d.rowid = fts.rowid
        WHERE docs_fts MATCH ?
    """
    params: list = [sanitized_query]

    if status:
        sql += ' AND d.status = ?'
        params.append(status)

    if tags:
        for tag in tags:
            sql += " AND d.tags LIKE ? ESCAPE '\\'"
            params.append(f'%"{_escape_like(tag)}"%')

    sql += ' ORDER BY rank LIMIT ?'
    params.append(limit)

    rows = db.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _sanitize_fts_query(query: str) -> str:
    """Quote each token so FTS5 treats user input as literal search text."""
    tokens = query.split()
    return " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards so a caller value matches literally.

    Intended for embedding in a LIKE pattern used with `ESCAPE '\\'`; the
    backslash is escaped first. Without this, a `_`/`%` in a tag name acts as a
    wildcard and produces false-positive matches. See #225.
    """
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def raw_query(db: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    """Execute a validated SELECT query. Returns {columns, rows}."""
    _validate_sql(sql)
    cursor = db.execute(sql, params)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    rows = [list(row) for row in cursor.fetchall()]
    return {'columns': columns, 'rows': rows}


def _mask_sql_literals_and_comments(sql: str) -> str:
    """Replace SQL string/comment/quoted-identifier contents with spaces.

    Blanks the contents of single-quoted string literals, comments, and all
    three quoted-identifier forms ("...", `...`, [...]) while keeping the
    delimiters, so regex checks see the statement's structure but never
    user-controlled text — a column named "delete" can't false-positive the
    keyword scan, and text inside a string can't mint a fake CTE name or a
    phantom `FROM x` table ref (see #239/#240/#253).

    The output is the same length as the input, character for character.
    _validate_sql relies on that: it locates quoted table refs by their
    delimiter positions here, then reads the identifier text out of the
    ORIGINAL sql by span.
    """
    chars = list(sql)
    i = 0
    state = "normal"

    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""

        if state == "normal":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif ch == "`":
                state = "backtick"
            elif ch == "[":
                state = "bracket"
            elif ch == "-" and nxt == "-":
                chars[i] = chars[i + 1] = " "
                i += 1
                state = "line_comment"
            elif ch == "/" and nxt == "*":
                chars[i] = chars[i + 1] = " "
                i += 1
                state = "block_comment"
        elif state == "single":
            if ch == "'" and nxt == "'":
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == "'":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "double":
            if ch == '"' and nxt == '"':
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == '"':
                state = "normal"
            else:
                chars[i] = " "
        elif state == "backtick":
            if ch == "`" and nxt == "`":
                chars[i] = chars[i + 1] = " "
                i += 1
            elif ch == "`":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "bracket":
            if ch == "]":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "line_comment":
            if ch == "\n":
                state = "normal"
            else:
                chars[i] = " "
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                chars[i] = chars[i + 1] = " "
                i += 1
                state = "normal"
            else:
                chars[i] = " "

        i += 1

    return "".join(chars)


def _validate_sql(sql: str):
    """Ensure SQL is SELECT-only and uses only allowed tables."""
    # Every check runs on the masked view, where all user-controlled text
    # (strings, comments, quoted-identifier contents) is blanked to spaces.
    # NOT .strip()ed: the masked view stays position-aligned with `sql` so
    # quoted table names can be read back out of the original by span.
    cleaned = _mask_sql_literals_and_comments(sql)

    # Accept WITH (CTEs) as well as SELECT — both are read-only and the MCP
    # query_graph guard already allows them. See #223. Leading whitespace is
    # tolerated inline (a leading comment masks to spaces).
    if not re.match(r'\s*(SELECT|WITH)\b', cleaned, re.IGNORECASE):
        print('Error: only SELECT queries are allowed', file=sys.stderr)
        sys.exit(1)

    # Unanchored scan: a semicolon-anchored check (`;\s*KEYWORD`) only caught
    # stacked statements and let single-statement CTE-prefixed DML through,
    # e.g. `WITH x AS (SELECT 1) DELETE FROM docs`. See #239. Safe to scan
    # everywhere because string literals, comments, and quoted identifiers are
    # all blanked in the masked view. ATTACH is deliberately absent: it is
    # non-reserved (legal as a bare column/alias name), and an ATTACH
    # statement can neither follow a CTE nor pass the SELECT/WITH prefix
    # check above, so scanning for it adds only false positives.
    for keyword in ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE'):
        if re.search(rf'\b{keyword}\b', cleaned, re.IGNORECASE):
            print(f'Error: {keyword} statements are not allowed', file=sys.stderr)
            sys.exit(1)

    # REPLACE needs its own check, NOT a slot in the loop above: `REPLACE INTO`
    # is SQLite's `INSERT OR REPLACE` alias — a write verb that can ride a CTE
    # prefix (`WITH x AS (...) REPLACE INTO docs ...`) and carries no FROM/JOIN,
    # so nothing else here catches it. But bare `REPLACE` is also the scalar
    # string function (`SELECT REPLACE(title, 'a', 'b') FROM docs`), which must
    # stay allowed — hence the required `INTO`, not `\bREPLACE\b`. Do not fold
    # this back into the tuple. See adversarial review of #239/#240.
    if re.search(r'\bREPLACE\s+INTO\b', cleaned, re.IGNORECASE):
        print('Error: REPLACE statements are not allowed', file=sys.stderr)
        sys.exit(1)

    # CTE alias names are virtual tables defined by `WITH name AS (...)`; a
    # reference like `FROM name` must not be checked against ALLOWED_TABLES.
    # See #223. A CTE alias may itself be quoted (`WITH "myres" AS (...)`),
    # and the FROM/JOIN check below now matches quoted table refs — so the
    # collector must recognize the same four identifier forms, or a quoted
    # alias is never registered and its quoted `FROM` ref is falsely rejected.
    # Located on the masked view (so string/identifier text can't mint a fake
    # CTE name) but the name is read from the original sql by span, exactly
    # like the table-ref check (masking is length-preserving).
    cte_names = set()
    for m in re.finditer(
        r'(?:`([^`]*)`|\[([^\]]*)\]|"([^"]*)"|\b(\w+))\s+AS\s*\(',
        cleaned,
        re.IGNORECASE,
    ):
        group = next(g for g in (1, 2, 3, 4) if m.group(g) is not None)
        cte_names.add(sql[m.start(group):m.end(group)].lower())

    # Validate table references — capture the identifier after FROM/JOIN
    # whether bare, `backtick`-quoted, [bracket]-quoted, or "double"-quoted.
    # `\w+` alone misses the quoted forms (the quote chars aren't word
    # characters), silently skipping the allow-list check and letting e.g.
    # `FROM \`sqlite_master\`` or `FROM "sqlite_master"` through. See
    # #228/#240. The refs are LOCATED on the masked view — where a string
    # like 'notes from meeting' can't produce a phantom ref — but a quoted
    # identifier's content is blanked there, so the actual name is read from
    # the original sql at the capture-group span (masking is 1:1 on length).
    for m in re.finditer(
        r'\b(?:FROM|JOIN)\s+(?:`([^`]*)`|\[([^\]]*)\]|"([^"]*)"|(\w+))',
        cleaned,
        re.IGNORECASE,
    ):
        group = next(g for g in (1, 2, 3, 4) if m.group(g) is not None)
        table = sql[m.start(group):m.end(group)]
        if table.lower() in ALLOWED_TABLES or table.lower() in cte_names:
            continue
        print(f'Error: table "{table}" is not allowed (allowed: {", ".join(sorted(ALLOWED_TABLES))})', file=sys.stderr)
        sys.exit(1)
