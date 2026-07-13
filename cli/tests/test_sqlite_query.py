"""Tests for SQLite query/search helper behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from schist.index_contract import INDEX_SCHEMA_VERSION
from schist.sqlite_query import fts_search, get_db, raw_query


def _connect(path: Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_get_db_reingests_when_required_side_table_missing(tmp_path: Path) -> None:
    """Pre-paper_metadata DBs should self-heal instead of failing at query time."""
    vault = tmp_path / "vault"
    db_path = vault / ".schist" / "schist.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_called_once_with(str(vault), str(db_path))


def test_get_db_keeps_current_db_when_required_tables_exist(tmp_path: Path) -> None:
    """A healthy populated DB with all required tables is kept as-is. (The
    docs row matters since #244: required tables with an empty docs table and
    no completion marker is exactly the SIGKILL-artifact shape and would
    correctly trigger re-ingest.)"""
    vault = tmp_path / "vault"
    db_path = vault / ".schist" / "schist.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE paper_metadata (doc_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE concept_aliases (duplicate_slug TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO docs VALUES ('d1')")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_not_called()


def _vault_db(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    db_path = vault / ".schist" / "schist.db"
    db_path.parent.mkdir(parents=True)
    return vault, db_path


def _create_required_tables(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE paper_metadata (doc_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE concept_aliases (duplicate_slug TEXT PRIMARY KEY)")


def test_get_db_reingests_sigkill_artifact_schema_only_db(tmp_path: Path) -> None:
    """SIGKILL during ingest commits the schema DDL but rolls back the data,
    leaving valid empty tables with user_version=0. get_db must treat that as
    an incomplete ingest instead of silently serving an empty index (#244)."""
    vault, db_path = _vault_db(tmp_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 0")
    _create_required_tables(conn)
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_called_once_with(str(vault), str(db_path))


def test_get_db_keeps_pre_marker_db_with_rows(tmp_path: Path) -> None:
    """DBs built before the user_version marker existed have version 0 but
    real rows — they must NOT be re-ingested on upgrade day (#244)."""
    vault, db_path = _vault_db(tmp_path)

    conn = sqlite3.connect(db_path)
    _create_required_tables(conn)
    conn.execute("INSERT INTO docs VALUES ('d1')")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_not_called()


def test_get_db_keeps_completed_ingest_of_empty_vault(tmp_path: Path) -> None:
    """A vault with no notes legitimately produces zero docs; the completion
    marker distinguishes that from a crashed ingest — no re-ingest loop."""
    vault, db_path = _vault_db(tmp_path)

    conn = sqlite3.connect(db_path)
    _create_required_tables(conn)
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_not_called()


def test_get_db_reingests_stale_schema_version(tmp_path: Path) -> None:
    """A non-zero user_version different from INDEX_SCHEMA_VERSION means the
    index was completed by a different schema.sql generation — the index is
    disposable, so rebuild IS the migration path (#130 D3). Rows present, so
    neither the #244 heal nor the required-tables check would fire; only the
    version check catches this."""
    vault, db_path = _vault_db(tmp_path)

    conn = sqlite3.connect(db_path)
    _create_required_tables(conn)
    conn.execute("INSERT INTO docs VALUES ('d1')")
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_called_once_with(str(vault), str(db_path))


def test_get_db_stale_version_end_to_end_rebuild_restamps_current(tmp_path: Path) -> None:
    """Skew-free by construction: the in-process ingest that get_db triggers
    stamps the same INDEX_SCHEMA_VERSION the reader checked, so one rebuild
    settles the DB — no loop."""
    vault, db_path = _vault_db(tmp_path)
    (vault / "2026-07-06-stale.md").write_text(
        "---\ntitle: Stale\ndate: 2026-07-06\n---\n\nBody.\n", encoding="utf-8"
    )

    db = get_db(str(vault), str(db_path))
    db.close()

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION + 1}")
    # A row not backed by markdown — must vanish when the rebuild re-scans.
    conn.execute(
        "INSERT INTO docs (id, title, body) VALUES ('phantom.md', 'Phantom', 'x')"
    )
    conn.commit()
    conn.close()

    db = get_db(str(vault), str(db_path))
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == INDEX_SCHEMA_VERSION
        ids = {row["id"] for row in db.execute("SELECT id FROM docs").fetchall()}
        assert ids == {"2026-07-06-stale.md"}
    finally:
        db.close()


def test_get_db_opens_wal_resident_index_without_reingest(tmp_path: Path) -> None:
    """Since #254 an open reader can block the writer's close-checkpoint,
    leaving the main file as a header-only page with the entire index in the
    -wal sibling. get_db must open that DB (SQLite replays the WAL) instead
    of re-running a full ingest — the scenario #310 worried about. The
    journal_mode=WAL transition materializes the header page immediately, so
    the main file is never 0 bytes here and the size check passes."""
    import os

    vault, db_path = _vault_db(tmp_path)

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    _create_required_tables(writer)
    writer.execute("INSERT INTO docs VALUES ('d1')")
    writer.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    writer.commit()

    # A read-only connection with an active statement blocks the writer's
    # close-time checkpoint, and a read-only close can't checkpoint either —
    # same technique as test_sync's WAL-resident-index fixture.
    reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cursor = reader.execute("SELECT name FROM sqlite_master")
    writer.close()
    cursor.fetchall()
    reader.close()

    # Precondition: header-only main file, index in the -wal.
    assert os.path.getsize(db_path) <= 4096, (
        "test precondition failed: expected a header-only main file"
    )
    wal = Path(f"{db_path}-wal")
    assert wal.exists() and wal.stat().st_size > 0, (
        "test precondition failed: expected the index to live in the -wal"
    )

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        rows = db.execute("SELECT id FROM docs").fetchall()
        db.close()

    run_ingest.assert_not_called()
    assert [row["id"] for row in rows] == ["d1"]


def test_get_db_reingests_when_concept_aliases_missing(tmp_path: Path) -> None:
    """Vaults upgraded past paper_metadata but before concept_aliases must
    self-heal rather than fail later in add_concept_alias. See #224."""
    vault = tmp_path / "vault"
    db_path = vault / ".schist" / "schist.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE paper_metadata (doc_id TEXT PRIMARY KEY)")
    # concept_aliases intentionally absent
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_called_once_with(str(vault), str(db_path))


