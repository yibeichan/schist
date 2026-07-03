"""Tests for SQLite query/search helper behavior."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

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
    vault = tmp_path / "vault"
    db_path = vault / ".schist" / "schist.db"
    db_path.parent.mkdir(parents=True)

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE paper_metadata (doc_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE concept_aliases (duplicate_slug TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_not_called()


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
