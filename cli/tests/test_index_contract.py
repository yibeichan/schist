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

import hashlib
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


def _computed_schema_sql_digest() -> str:
    """Recompute the digest exactly as documented in index_contract.py:
    materialize schema.sql, keep sqlite_master rows outside the sqlite_* /
    docs_fts_* namespaces, sort the "type\\x1fname\\x1fsql" strings, join
    with \\x1e, SHA-256 the UTF-8 bytes. The TS twin recomputes it
    identically."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_schema_sql())
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    finally:
        conn.close()
    kept = sorted(
        f"{t}\x1f{n}\x1f{s or ''}"
        for (t, n, s) in rows
        if not n.startswith("sqlite_") and not n.startswith("docs_fts_")
    )
    return hashlib.sha256("\x1e".join(kept).encode("utf-8")).hexdigest()


def test_schema_sql_digest_pins_the_materialized_ddl() -> None:
    """ANY schema.sql DDL edit must force a visible contract diff, even one
    that dodges every list-based check (e.g. adding a docs column + a reader
    SELECT while forgetting the schemaVersion bump and requiredDocsColumns
    entry — requiredDocsColumns is only checked as a subset). On failure:
    update schema/index-contract.json + both baked mirrors with the digest
    below AND decide whether schemaVersion must bump."""
    computed = _computed_schema_sql_digest()
    assert _contract()["schemaSqlDigest"] == computed, (
        f"schema.sql changed: recomputed digest is {computed}"
    )


def test_doctor_textual_parsers_match_the_real_mcp_source() -> None:
    """doctor's MCP-side checks regex-parse constants textually out of the
    compiled dist. Pin those regexes to the REAL sqlite-reader.ts (tsc only
    strips type annotations, which the regexes tolerate): a refactor of
    either literal to a derived expression would otherwise silently
    downgrade doctor's checks to SKIP — every existing doctor test uses
    hand-written stubs and would keep passing."""
    from schist.doctor import (
        _DOC_COL_STRING_RE,
        _INDEX_CONTRACT_FALLBACK_RE,
        _REQUIRED_DOCS_RE,
        _SCHEMA_VERSION_KEY_RE,
    )

    src = (
        _repo_root() / "mcp-server" / "src" / "sqlite-reader.ts"
    ).read_text(encoding="utf-8")
    contract = _contract()

    m = _REQUIRED_DOCS_RE.search(src)
    assert m is not None, "REQUIRED_DOCS_COLUMNS Set literal no longer parseable"
    assert set(_DOC_COL_STRING_RE.findall(m.group(1))) == set(
        contract["requiredDocsColumns"]
    )

    block = _INDEX_CONTRACT_FALLBACK_RE.search(src)
    assert block is not None, "INDEX_CONTRACT_FALLBACK literal no longer parseable"
    version = _SCHEMA_VERSION_KEY_RE.search(block.group(1))
    assert version is not None, "schemaVersion entry no longer parseable"
    assert int(version.group(1)) == contract["schemaVersion"]


def test_sync_side_table_preservation_covers_exactly_the_rebuild_survivors() -> None:
    """rebuildSurvivors is declarative; the runtime that actually carries
    survivor rows across a spoke-pull rebuild is sync.py's
    _preserve_side_tables, keyed off its own _SIDE_TABLE_COLUMNS. A survivor
    declared in the contract but absent there would be silently dropped on
    the next spoke pull."""
    from schist.sync import _SIDE_TABLE_COLUMNS

    assert set(_SIDE_TABLE_COLUMNS) == set(_contract()["rebuildSurvivors"])


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


# ── Loader fallback paths (the only paths production installs exercise) ────


def test_loader_missing_file_falls_back_silently(tmp_path: Path, capsys) -> None:
    """An installed wheel has no repo checkout — absence is the NORMAL state
    and must not warn on every CLI invocation."""
    contract = load_index_contract(tmp_path / "does-not-exist.json")
    assert contract == INDEX_CONTRACT_FALLBACK
    assert capsys.readouterr().err == ""


def test_loader_malformed_json_warns_and_falls_back(tmp_path: Path, capsys) -> None:
    """json.JSONDecodeError subclasses ValueError, NOT OSError — it must hit
    the warn path, not be swallowed with the missing-file case."""
    bad = tmp_path / "index-contract.json"
    bad.write_text("{ not json", encoding="utf-8")
    contract = load_index_contract(bad)
    assert contract == INDEX_CONTRACT_FALLBACK
    assert "malformed" in capsys.readouterr().err


def test_loader_invalid_shape_warns_and_falls_back(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "index-contract.json"
    bad.write_text(
        json.dumps({**INDEX_CONTRACT_FALLBACK, "requiredTables": []}),
        encoding="utf-8",
    )
    contract = load_index_contract(bad)
    assert contract == INDEX_CONTRACT_FALLBACK
    assert "malformed" in capsys.readouterr().err


def test_loader_coerces_integral_float_schema_version(tmp_path: Path, capsys) -> None:
    """JSON has one number type: JS parses `2.0` as the integer 2 and cannot
    even observe the difference, so the Python loader must coerce integral
    floats rather than desync the two languages on a hand-edited file."""
    f = tmp_path / "index-contract.json"
    f.write_text(
        json.dumps({**INDEX_CONTRACT_FALLBACK, "schemaVersion": 2.0}),
        encoding="utf-8",
    )
    contract = load_index_contract(f)
    assert contract["schemaVersion"] == 2
    assert isinstance(contract["schemaVersion"], int)
    assert capsys.readouterr().err == ""


def test_loader_rejects_schema_version_beyond_32_bits(tmp_path: Path, capsys) -> None:
    """`PRAGMA user_version` is a signed 32-bit header field; an oversized
    version would truncate on stamp and put every reader in a permanent
    rebuild loop — reject it at load time instead."""
    f = tmp_path / "index-contract.json"
    f.write_text(
        json.dumps({**INDEX_CONTRACT_FALLBACK, "schemaVersion": 2**31}),
        encoding="utf-8",
    )
    contract = load_index_contract(f)
    assert contract == INDEX_CONTRACT_FALLBACK
    assert "malformed" in capsys.readouterr().err


def test_loader_rejects_non_hex_digest(tmp_path: Path, capsys) -> None:
    f = tmp_path / "index-contract.json"
    f.write_text(
        json.dumps({**INDEX_CONTRACT_FALLBACK, "schemaSqlDigest": "zz"}),
        encoding="utf-8",
    )
    contract = load_index_contract(f)
    assert contract == INDEX_CONTRACT_FALLBACK
    assert "malformed" in capsys.readouterr().err