def test_validate_sql_allows_blocked_keywords_inside_string_literals() -> None:
    conn = _connect()
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'create-note workflow')")

    result = raw_query(
        conn,
        "SELECT id FROM docs WHERE title LIKE '%create-note%'",
    )

    assert result == {"columns": ["id"], "rows": [["a"]]}


def test_validate_sql_does_not_strip_comment_markers_inside_strings() -> None:
    conn = _connect()
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'foo--bar')")

    result = raw_query(conn, "SELECT id FROM docs WHERE title = 'foo--bar'")

    assert result == {"columns": ["id"], "rows": [["a"]]}


def test_validate_sql_still_rejects_stacked_write_statements(capsys) -> None:
    conn = _connect()
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT id FROM docs; DELETE FROM docs")

    assert "DELETE statements are not allowed" in capsys.readouterr().err


def test_raw_query_allows_concept_aliases_table() -> None:
    conn = _connect()
    conn.execute("""
        CREATE TABLE concept_aliases (
            duplicate_slug TEXT NOT NULL,
            canonical_slug TEXT NOT NULL,
            reason TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (duplicate_slug, canonical_slug)
        )
    """)
    conn.execute(
        "INSERT INTO concept_aliases "
        "(duplicate_slug, canonical_slug, reason, created_by) "
        "VALUES ('ml', 'machine-learning', 'abbreviation', 'agent-a')"
    )

    result = raw_query(
        conn,
        "SELECT duplicate_slug, canonical_slug FROM concept_aliases",
    )

    assert result == {
        "columns": ["duplicate_slug", "canonical_slug"],
        "rows": [["ml", "machine-learning"]],
    }


