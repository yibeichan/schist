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
    conn.commit()
    conn.close()

    with patch("schist.sqlite_query._run_ingest") as run_ingest:
        db = get_db(str(vault), str(db_path))
        db.close()

    run_ingest.assert_not_called()


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
