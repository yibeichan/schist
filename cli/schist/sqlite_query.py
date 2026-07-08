"""SQLite query helpers — DB access, FTS search, raw query."""

import os
import re
import sqlite3
import sys

from .index_contract import INDEX_SCHEMA_VERSION, REQUIRED_TABLES, TABLES

# Both table sets are single-sourced from schema/index-contract.json via
# index_contract.py — the same contract mcp-server/src/sqlite-reader.ts
# consumes — so the two languages can no longer drift apart (#339, #130 D3).
ALLOWED_TABLES = TABLES


def get_db(vault_path: str, db_path: str | None = None) -> sqlite3.Connection:
    """Open SQLite connection, running ingest if DB missing or empty."""
    if db_path is None:
        db_path = os.path.join(vault_path, '.schist', 'schist.db')

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    needs_ingest = not os.path.exists(db_path) or os.path.getsize(db_path) == 0

    # Note on WAL (#310): when an open reader blocks the writer's close-time
    # checkpoint, the whole index legitimately lives in the -wal sibling and
    # the main file stays a header-only page. That state passes the size
    # check above (the journal_mode=WAL transition materializes the 4 KB
    # header immediately, so the main file is never 0 bytes with a populated
    # WAL), and the connection below replays the WAL, so the table check sees
    # the real schema. Pinned by a regression test.
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
            else:
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    if not conn.execute(
                        'SELECT EXISTS(SELECT 1 FROM docs)'
                    ).fetchone()[0]:
                        # SIGKILL during ingest commits the schema
                        # (executescript auto-commits) but rolls back the data
                        # transaction, leaving valid empty tables that the
                        # check above accepts. Ingest stamps
                        # INDEX_SCHEMA_VERSION atomically with the data, so
                        # version 0 plus an empty docs table means the ingest
                        # never completed (#244). Pre-marker DBs with rows
                        # keep version 0 and are left alone — for them the
                        # required-tables check above is the only gate.
                        needs_ingest = True
                elif version != INDEX_SCHEMA_VERSION:
                    # The DB was completed by a different schema.sql
                    # generation (#130 D3). The index is disposable, so
                    # rebuild IS the migration path — no ALTER migrations.
                    # Skew-free by construction: _run_ingest imports
                    # schist.ingest in-process, so the version this reader
                    # expects is the version that ingest will stamp.
                    needs_ingest = True
        except sqlite3.DatabaseError:
            needs_ingest = True
        finally:
            conn.close()

    if needs_ingest:
        try:
            _run_ingest(vault_path, db_path)
        except sqlite3.OperationalError as e:
            # Concurrent-writer race (#330): mid-ingest, another process's DB
            # legitimately looks incomplete (user_version=0, empty docs), so
            # this reader triggers a competing heal-ingest. If the writer's
            # transaction outlasts the busy timeout, that ingest fails with
            # "database is locked". That is contention, not corruption — fall
            # through and open the existing DB (in WAL mode readers see the
            # last committed snapshot; the owning writer completes the index).
            # Any other OperationalError, or a locked error with no DB file to
            # fall back to, still propagates.
            if not _is_db_locked_error(e) or not os.path.exists(db_path):
                raise
            # Don't fall through silently: an empty/stale result during
            # contention is indistinguishable from a genuinely empty vault
            # without this. Tells the operator to retry rather than trust it.
            print(
                "Warning: index busy — another writer is rebuilding it; "
                "results may be incomplete, retry shortly.",
                file=sys.stderr,
            )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _is_db_locked_error(exc: BaseException) -> bool:
    """True for SQLITE_BUSY/SQLITE_LOCKED contention errors.

    sqlite3 maps both to OperationalError with "database is locked" /
    "database table is locked" messages; there is no stable errno on the
    exception across supported Python versions, so match on the message.
    """
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


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
    except Exception as e:
        if _is_db_locked_error(e):
            # SQLITE_BUSY/SQLITE_LOCKED (#330): another writer — typically a
            # concurrent post-commit ingest — owns the DB right now. The file
            # is NOT a broken artifact; unlinking it here would yank db+wal
            # out from under that live writer (its rebuilt index would land
            # in a deleted inode and the next reader would rebuild yet
            # again). Leave the files alone and let the caller decide.
            raise
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