def test_fts_search_sanitizes_special_syntax_without_traceback() -> None:
    conn = _connect()
    conn.execute("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY,
            title TEXT,
            date TEXT,
            status TEXT,
            tags TEXT,
            body TEXT
        )
    """)
    conn.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED)")
    conn.execute(
        "INSERT INTO docs (rowid, id, title, date, status, tags, body) "
        "VALUES (1, 'notes/a.md', 'Syntax', '2026-06-09', 'draft', '[]', 'literal (unclosed text')"
    )
    conn.execute(
        "INSERT INTO docs_fts (rowid, title, body, tags, scope) "
        "VALUES (1, 'Syntax', 'literal (unclosed text', '[]', 'global')"
    )

    rows = fts_search(conn, "(unclosed")

    assert [row["id"] for row in rows] == ["notes/a.md"]


def test_fts_search_matches_mcp_literal_operator_behavior() -> None:
    conn = _connect()
    conn.execute("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY,
            title TEXT,
            date TEXT,
            status TEXT,
            tags TEXT,
            body TEXT
        )
    """)
    conn.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED)")
    conn.execute(
        "INSERT INTO docs (rowid, id, title, date, status, tags, body) "
        "VALUES (1, 'notes/a.md', 'Foo', '2026-06-09', 'draft', '[]', 'foo only')"
    )
    conn.execute(
        "INSERT INTO docs (rowid, id, title, date, status, tags, body) "
        "VALUES (2, 'notes/b.md', 'Bar', '2026-06-09', 'draft', '[]', 'bar only')"
    )
    conn.execute(
        "INSERT INTO docs_fts (rowid, title, body, tags, scope) "
        "VALUES (1, 'Foo', 'foo only', '[]', 'global')"
    )
    conn.execute(
        "INSERT INTO docs_fts (rowid, title, body, tags, scope) "
        "VALUES (2, 'Bar', 'bar only', '[]', 'global')"
    )

    assert fts_search(conn, "foo OR bar") == []


def _docs_concepts_conn() -> sqlite3.Connection:
    conn = _connect()
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE concepts (slug TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE edges (id INTEGER PRIMARY KEY, target TEXT)")
    return conn


def test_raw_query_accepts_simple_cte() -> None:
    """A WITH (CTE) query is read-only and must validate. See #223."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO concepts (slug, title) VALUES ('a', 'Alpha')")

    result = raw_query(
        conn,
        "WITH ranked AS (SELECT slug, title FROM concepts) "
        "SELECT title FROM ranked ORDER BY slug",
    )

    assert result == {"columns": ["title"], "rows": [["Alpha"]]}


def test_raw_query_accepts_multiple_ctes() -> None:
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO concepts (slug, title) VALUES ('a', 'Alpha')")
    conn.execute("INSERT INTO edges (id, target) VALUES (1, 'a')")

    result = raw_query(
        conn,
        "WITH c AS (SELECT slug, title FROM concepts), "
        "m AS (SELECT target AS slug, COUNT(*) AS n FROM edges GROUP BY target) "
        "SELECT c.title, m.n FROM c JOIN m ON m.slug = c.slug",
    )

    assert result == {"columns": ["title", "n"], "rows": [["Alpha", 1]]}


def test_raw_query_cte_referencing_disallowed_table_still_rejected(capsys) -> None:
    """CTE syntax must not become an escape hatch around ALLOWED_TABLES."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(
            conn,
            "WITH x AS (SELECT name FROM sqlite_master) SELECT * FROM x",
        )

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_rejects_backtick_quoted_table() -> None:
    """Backtick-quoted identifiers must not bypass ALLOWED_TABLES. See #228."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT name FROM `sqlite_master`")


def test_raw_query_rejects_cte_prefixed_delete(capsys) -> None:
    """A CTE followed by DML is a single statement with no semicolon; the
    write-keyword guard must still catch it. See #239."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'A')")

    with pytest.raises(SystemExit):
        raw_query(conn, "WITH x AS (SELECT 1) DELETE FROM docs WHERE 1=1")

    assert "DELETE statements are not allowed" in capsys.readouterr().err
    # The guard must fire during validation, before the DELETE executes.
    assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1


def test_raw_query_rejects_cte_prefixed_insert(capsys) -> None:
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(
            conn,
            "WITH x AS (SELECT 1) INSERT INTO docs (id, title) SELECT 'a', 'A' FROM x",
        )

    assert "INSERT statements are not allowed" in capsys.readouterr().err


