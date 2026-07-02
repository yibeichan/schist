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
    """Replace SQL string/comment contents with spaces before regex checks."""
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
    cleaned = _mask_sql_literals_and_comments(sql)
    cleaned = cleaned.strip()

    # Accept WITH (CTEs) as well as SELECT — both are read-only and the MCP
    # query_graph guard already allows them. See #223.
    if not re.match(r'(SELECT|WITH)\b', cleaned, re.IGNORECASE):
        print('Error: only SELECT queries are allowed', file=sys.stderr)
        sys.exit(1)

    # sqlite3.execute() rejects multiple statements, but catch obvious stacked
    # write attempts here so the CLI reports the same friendly validation style.
    for keyword in ('INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'ATTACH'):
        if re.search(rf';\s*{keyword}\b', cleaned, re.IGNORECASE):
            print(f'Error: {keyword} statements are not allowed', file=sys.stderr)
            sys.exit(1)

    # CTE alias names are virtual tables defined by `WITH name AS (...)`; a
    # reference like `FROM name` must not be checked against ALLOWED_TABLES.
    # See #223.
    cte_names = {m.lower() for m in re.findall(r'\b(\w+)\s+AS\s*\(', cleaned, re.IGNORECASE)}

    # Validate table references — capture the identifier after FROM/JOIN whether
    # bare, `backtick`-quoted, or [bracket]-quoted. `\w+` alone misses the quoted
    # forms (the quote chars aren't word characters), silently skipping the
    # allow-list check and letting e.g. `FROM \`sqlite_master\`` through. See #228.
    table_refs = re.findall(
        r'\b(?:FROM|JOIN)\s+(?:`([^`]+)`|\[([^\]]+)\]|(\w+))',
        cleaned,
        re.IGNORECASE,
    )
    for backtick, bracket, bare in table_refs:
        table = backtick or bracket or bare
        if table.lower() in ALLOWED_TABLES or table.lower() in cte_names:
            continue
        print(f'Error: table "{table}" is not allowed (allowed: {", ".join(sorted(ALLOWED_TABLES))})', file=sys.stderr)
        sys.exit(1)
