from argparse import Namespace

import pytest
import yaml

from schist import commands


def _schema_args(validate: bool = False) -> Namespace:
    return Namespace(validate=validate)


class TestSchemaCommand:
    def test_prints_vault_schema_when_present(self, tmp_path, capsys):
        (tmp_path / "schist.yaml").write_text(
            "\n".join([
                "name: Custom Vault",
                "directories:",
                "  - notes",
                "  - lab",
                "statuses:",
                "  - draft",
                "connection_types:",
                "  - related",
                "write_branch: drafts",
                "",
            ]),
            encoding="utf-8",
        )

        commands.schema(_schema_args(), str(tmp_path), str(tmp_path / ".schist" / "schist.db"))

        cfg = yaml.safe_load(capsys.readouterr().out)
        assert cfg["name"] == "Custom Vault"
        assert cfg["directories"] == ["notes", "lab"]

    def test_prints_packaged_default_when_vault_schema_missing(self, tmp_path, capsys):
        commands.schema(_schema_args(), str(tmp_path), str(tmp_path / ".schist" / "schist.db"))

        cfg = yaml.safe_load(capsys.readouterr().out)
        assert cfg["directories"]["notes"] == "notes/"
        assert cfg["directories"]["projects"] == "projects/"
        assert cfg["write_branch"] == "drafts"

    def test_validate_happy_path(self, tmp_path, capsys):
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "good.md").write_text(
            "---\ntitle: Valid Note\nstatus: draft\n---\n\nBody\n",
            encoding="utf-8",
        )

        commands.schema(_schema_args(validate=True), str(tmp_path), str(tmp_path / ".schist" / "schist.db"))

        assert capsys.readouterr().out.strip() == "All documents valid."

    def test_validate_reports_missing_title_topic_or_concept(self, tmp_path, capsys):
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "bad.md").write_text(
            "---\nstatus: draft\n---\n\nBody\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc:
            commands.schema(_schema_args(validate=True), str(tmp_path), str(tmp_path / ".schist" / "schist.db"))

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "1 violation(s):" in out
        assert "notes/bad.md: missing title/topic/concept in frontmatter" in out

    def test_validate_skips_file_symlink_escaping_vault(self, tmp_path, capsys):
        # #342 parity: rglob follows symlinks, so a note symlink whose target
        # lives outside the vault would otherwise be read and validated,
        # mixing external state into the vault's health report. It must be
        # skipped with a stderr WARN, not counted as a violation.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "loose.md").write_text("no frontmatter\n", encoding="utf-8")

        notes_dir = tmp_path / "vault" / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "sneaky.md").symlink_to(outside / "loose.md")

        vault = tmp_path / "vault"
        commands.schema(_schema_args(validate=True), str(vault), str(vault / ".schist" / "schist.db"))

        captured = capsys.readouterr()
        assert captured.out.strip() == "All documents valid."
        assert "resolves outside the vault (symlink)" in captured.err

    def test_validate_skips_dir_symlink_escaping_vault(self, tmp_path, capsys):
        # A directory symlink into an external tree (the innocuously-named case
        # the hidden-dir filter can't catch) must not pull that tree's .md
        # files into the report. rglob follows directory symlinks on Python
        # <=3.12 patch releases; the resolved-path containment check guards it
        # regardless of the running interpreter's glob behavior.
        external = tmp_path / "external" / "notes"
        external.mkdir(parents=True)
        (external / "foreign.md").write_text(
            "---\nnot_title: x\n---\n\nBody\n", encoding="utf-8",
        )

        vault = tmp_path / "vault"
        notes_dir = vault / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "good.md").write_text(
            "---\ntitle: Valid\n---\n\nBody\n", encoding="utf-8",
        )
        (notes_dir / "papers-archive").symlink_to(
            tmp_path / "external" / "notes", target_is_directory=True,
        )

        commands.schema(_schema_args(validate=True), str(vault), str(vault / ".schist" / "schist.db"))

        # The one real in-vault note is valid; the external foreign.md (which
        # would be a violation if recursed) never enters the report.
        assert capsys.readouterr().out.strip() == "All documents valid."
