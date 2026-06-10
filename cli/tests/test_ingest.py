"""Tests for the schist-ingest path.

Two layers:

1. **Unit** — call `schist.ingest.ingest()` directly on a tiny vault and
   assert the resulting SQLite DB has the expected rows. Proves the
   in-process call path used by `sqlite_query._run_ingest` and
   `sync._rebuild_index` is correct.

2. **Integration** — build the wheel from `cli/`, install it into a
   fresh venv, and invoke the `schist-ingest` console script end-to-end.
   Proves (a) the wheel includes `schema.sql` as package data, (b) the
   `schist-ingest` entry point is registered, and (c) the installed
   package can ingest a vault without the source tree on disk.

The integration test is the smoke test that catches a packaging
regression — if someone edits `cli/pyproject.toml` in a way that drops
`schema.sql` from the wheel, unit tests pass (source tree still has
the file) but the installed wheel breaks. That's exactly the class of
bug this test is here to catch.
"""

from __future__ import annotations

import os
import json
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


def _write_vault(root: Path) -> None:
    """Materialize a minimal vault with one note, one concept, one edge."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes").mkdir(exist_ok=True)
    (root / "concepts").mkdir(exist_ok=True)

    (root / "notes" / "2026-04-16-hello.md").write_text(
        "---\n"
        "title: Hello Ingest\n"
        "date: 2026-04-16\n"
        "tags: [smoke-test]\n"
        "status: draft\n"
        "concepts: [ingestion]\n"
        "---\n"
        "\n"
        "First note body.\n"
        "\n"
        "## Connections\n"
        "- extends: concepts/ingestion.md\n",
        encoding="utf-8",
    )
    (root / "concepts" / "ingestion.md").write_text(
        "---\n"
        "concept: ingestion\n"
        "title: Ingestion\n"
        "---\n"
        "\n"
        "Markdown → SQLite rebuild.\n",
        encoding="utf-8",
    )


def _assert_vault_ingested(db_path: Path) -> None:
    """Common assertions both layers run against the ingested DB."""
    assert db_path.exists(), f"DB not created at {db_path}"
    conn = sqlite3.connect(db_path)
    try:
        titles = {row[0] for row in conn.execute("SELECT title FROM docs")}
        assert "Hello Ingest" in titles, f"docs missing Hello Ingest: {titles}"
        slugs = {row[0] for row in conn.execute("SELECT slug FROM concepts")}
        assert "ingestion" in slugs, f"concepts missing ingestion: {slugs}"
        edge_types = {row[0] for row in conn.execute("SELECT type FROM edges")}
        assert "extends" in edge_types, f"edges missing extends: {edge_types}"
        implicit = conn.execute(
            "SELECT context FROM edges WHERE source = ? AND target = ? AND type = ?",
            ("notes/2026-04-16-hello.md", "ingestion", "references"),
        ).fetchone()
        assert implicit == (None,), "frontmatter concepts should create an implicit references edge"
        file_ref_cols = {row[1] for row in conn.execute("PRAGMA table_info(docs)")}
        assert "file_ref" in file_ref_cols
    finally:
        conn.close()


def test_ingest_deduplicates_implicit_concept_edges(tmp_path: Path) -> None:
    """Frontmatter concepts emit one references edge per unique source/target/type."""
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "2026-06-08-duplicate-concepts.md",
        "---\n"
        "title: Duplicate Concepts\n"
        "date: 2026-06-08\n"
        "concepts: [ingestion, ingestion]\n"
        "---\n"
        "\n"
        "Body.\n",
    )
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT source, target, type FROM edges WHERE target = ? AND type = ?",
            ("ingestion", "references"),
        ).fetchall()
    finally:
        conn.close()

    assert rows == [("notes/2026-06-08-duplicate-concepts.md", "ingestion", "references")]


def test_ingest_indexes_file_ref_frontmatter(tmp_path: Path) -> None:
    """`file_ref` is stored as a nullable docs column for query_graph lookup."""
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "2026-06-08-file-ref.md",
        "---\n"
        "title: File Ref\n"
        "date: 2026-06-08\n"
        "file_ref: /mnt/data/papers/example.pdf\n"
        "---\n"
        "\n"
        "Body.\n",
    )
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT title, file_ref FROM docs WHERE title = 'File Ref'").fetchone()
    finally:
        conn.close()

    assert row == ("File Ref", "/mnt/data/papers/example.pdf")


def test_ingest_indexes_paper_metadata(tmp_path: Path) -> None:
    """Citation-grade paper frontmatter populates the paper_metadata side table."""
    from schist.ingest import ingest
    from schist.sqlite_query import raw_query

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "papers").mkdir()
    (vault / "papers" / "2026-06-08-example-paper.md").write_text(
        "---\n"
        "title: Example Paper\n"
        "date: 2026-06-08\n"
        "authors:\n"
        "  - Doe, Jane\n"
        "  - Roe, Richard\n"
        "year: 2024\n"
        "venue: Journal of Examples\n"
        "type: journal\n"
        "doi: 10.1234/example\n"
        "arxiv_id: ''\n"
        "pubmed_pmid: '12345678'\n"
        "bibtex_key: doe2024example\n"
        "url: https://doi.org/10.1234/example\n"
        "verification:\n"
        "  verified_on: 2026-06-08\n"
        "  verified_by: codex\n"
        "  verified_against:\n"
        "    - crossref:10.1234/example\n"
        "    - pubmed:12345678\n"
        "---\n"
        "\n"
        "Body.\n",
        encoding="utf-8",
    )
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT pm.*, d.title
            FROM paper_metadata pm
            JOIN docs d ON d.id = pm.doc_id
            WHERE d.title = 'Example Paper'
            """
        ).fetchone()
        query = raw_query(
            conn,
            "SELECT d.title, pm.year FROM docs d JOIN paper_metadata pm ON pm.doc_id = d.id",
        )
    finally:
        conn.close()

    assert row is not None
    assert json.loads(row["authors"]) == ["Doe, Jane", "Roe, Richard"]
    assert row["year"] == 2024
    assert row["venue"] == "Journal of Examples"
    assert row["paper_type"] == "journal"
    assert row["doi"] == "10.1234/example"
    assert row["pubmed_pmid"] == "12345678"
    assert row["bibtex_key"] == "doe2024example"
    assert row["verified"] == 1
    assert row["verified_by"] == "codex"
    assert row["verified_date"] == "2026-06-08"
    assert json.loads(row["verification_sources"]) == [
        "crossref:10.1234/example",
        "pubmed:12345678",
    ]
    assert row["url"] == "https://doi.org/10.1234/example"
    assert query == {"columns": ["title", "year"], "rows": [["Example Paper", 2024]]}