def test_raw_query_rejects_cte_prefixed_replace(capsys) -> None:
    """REPLACE INTO is SQLite's INSERT-OR-REPLACE alias and can ride a CTE
    prefix with no FROM/JOIN — the same bypass class as #239, for a verb not
    in the main keyword loop. Adversarial-review finding."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'ok')")

    with pytest.raises(SystemExit):
        raw_query(
            conn,
            "WITH x AS (SELECT 1) REPLACE INTO docs (id, title) VALUES ('a', 'PWNED')",
        )

    assert "REPLACE statements are not allowed" in capsys.readouterr().err
    # Guard must fire before the write executes.
    assert conn.execute("SELECT title FROM docs WHERE id='a'").fetchone()[0] == "ok"


def test_raw_query_rejects_plain_replace_into(capsys) -> None:
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "REPLACE INTO docs (id, title) VALUES ('a', 'b')")


def test_raw_query_allows_replace_scalar_function() -> None:
    """The bare REPLACE() string function must stay allowed — only REPLACE
    INTO is a write."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'Note A')")

    result = raw_query(conn, "SELECT REPLACE(title, 'Note', 'Doc') AS t FROM docs")

    assert result == {"columns": ["t"], "rows": [["Doc A"]]}


def test_raw_query_rejects_double_quoted_disallowed_table(capsys) -> None:
    """Double-quoted SQLite identifiers must not bypass ALLOWED_TABLES. See #240."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, 'SELECT name FROM "sqlite_master"')

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_allows_double_quoted_allowed_table() -> None:
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'A')")

    result = raw_query(conn, 'SELECT id FROM "docs"')

    assert result == {"columns": ["id"], "rows": [["a"]]}


def test_raw_query_allows_keyword_named_quoted_identifiers() -> None:
    """Quoted identifiers whose names are write keywords must not trip the
    unanchored keyword scan (the #239 fix must not regress into #253's
    false-positive). Covers all three SQLite identifier-quoting forms."""
    conn = _connect()
    conn.execute('CREATE TABLE docs (id TEXT PRIMARY KEY, "delete" TEXT, "update" TEXT)')
    conn.execute("INSERT INTO docs (id, \"delete\", \"update\") VALUES ('a', 'd', 'u')")

    for sql in (
        'SELECT "delete" FROM docs',
        "SELECT `delete`, `update` FROM docs",
        "SELECT [delete] FROM docs",
        'SELECT id AS "create" FROM docs',
    ):
        result = raw_query(conn, sql)
        assert result["rows"], sql


def test_raw_query_allows_write_keywords_inside_string_literals() -> None:
    """Standalone keywords in string values must not trip the unanchored scan."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'when to DELETE FROM a vault')")

    result = raw_query(conn, "SELECT id FROM docs WHERE title LIKE '%DELETE%'")

    assert result == {"columns": ["id"], "rows": [["a"]]}


def test_raw_query_quoted_identifier_cannot_mint_cte_alias(capsys) -> None:
    """Text inside a quoted identifier must not register as a CTE name that
    shadows a blocked table in the allow-list check."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, 'SELECT "sqlite_master AS (" FROM sqlite_master')

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_allows_quoted_cte_aliases() -> None:
    """A CTE alias may be quoted in any of the three SQLite forms, and the
    quoted `FROM` ref back to it must resolve to the CTE — not be checked
    against ALLOWED_TABLES. Regression from extending the FROM check to
    quoted forms without teaching the CTE collector the same (review finding)."""
    conn = _docs_concepts_conn()

    for sql in (
        'WITH "myres" AS (SELECT id FROM docs) SELECT * FROM "myres"',
        "WITH `myres` AS (SELECT id FROM docs) SELECT * FROM `myres`",
        "WITH [myres] AS (SELECT id FROM docs) SELECT * FROM [myres]",
        "WITH myres AS (SELECT id FROM docs) SELECT * FROM myres",
    ):
        raw_query(conn, sql)  # must not SystemExit


def test_raw_query_allows_from_phrases_inside_double_quoted_strings() -> None:
    """SQLite falls back to treating an unresolvable "double-quoted" token as
    a string literal, and LLM-generated SQL uses that form constantly. Text
    like "notes from meeting" inside one must not be parsed as a phantom
    `FROM meeting` table ref (adversarial-review finding on the #240 fix)."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'notes from meeting')")

    for sql in (
        'SELECT * FROM docs WHERE title = "notes from meeting"',
        'SELECT id FROM docs WHERE title LIKE "%from now on%"',
        'SELECT * FROM docs WHERE title = "join tomorrow"',
    ):
        raw_query(conn, sql)  # must not SystemExit


def test_raw_query_allows_attach_as_identifier() -> None:
    """ATTACH is non-reserved and legal as a bare column/alias name; the
    keyword scan must not block it. Real ATTACH statements are still caught
    by the SELECT/WITH prefix check (next test)."""
    conn = _docs_concepts_conn()
    conn.execute("INSERT INTO docs (id, title) VALUES ('a', 'A')")

    result = raw_query(conn, "SELECT id AS attach FROM docs")

    assert result == {"columns": ["attach"], "rows": [["a"]]}


def test_raw_query_still_rejects_attach_statement(capsys) -> None:
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "ATTACH DATABASE '/tmp/evil.db' AS evil")

    assert "only SELECT queries are allowed" in capsys.readouterr().err


