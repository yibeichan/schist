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
import shutil
import sqlite3
import subprocess
import sys
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
    finally:
        conn.close()


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

pytestmark_integration = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("python3") is None,
        reason="python3 not available to host a fresh venv",
    ),
]


def _cli_source_dir() -> Path:
    """Path to the cli/ directory regardless of cwd."""
    return Path(__file__).resolve().parent.parent


def _build_wheel(dest: Path) -> Path:
    """Build the schist wheel into dest/. Returns the wheel file path."""
    # Use `pip wheel` instead of `python -m build` so we don't require the
    # `build` package to be pre-installed in the test env.
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-deps",
            "--wheel-dir", str(dest),
            str(_cli_source_dir()),
        ],
        check=True,
        capture_output=True,
    )
    wheels = list(dest.glob("schist-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def _make_venv(path: Path) -> Path:
    """Create a venv at path and return its python executable."""
    subprocess.run(
        [sys.executable, "-m", "venv", str(path)],
        check=True,
        capture_output=True,
    )
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
    subprocess.run(
        [str(venv_py), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
        capture_output=True,
    )

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
    subprocess.run(
        [str(schist_ingest), "--vault", str(vault), "--db", str(db)],
        check=True,
        capture_output=True,
        env=env,
    )

    _assert_vault_ingested(db)