def test_papers_without_metadata_get_unverified_paper_row(tmp_path: Path) -> None:
    """Every papers/ note gets a row, enabling 'show unverified papers' queries."""
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "papers").mkdir()
    (vault / "papers" / "2026-06-08-unverified.md").write_text(
        "---\ntitle: Unverified\ndate: 2026-06-08\n---\n\nBody.\n",
        encoding="utf-8",
    )
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT verified, doi FROM paper_metadata WHERE doc_id = ?",
            ("papers/2026-06-08-unverified.md",),
        ).fetchone()
    finally:
        conn.close()

    assert row == (0, None)


def test_cli_add_writes_file_ref_frontmatter(tmp_path: Path) -> None:
    """The CLI note-creation path can stamp `file_ref` into frontmatter."""
    from schist.commands import add

    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=vault, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=vault, check=True)

    args = SimpleNamespace(
        title="CLI File Ref",
        body="body",
        tags=None,
        concepts=None,
        file_ref="/mnt/data/cli.pdf",
        status="draft",
        directory="notes",
    )

    add(args, str(vault), str(vault / ".schist" / "schist.db"))

    written = next((vault / "notes").glob("*-cli-file-ref.md"))
    content = written.read_text(encoding="utf-8")
    assert "file_ref: /mnt/data/cli.pdf" in content