def test_raw_query_rejects_bracket_quoted_table() -> None:
    """Bracket-quoted identifiers must not bypass ALLOWED_TABLES either."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT name FROM [sqlite_master]")


def test_fts_search_tag_underscore_is_not_a_wildcard() -> None:
    """A `_` in a tag filter must match literally, not as a wildcard. See #225."""
    conn = _connect()
    conn.execute("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, title TEXT, date TEXT,
            status TEXT, tags TEXT, body TEXT
        )
    """)
    conn.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags, scope UNINDEXED)")
    # Note A: snake_case tag; Note B: differs only at the underscore position.
    conn.execute(
        "INSERT INTO docs (rowid, id, title, date, status, tags, body) "
        "VALUES (1, 'notes/a.md', 'A', '2026-06-15', 'draft', '[\"machine_learning\"]', 'ml text')"
    )
    conn.execute(
        "INSERT INTO docs (rowid, id, title, date, status, tags, body) "
        "VALUES (2, 'notes/b.md', 'B', '2026-06-15', 'draft', '[\"machine-learning\"]', 'ml text')"
    )
    for rowid, tags in ((1, '["machine_learning"]'), (2, '["machine-learning"]')):
        conn.execute(
            "INSERT INTO docs_fts (rowid, title, body, tags, scope) "
            "VALUES (?, 'x', 'ml text', ?, 'global')",
            (rowid, tags),
        )

    rows = fts_search(conn, "ml", tags=["machine_learning"])

    assert [row["id"] for row in rows] == ["notes/a.md"]


def test_run_ingest_failure_removes_wal_siblings(tmp_path: Path) -> None:
    """#254: a failed ingest must delete its -wal/-shm too, or the surviving
    -wal is silently replayed into the next DB file created at this path."""
    from schist.sqlite_query import _run_ingest

    db = tmp_path / "schist.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").write_bytes(b"junk")

    with patch("schist.ingest.ingest", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            _run_ingest(str(tmp_path), str(db))

    for suffix in ("", "-wal", "-shm"):
        assert not Path(f"{db}{suffix}").exists(), suffix


# ── #306: engine-level authorizer backstop ──────────────────────────────────


def _fts_conn() -> sqlite3.Connection:
    """docs + external-content docs_fts, mirroring schema.sql's FTS setup."""
    conn = _connect()
    conn.execute(
        "CREATE TABLE docs (id TEXT PRIMARY KEY, title TEXT, body TEXT,"
        " tags TEXT, scope TEXT)"
    )
    conn.execute(
        "INSERT INTO docs VALUES ('a', 'Note 1', 'hello world', '[]', 'global')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, tags,"
        " scope UNINDEXED, content='docs', content_rowid='rowid')"
    )
    conn.execute(
        "INSERT INTO docs_fts(rowid, title, body, tags, scope)"
        " SELECT rowid, title, body, tags, scope FROM docs"
    )
    conn.commit()
    return conn


def test_raw_query_rejects_comma_join_disallowed_table(capsys) -> None:
    """`FROM docs, sqlite_master`: the regex captures only the first table
    after FROM, so the fast path passes — the authorizer must catch it."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT * FROM docs, sqlite_master")

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_rejects_no_space_quoted_disallowed_table(capsys) -> None:
    """`FROM"sqlite_master"` (no whitespace) is valid SQLite the regex's
    `\\s+` never matches."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, 'SELECT * FROM"sqlite_master"')

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_rejects_parenthesized_disallowed_table(capsys) -> None:
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT * FROM (sqlite_master)")

    assert 'table "sqlite_master" is not allowed' in capsys.readouterr().err


def test_raw_query_rejects_pragma_table_valued_function(capsys) -> None:
    """pragma_* table-valued functions read schema metadata without any
    FROM-able table name the regex would flag."""
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT * FROM pragma_table_info('docs')")

    assert 'is not allowed' in capsys.readouterr().err


