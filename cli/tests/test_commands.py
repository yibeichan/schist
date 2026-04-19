"""Tests for CLI commands: add, link, assign-domain."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from schist.commands import assign_domain
from schist.ingest import ingest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vault_with_note(tmp_path: Path, with_domain: bool = False) -> tuple[str, str]:
    """Create a minimal vault with one note. Returns (vault_path, note_id)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "notes").mkdir()

    domain_fm = "domain: ai\n" if with_domain else ""

    note = vault / "notes" / "2026-04-19-test.md"
    note.write_text(
        f"---\n"
        "title: Test Note\n"
        "date: 2026-04-19\n"
        "status: draft\n"
        f"{domain_fm}"
        "---\n"
        "Test body.\n",
        encoding="utf-8",
    )

    # Initialize git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=vault, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=vault, check=True, capture_output=True)

    return str(vault), "notes/2026-04-19-test.md"


# ---------------------------------------------------------------------------
# assign_domain
# ---------------------------------------------------------------------------


class TestAssignDomain:
    def test_assign_domain_happy_path(self, tmp_path, capsys):
        """Happy path: assign valid domain to note."""
        vault_path, note_id = _make_vault_with_note(tmp_path)

        # Create vault.yaml with domains
        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains:\n  - ai\n  - security\n  - ops\n", encoding="utf-8")

        args = MagicMock(id=note_id, domain="security")
        db_path = tmp_path / "db.sqlite"

        assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert 'Assigned domain "security"' in captured.out

        # Verify frontmatter was updated
        note_content = (Path(vault_path) / note_id).read_text(encoding="utf-8")
        assert "domain: security" in note_content

    def test_assign_domain_replaces_existing(self, tmp_path, capsys):
        """Assigning a domain replaces an existing domain value."""
        vault_path, note_id = _make_vault_with_note(tmp_path, with_domain=True)

        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains:\n  - ai\n  - security\n", encoding="utf-8")

        args = MagicMock(id=note_id, domain="security")
        db_path = tmp_path / "db.sqlite"

        assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert 'Assigned domain "security"' in captured.out

        # Verify only one domain in frontmatter
        note_content = (Path(vault_path) / note_id).read_text(encoding="utf-8")
        assert note_content.count("domain:") == 1

    def test_assign_domain_rejects_invalid_domain(self, tmp_path, capsys):
        """Reject domain not in vault.yaml."""
        vault_path, note_id = _make_vault_with_note(tmp_path)

        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains:\n  - ai\n  - security\n", encoding="utf-8")

        args = MagicMock(id=note_id, domain="invalid-domain")
        db_path = tmp_path / "db.sqlite"

        with pytest.raises(SystemExit):
            assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert 'not in vault.yaml' in captured.err
        assert "Valid domains:" in captured.err

    def test_assign_domain_skips_validation_when_no_vault_yaml(self, tmp_path, capsys):
        """When vault.yaml doesn't exist, domain is assigned without validation."""
        vault_path, note_id = _make_vault_with_note(tmp_path)

        # No vault.yaml created

        args = MagicMock(id=note_id, domain="any-domain")
        db_path = tmp_path / "db.sqlite"

        assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert 'Assigned domain "any-domain"' in captured.out

    def test_assign_domain_rejects_missing_note(self, tmp_path, capsys):
        """Reject when note doesn't exist."""
        vault_path, _ = _make_vault_with_note(tmp_path)

        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains:\n  - ai\n", encoding="utf-8")

        args = MagicMock(id="notes/nonexistent.md", domain="ai")
        db_path = tmp_path / "db.sqlite"

        with pytest.raises(SystemExit):
            assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert "note not found" in captured.err

    def test_assign_domain_ingests_to_sqlite(self, tmp_path):
        """Domain value appears in SQLite after ingest."""
        vault_path, note_id = _make_vault_with_note(tmp_path)

        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains:\n  - ai\n  - ml\n", encoding="utf-8")

        args = MagicMock(id=note_id, domain="ml")
        db_path = tmp_path / "db.sqlite"

        assign_domain(args, vault_path, str(db_path))

        # Ingest to build SQLite
        ingest(vault_path, str(db_path))

        # Verify domain in SQLite
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT domain FROM docs WHERE id = ?", (note_id,)).fetchone()
            assert row is not None, "Note not found in DB"
            assert row[0] == "ml", f"Expected domain 'ml', got {row[0]!r}"
        finally:
            conn.close()

    def test_assign_domain_allows_empty_domains_list(self, tmp_path, capsys):
        """Empty domains list in vault.yaml allows any domain."""
        vault_path, note_id = _make_vault_with_note(tmp_path)

        vault_yaml = Path(vault_path) / "vault.yaml"
        vault_yaml.write_text("domains: []\n", encoding="utf-8")

        args = MagicMock(id=note_id, domain="anything")
        db_path = tmp_path / "db.sqlite"

        assign_domain(args, vault_path, str(db_path))

        captured = capsys.readouterr()
        assert 'Assigned domain "anything"' in captured.out