def test_ingest_in_process(tmp_path: Path) -> None:
    """Unit: importable `schist.ingest.ingest()` populates SQLite."""
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    _write_vault(vault)
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    ingest(str(vault), str(db))
    _assert_vault_ingested(db)


def test_sqlite_query_run_ingest_uses_in_process(tmp_path: Path) -> None:
    """Unit: `sqlite_query._run_ingest` routes to schist.ingest.ingest.

    Guards against regressing the subprocess → in-process switch that
    made `pip install schist` viable (the old code shelled out to
    `ingestion/ingest.py` relative to the source tree, which does not
    exist in an installed wheel).
    """
    from schist.sqlite_query import _run_ingest

    vault = tmp_path / "vault"
    _write_vault(vault)
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    _run_ingest(str(vault), str(db))
    _assert_vault_ingested(db)


# ---- Integration: build wheel and invoke the console script --------------


def _run_subprocess(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run wrapper that surfaces stdout/stderr on CalledProcessError.

    The integration test silences subprocess output by default (so passing
    runs don't spam the test report) — but on failure, the default
    `CalledProcessError("returned non-zero exit status 1")` is uselessly
    opaque in CI. This wrapper re-raises with the captured streams
    appended so the failure log shows what pip / venv / schist-ingest
    actually said.
    """
    kwargs.setdefault("check", True)
    kwargs.setdefault("capture_output", True)
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.CalledProcessError as e:
        stdout = (e.stdout or b"").decode("utf-8", errors="replace")
        stderr = (e.stderr or b"").decode("utf-8", errors="replace")
        raise AssertionError(
            f"subprocess failed: {' '.join(map(str, cmd))}\n"
            f"exit code: {e.returncode}\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        ) from e


def _cli_source_dir() -> Path:
    """Path to the cli/ directory regardless of cwd."""
    return Path(__file__).resolve().parent.parent


def _has_uv() -> bool:
    """Check whether uv is available on PATH."""
    return shutil.which("uv") is not None


def _build_wheel(dest: Path) -> Path:
    """Build the schist wheel into dest/. Returns the wheel file path."""
    if _has_uv():
        _run_subprocess([
            "uv", "build",
            "--wheel",
            "--out-dir", str(dest),
            str(_cli_source_dir()),
        ])
    else:
        _run_subprocess([
            sys.executable, "-m", "pip", "wheel",
            "--no-deps",
            "--wheel-dir", str(dest),
            str(_cli_source_dir()),
        ])
    wheels = list(dest.glob("schist-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _make_venv(path: Path) -> Path:
    """Create a venv at path and return its python executable."""
    if _has_uv():
        _run_subprocess(["uv", "venv", "--python", sys.executable, str(path)])
        # uv venvs lack pip — install it so the wheel-install step works.
        _run_subprocess(["uv", "pip", "install", "--python", str(path / "bin" / "python"), "pip"])
    else:
        _run_subprocess([sys.executable, "-m", "venv", str(path)])
    return path / "bin" / "python"


@pytest.mark.integration
def test_wheel_install_ships_schema_and_console_script(tmp_path: Path) -> None:
    """Integration: building the wheel and installing it into a fresh
    venv yields a working `schist-ingest` console script that can ingest
    a vault using the bundled schema.sql.

    Catches packaging regressions that unit tests cannot see — missing
    package-data, un-registered entry points, or paths that only work
    in editable installs.
    """
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    wheel = _build_wheel(wheel_dir)

    venv_dir = tmp_path / "venv"
    venv_py = _make_venv(venv_dir)

    # Install the wheel into the fresh venv. --no-index would be stricter
    # but we need PyPI for the two runtime deps (python-frontmatter, pyyaml).
    _run_subprocess([str(venv_py), "-m", "pip", "install", "--quiet", str(wheel)])

    schist_ingest = venv_dir / "bin" / "schist-ingest"
    assert schist_ingest.exists(), (
        f"schist-ingest console script not installed: listing bin/ = "
        f"{sorted(p.name for p in (venv_dir / 'bin').iterdir())}"
    )

    vault = tmp_path / "vault"
    _write_vault(vault)
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    # Run schist-ingest with the venv's environment so it resolves its
    # deps (frontmatter, yaml) from the venv, not the test-runner env.
    env = os.environ.copy()
    env["PATH"] = f"{venv_dir / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    _run_subprocess(
        [str(schist_ingest), "--vault", str(vault), "--db", str(db)],
        env=env,
    )

    _assert_vault_ingested(db)


def test_run_ingest_deletes_partial_db_on_failure(tmp_path: Path) -> None:
    """Unit: `_run_ingest` removes the partial DB if ingest raises.

    Without this cleanup, a failure during ingest would leave a docs
    table on disk. The next `get_db()` call would see the table exists,
    set needs_ingest=False, and silently return that stale DB to the
    caller. This test verifies that ANY exception from `ingest()`
    triggers DB removal — exact failure timing within `_ingest_into`
    isn't load-bearing, just that the cleanup is unconditional.
    """
    from schist.sqlite_query import _run_ingest

    vault = tmp_path / "vault"
    _write_vault(vault)
    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    # Force ingest to raise after `executescript` has already committed
    # the schema. Wrap `_ingest_into` so the original body runs (schema
    # written, docs/concepts/edges INSERTed) and we raise just before the
    # function returns — the exception propagates out of `ingest()` into
    # `_run_ingest`, which must then delete the partial DB.
    import schist.ingest as ingest_mod

    original = ingest_mod._ingest_into

    class _Boom(Exception):
        pass

    def _explode(*args, **kwargs):
        original(*args, **kwargs)
        raise _Boom("synthetic ingest failure")

    ingest_mod._ingest_into = _explode
    try:
        with pytest.raises(_Boom):
            _run_ingest(str(vault), str(db))
    finally:
        ingest_mod._ingest_into = original

    assert not db.exists(), (
        f"_run_ingest must delete the partial DB on failure but {db} still exists; "
        "next get_db() would trust an empty schema-only DB and serve no rows."
    )


# ---- #69: confidence field round-trip via frontmatter --------------------


def _write_note(vault: Path, name: str, body_block: str) -> None:
    (vault / "notes").mkdir(parents=True, exist_ok=True)
    (vault / "notes" / name).write_text(body_block, encoding="utf-8")


def test_ingest_reads_confidence_from_frontmatter(tmp_path: Path) -> None:
    """#69: `confidence` frontmatter populates `docs.confidence` for valid enum values.

    The schema's CHECK constraint AND ingest.py both gate on
    {'low','medium','high'} — this test exercises the happy path for all
    three and asserts NULL is preserved (no silent default to 'medium').
    """
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    for level in ("low", "medium", "high"):
        _write_note(
            vault,
            f"2026-05-24-{level}.md",
            f"---\ntitle: {level.title()}\ndate: 2026-05-24\nconfidence: {level}\n---\n\nBody.\n",
        )
    # A note that does NOT declare confidence — should land as NULL, not 'medium'.
    _write_note(
        vault,
        "2026-05-24-undeclared.md",
        "---\ntitle: Undeclared\ndate: 2026-05-24\n---\n\nBody.\n",
    )

    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute("SELECT title, confidence FROM docs"))
    finally:
        conn.close()

    assert rows == {
        "Low": "low",
        "Medium": "medium",
        "High": "high",
        "Undeclared": None,  # NOT 'medium' — the NULL state is load-bearing.
    }


def test_ingest_skips_invalid_confidence(tmp_path: Path) -> None:
    """#69: garbage confidence value falls back to NULL, not a crash.

    ingest.py validates against the enum before INSERTing. An off-enum
    string (e.g. an LLM hallucinating 'very-high') ingests as NULL —
    safer than CHECK-constraint-rejecting the whole row, which would
    silently drop the doc from the index.
    """
    from schist.ingest import ingest

    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(
        vault,
        "2026-05-24-garbage.md",
        "---\ntitle: Garbage\ndate: 2026-05-24\nconfidence: very-high\n---\n\nBody.\n",
    )

    db = vault / ".schist" / "schist.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    ingest(str(vault), str(db))

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT title, confidence FROM docs").fetchone()
    finally:
        conn.close()

    assert row == ("Garbage", None)