def test_raw_query_allows_fts_match_with_snippet_and_rank() -> None:
    """FTS5 MATCH internally reads the docs_fts_* shadow tables, the docs
    content table, and `PRAGMA data_version`; the authorizer must allow all
    three or every FTS query dies with 'not authorized'."""
    conn = _fts_conn()

    result = raw_query(
        conn,
        "SELECT d.id, snippet(docs_fts, 1, '[', ']', '...', 32) AS snip"
        " FROM docs_fts fts JOIN docs d ON d.rowid = fts.rowid"
        " WHERE docs_fts MATCH 'hello' ORDER BY rank LIMIT 5",
    )

    assert result["columns"] == ["id", "snip"]
    assert result["rows"] == [["a", "[hello] world"]]


def test_raw_query_allows_recursive_cte() -> None:
    conn = _docs_concepts_conn()

    result = raw_query(
        conn,
        "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt"
        " WHERE x < 3) SELECT x FROM cnt",
    )

    assert result["rows"] == [[1], [2], [3]]


def test_raw_query_authorizer_removed_after_success() -> None:
    """The backstop must not outlive the query: later statements on the same
    connection (ingest, fts_search, get_db's schema probe) run unrestricted.
    Guards the Python 3.10 pitfall where set_authorizer(None) fails to clear
    the hook and leaves the connection denying everything."""
    conn = _docs_concepts_conn()

    raw_query(conn, "SELECT id FROM docs")

    names = conn.execute("SELECT name FROM sqlite_master").fetchall()
    assert names  # would raise sqlite3.DatabaseError if still restricted


def test_raw_query_authorizer_removed_after_denial() -> None:
    conn = _docs_concepts_conn()

    with pytest.raises(SystemExit):
        raw_query(conn, "SELECT * FROM docs, sqlite_master")

    names = conn.execute("SELECT name FROM sqlite_master").fetchall()
    assert names


# ---------------------------------------------------------------------------
# Heal-path vs concurrent-writer lock (#330)
# ---------------------------------------------------------------------------


def _lock_contending_ingest(vault_path: str, db_path: str) -> None:
    """Stand-in for a real concurrent heal-ingest: try to take the write lock
    with a short busy timeout while another connection holds it, producing a
    genuine 'database is locked' OperationalError."""
    b = sqlite3.connect(db_path, timeout=0.05)
    try:
        b.execute("BEGIN IMMEDIATE")
    finally:
        b.close()


def test_run_ingest_locked_db_is_not_unlinked(tmp_path: Path) -> None:
    """#330: SQLITE_BUSY means another writer owns the DB right now — the
    failure path must NOT unlink the live db/wal out from under it."""
    from schist.sqlite_query import _run_ingest

    vault, db_path = _vault_db(tmp_path)
    writer = sqlite3.connect(db_path)
    _create_required_tables(writer)
    writer.execute("INSERT INTO docs VALUES ('d1')")
    writer.commit()
    # Writer A holds the write lock, as a long ingest transaction would.
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO docs VALUES ('d2')")
    try:
        with patch("schist.ingest.ingest", side_effect=_lock_contending_ingest):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                _run_ingest(str(vault), str(db_path))
    finally:
        writer.rollback()
        writer.close()

    assert db_path.exists(), "live DB unlinked under a concurrent writer"
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1
    finally:
        conn.close()


def test_run_ingest_non_lock_failure_still_unlinks_artifact(tmp_path: Path) -> None:
    """Non-contention failures keep the existing behavior: the partial DB is
    a broken artifact and must be removed so the next get_db() rebuilds."""
    from schist.sqlite_query import _run_ingest

    vault, db_path = _vault_db(tmp_path)
    conn = sqlite3.connect(db_path)
    _create_required_tables(conn)
    conn.commit()
    conn.close()

    with patch("schist.ingest.ingest", side_effect=ValueError("boom")):
        with pytest.raises(ValueError):
            _run_ingest(str(vault), str(db_path))

    assert not db_path.exists()


