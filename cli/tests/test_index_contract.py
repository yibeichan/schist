"""schema/index-contract.json is the single source for the vault-index table
contract (#130 D3) — tables, required tables/columns, rebuild survivors, and
the schema version ingest stamps into ``PRAGMA user_version``. It is consumed
here (``schist.index_contract``) and by ``mcp-server/src/sqlite-reader.ts``;
the per-language REQUIRED_TABLES constants it replaced had drifted (#339).

Two layers of pinning make the single source real:
  1. mirror drift — the baked-in fallback each component ships (repo-root
     ``schema/`` files ship with neither package) must equal the JSON;
  2. schema.sql parity — the contract must describe what schema.sql actually
     creates, so neither can change without the other.
The TS twin lives in mcp-server/tests/index-contract.test.ts.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from schist.index_contract import (
    INDEX_CONTRACT_FALLBACK,
    INDEX_SCHEMA_VERSION,
    REQUIRED_TABLES,
    TABLES,
    load_index_contract,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _contract() -> dict:
    fixture = _repo_root() / "schema" / "index-contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _schema_sql() -> str:
    return (_repo_root() / "cli" / "schist" / "schema.sql").read_text(encoding="utf-8")


def _materialize_schema() -> tuple[set[str], list[str]]:
    """Run schema.sql into :memory: and return (tables, docs columns)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_schema_sql())
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # docs_fts_* are FTS5 shadow tables and sqlite_sequence is the
        # AUTOINCREMENT bookkeeping table — implementation details, not part
        # of the contract surface.
        tables = {
            n for n in names if n != "sqlite_sequence" and not n.startswith("docs_fts_")
        }
        docs_columns = [
            row[1] for row in conn.execute("PRAGMA table_info(docs)").fetchall()
        ]
        return tables, docs_columns
    finally:
        conn.close()


# ── Mirror drift ───────────────────────────────────────────────────────────


def test_fallback_mirrors_schema_index_contract_json() -> None:
    assert INDEX_CONTRACT_FALLBACK == _contract()


def test_load_index_contract_returns_canonical_in_repo_checkout() -> None:
    assert load_index_contract() == _contract()


def test_module_constants_derive_from_the_contract() -> None:
    contract = _contract()
    assert INDEX_SCHEMA_VERSION == contract["schemaVersion"]
    assert TABLES == frozenset(contract["tables"])
    assert REQUIRED_TABLES == frozenset(contract["requiredTables"])


# ── schema.sql parity ──────────────────────────────────────────────────────


def test_schema_version_is_a_positive_integer() -> None:
    version = _contract()["schemaVersion"]
    assert isinstance(version, int) and not isinstance(version, bool)
    assert version > 0


def test_contract_tables_are_exactly_what_schema_sql_creates() -> None:
    tables, _ = _materialize_schema()
    assert set(_contract()["tables"]) == tables


def test_required_tables_are_a_subset_of_tables_and_include_docs() -> None:
    contract = _contract()
    assert set(contract["requiredTables"]) <= set(contract["tables"])
    # #339: a DB without docs is unusable for every read path; the fix went
    # in the direction of the CLI's stricter set.
    assert "docs" in contract["requiredTables"]


def test_required_docs_columns_exist_in_schema_sql() -> None:
    _, docs_columns = _materialize_schema()
    # A deliberate subset: readers don't need created_at/updated_at, so the
    # contract only requires the columns the read paths SELECT.
    assert set(_contract()["requiredDocsColumns"]) <= set(docs_columns)


def test_rebuild_survivors_match_schema_sql_drop_create_structure() -> None:
    """A survivor must be created with IF NOT EXISTS and must NOT appear in
    the DROP list; every other contract table must be dropped, or a
    commit-path rebuild (ingest against the existing DB) would silently keep
    its stale rows."""
    contract = _contract()
    schema_sql = _schema_sql()
    dropped = set(re.findall(r"DROP TABLE IF EXISTS (\w+)", schema_sql, re.IGNORECASE))
    if_not_exists = set(
        re.findall(
            r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)", schema_sql, re.IGNORECASE
        )
    )

    survivors = set(contract["rebuildSurvivors"])
    assert survivors <= set(contract["tables"])
    assert survivors <= if_not_exists
    assert not survivors & dropped
    assert set(contract["tables"]) - survivors <= dropped


def test_ingest_stamps_the_contract_schema_version(tmp_path: Path) -> None:
    """The version readers compare against must be the version ingest stamps —
    the Python side is skew-free by construction (in-process import), and this
    pins schemaVersion to actual ingest behavior rather than to itself."""
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "2026-07-06-contract.md").write_text(
        "---\ntitle: Contract\ndate: 2026-07-06\n---\n\nBody.\n", encoding="utf-8"
    )
    db_path = tmp_path / "schist.db"
    ingest(str(vault), str(db_path))

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("PRAGMA user_version").fetchone()[0] == INDEX_SCHEMA_VERSION
        )
    finally:
        conn.close()