def _query_authorizer(denied: list):
    """Build a SQLite authorizer enforcing the read-only table boundary.

    The regex checks in _validate_sql re-derive SQLite's table-reference
    grammar and always trail it — comma joins (`FROM docs, sqlite_master`),
    no-space quoting (`FROM"sqlite_master"`), and parenthesized refs
    (`FROM (sqlite_master)`) all bypassed the allow-list (the #223 → #228 →
    #240 → #306 lineage). The authorizer runs inside prepare, so it sees
    every table the engine actually resolves, however the SQL spelled it.
    The regex pass stays as the friendly-error fast path; this is the
    backstop that makes the boundary real.

    CTE names need no special-casing: reads through a CTE are reported
    against the underlying real table (the CTE name only appears in the
    authorizer's inner-most-trigger-or-view argument).

    Denials are recorded in `denied` as (action, arg1) so raw_query can
    turn the engine's terse "not authorized" into the fast path's error
    style.
    """
    def authorize(action, arg1, arg2, db_name, trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                      sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or '').lower()
            # docs_fts_* are the FTS5 shadow tables (docs_fts_idx, _data,
            # _docsize, _config); MATCH/snippet/bm25 read them internally,
            # so they must be readable wherever docs_fts itself is.
            if table in ALLOWED_TABLES or table.startswith('docs_fts_'):
                return sqlite3.SQLITE_OK
        elif action == sqlite3.SQLITE_PRAGMA and arg1 == 'data_version':
            # FTS5 reads `PRAGMA data_version` internally on every MATCH to
            # detect external-content changes; denying it breaks all FTS
            # queries. Read-only, and a user-typed `PRAGMA ...` statement
            # never reaches the engine anyway (_validate_sql requires a
            # SELECT/WITH prefix).
            return sqlite3.SQLITE_OK
        denied.append((action, arg1))
        return sqlite3.SQLITE_DENY

    return authorize


def raw_query(db: sqlite3.Connection, sql: str, params: tuple = ()) -> dict:
    """Execute a validated SELECT query. Returns {columns, rows}."""
    _validate_sql(sql)

    denied: list = []
    db.set_authorizer(_query_authorizer(denied))
    try:
        cursor = db.execute(sql, params)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [list(row) for row in cursor.fetchall()]
    except sqlite3.DatabaseError:
        if denied:
            action, name = denied[0]
            if action == sqlite3.SQLITE_READ:
                print(f'Error: table "{name}" is not allowed (allowed: {", ".join(sorted(ALLOWED_TABLES))})', file=sys.stderr)
            else:
                print('Error: only SELECT queries are allowed', file=sys.stderr)
            sys.exit(1)
        raise
    finally:
        # Clear the hook so later statements on this connection (ingest,
        # fts_search) run unrestricted. Passing None requires Python >= 3.11
        # (on 3.10 it failed to remove the hook, leaving the connection
        # denying everything); the cli package floor is 3.12.
        db.set_authorizer(None)
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
    # The optional `\([^)]*\)` matches a CTE column list — `WITH cnt(x) AS
    # (...)` — which recursive CTEs use almost universally; without it the
    # alias is never collected and `FROM cnt` is falsely rejected (#306).
    # Over-matching here only loosens the fast path: the authorizer in
    # raw_query still enforces the real table boundary.
    cte_names = set()
    for m in re.finditer(
        r'(?:`([^`]*)`|\[([^\]]*)\]|"([^"]*)"|\b(\w+))\s*(?:\([^)]*\))?\s+AS\s*\(',
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