def test_get_db_heal_path_survives_concurrent_ingest_lock(tmp_path: Path) -> None:
    """#330 end-to-end: reader B sees a mid-ingest DB (user_version=0, empty
    docs), triggers the heal ingest, which hits SQLITE_BUSY because writer A
    holds the write lock. get_db must treat that as contention — keep the
    file and return a usable connection — instead of propagating."""
    vault, db_path = _vault_db(tmp_path)

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    _create_required_tables(writer)
    writer.commit()
    # Mid-ingest shape: user_version=0 + empty docs + write lock held.
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO docs VALUES ('mid-ingest')")
    try:
        with patch("schist.ingest.ingest", side_effect=_lock_contending_ingest):
            db = get_db(str(vault), str(db_path))
        # The returned connection is usable (WAL readers see the last
        # committed snapshot while A's transaction is still open).
        assert db.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 0
        db.close()
    finally:
        writer.rollback()
        writer.close()

    assert db_path.exists()


def test_get_db_heal_path_propagates_non_lock_operational_error(tmp_path: Path) -> None:
    """Only lock contention is swallowed — real I/O failures still surface."""
    vault, db_path = _vault_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 0")
    _create_required_tables(conn)
    conn.commit()
    conn.close()

    with patch("schist.ingest.ingest",
               side_effect=sqlite3.OperationalError("disk I/O error")):
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            get_db(str(vault), str(db_path))


def test_get_db_heal_path_raises_on_pre_ddl_db_under_lock(tmp_path: Path) -> None:
    """#360 corner 1: a first-ingest race leaves a pre-DDL file (WAL header,
    zero tables) on disk while the writer holds the lock. Falling through
    would hand the caller a table-less connection that crashes later with
    'no such table' — worse than the honest locked error, which must win."""
    vault, db_path = _vault_db(tmp_path)

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")  # materializes the header page
    writer.execute("BEGIN IMMEDIATE")
    try:
        with patch("schist.ingest.ingest", side_effect=_lock_contending_ingest):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                get_db(str(vault), str(db_path))
    finally:
        writer.rollback()
        writer.close()

    assert db_path.exists(), "contention must not unlink the writer's file"


def test_get_db_heal_path_raises_on_foreign_generation_db_under_lock(
    tmp_path: Path,
) -> None:
    """#360 corner 2: during a migration race the on-disk DB carries another
    schema generation. Generations are unordered identities, so a non-zero
    mismatch has no shape guarantees — re-raise the locked error instead of
    silently serving wrong-generation results."""
    vault, db_path = _vault_db(tmp_path)

    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA journal_mode=WAL")
    _create_required_tables(writer)
    writer.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION + 98}")
    writer.execute("INSERT INTO docs VALUES ('old-gen')")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO docs VALUES ('old-gen-2')")
    try:
        with patch("schist.ingest.ingest", side_effect=_lock_contending_ingest):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                get_db(str(vault), str(db_path))
    finally:
        writer.rollback()
        writer.close()



def test_locked_fallthrough_probe_accepts_current_and_mid_ingest(
    tmp_path: Path,
) -> None:
    """Probe unit semantics: current generation and the version-0 mid-ingest
    state pass; a foreign generation and a missing required table do not."""
    from schist.sqlite_query import _locked_fallthrough_db_usable

    vault, db_path = _vault_db(tmp_path)
    conn = sqlite3.connect(db_path)
    _create_required_tables(conn)
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    assert _locked_fallthrough_db_usable(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    assert _locked_fallthrough_db_usable(str(db_path)) is True

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    assert _locked_fallthrough_db_usable(str(db_path)) is False

    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    conn.execute("DROP TABLE docs")
    conn.commit()
    conn.close()
    assert _locked_fallthrough_db_usable(str(db_path)) is False


def test_locked_fallthrough_probe_unusable_under_exclusive_lock(
    tmp_path: Path,
) -> None:
    """Rollback-journal EXCLUSIVE locking blocks even the probe's reads —
    that must map to 'unusable' (immediately, not after a busy wait), so the
    caller sees the honest locked error."""
    from schist.sqlite_query import _locked_fallthrough_db_usable

    vault, db_path = _vault_db(tmp_path)
    writer = sqlite3.connect(db_path)
    _create_required_tables(writer)
    writer.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
    writer.commit()
    writer.execute("PRAGMA locking_mode = EXCLUSIVE")
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO docs VALUES ('locked')")
    try:
        assert _locked_fallthrough_db_usable(str(db_path)) is False
    finally:
        writer.rollback()
        writer.close()
