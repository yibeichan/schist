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

    def test_validate_ignores_markdown_outside_configured_content_dirs(
        self, tmp_path, capsys,
    ):
        (tmp_path / "README.md").write_text("# Support documentation\n", encoding="utf-8")
        (tmp_path / "SCHEMA.md").write_text("# Schema documentation\n", encoding="utf-8")
        (tmp_path / "2026-08-25-root-note.md").write_text(
            "---\ntitle: Root note\n---\n\nValid root content.\n",
            encoding="utf-8",
        )
        shared = tmp_path / "shared" / "skills"
        shared.mkdir(parents=True)
        (shared / "SKILL.md").write_text("# Not a vault note\n", encoding="utf-8")
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "README.md").write_text("# Notes guide\n", encoding="utf-8")
        (notes / "good.md").write_text(
            "---\ntitle: Valid Note\nstatus: draft\n---\n\nBody\n",
            encoding="utf-8",
        )

        commands.schema(
            _schema_args(validate=True),
            str(tmp_path),
            str(tmp_path / ".schist" / "schist.db"),
        )

        assert capsys.readouterr().out.strip() == "All documents valid."

    def test_validate_accepts_stable_concept_shape(self, tmp_path, capsys):
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "stable-concept.md").write_text(
            "---\ntitle: Stable Concept\ntags: [graph]\n---\n\nDefinition.\n",
            encoding="utf-8",
        )

        commands.schema(
            _schema_args(validate=True),
            str(tmp_path),
            str(tmp_path / ".schist" / "schist.db"),
        )
        assert capsys.readouterr().out.strip() == "All documents valid."

    @pytest.mark.parametrize("directory", ["concepts", "Concepts"])
    def test_validate_reports_document_shaped_concept(
        self, tmp_path, capsys, directory,
    ):
        concepts_dir = tmp_path / directory
        concepts_dir.mkdir()
        (concepts_dir / "2026-07-24-legacy.md").write_text(
            "---\ntitle: Legacy\ndate: 2026-07-24\nstatus: draft\n---\n\n"
            "Definition.\n\n## Connections\n\n- extends: concepts/other\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc:
            commands.schema(
                _schema_args(validate=True),
                str(tmp_path),
                str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "concept frontmatter contains document-only field(s): date, status" in out
        assert "concept nodes cannot have outgoing ## Connections sections" in out

    @pytest.mark.parametrize("stem", [
        "invalid_slug",   # underscore
        "Invalid",        # uppercase
        "double--dash",   # empty segment
        "-leading",       # edge dash
    ])
    def test_validate_reports_invalid_concept_slug_stem(
        self, tmp_path, capsys, stem,
    ):
        """#474 — the stem-pattern branch had no test. The existing
        document-shaped fixture uses `2026-07-24-legacy`, whose stem MATCHES
        `[a-z0-9]+(-[a-z0-9]+)*`, so a regex regression here passed silently."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / f"{stem}.md").write_text(
            "---\ntitle: Invalid\n---\n\nDefinition.\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc:
            commands.schema(
                _schema_args(validate=True),
                str(tmp_path),
                str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "concept filename stem must match" in capsys.readouterr().out

    def test_validate_reports_concept_key_not_matching_stem(self, tmp_path, capsys):
        """#478 — ingest resolves the slug from `concept:` when present, so a
        key disagreeing with the filename indexes the file under a different
        slug than its name advertises."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "foo.md").write_text(
            "---\nconcept: bar\ntitle: Foo\n---\n\nDefinition.\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc:
            commands.schema(
                _schema_args(validate=True),
                str(tmp_path),
                str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "concept: key 'bar' does not match filename stem 'foo'" in out

    def test_validate_accepts_concept_key_matching_stem(self, tmp_path, capsys):
        """The legacy-but-consistent shape must stay valid — the check targets
        disagreement, not the presence of a `concept:` key."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "machine-learning.md").write_text(
            "---\nconcept: Machine Learning\ntitle: Machine Learning\n---\n\nDef.\n",
            encoding="utf-8",
        )

        commands.schema(
            _schema_args(validate=True),
            str(tmp_path),
            str(tmp_path / ".schist" / "schist.db"),
        )
        assert capsys.readouterr().out.strip() == "All documents valid."

    @pytest.mark.parametrize("value", ['""', "'   '", "[]"])
    def test_validate_reports_empty_or_non_string_concept_key(
        self, tmp_path, capsys, value,
    ):
        """#482 — ingest treats a blank key as "no key" on-axis, which is
        recoverable but unintended; validate must not stay silent or the two
        tools disagree about whether the file is well-formed."""
        concepts_dir = tmp_path / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "blank.md").write_text(
            f"---\nconcept: {value}\ntitle: Blank\n---\n\nDefinition.\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit) as exc:
            commands.schema(
                _schema_args(validate=True),
                str(tmp_path),
                str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "concept: key is empty or not a string" in capsys.readouterr().out

    def test_validate_reports_concept_marker_outside_concepts(self, tmp_path, capsys):
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "misplaced.md").write_text(
            "---\ntitle: Misplaced\nconcept: misplaced\n---\n\nDefinition.\n",
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            commands.schema(
                _schema_args(validate=True),
                str(tmp_path),
                str(tmp_path / ".schist" / "schist.db"),
            )
        assert "concept nodes must live under concepts/" in capsys.readouterr().out

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

    def test_validate_symlink_loop_does_not_crash(self, tmp_path, capsys):
        # A symlink loop must never propagate an uncaught exception out of
        # schema(). resolve() raises RuntimeError (NOT OSError) on a loop on
        # Python <=3.12 — the project floor — so a too-narrow `except OSError`
        # would let one looping .md symlink crash the whole validate run with a
        # traceback. On 3.13+ resolve() no longer raises and the loop instead
        # surfaces as a failed-to-parse violation at read time. Either outcome
        # (clean skip or reported violation) is acceptable; an uncaught
        # RuntimeError is not — and would fail this test on the floor Python.
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "good.md").write_text(
            "---\ntitle: Valid\n---\n\nBody\n", encoding="utf-8",
        )
        a = notes_dir / "a.md"
        b = notes_dir / "b.md"
        a.symlink_to(b)
        b.symlink_to(a)

        try:
            commands.schema(_schema_args(validate=True), str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        except SystemExit as e:
            # 3.13+: loop reported as a violation (non-zero exit) — fine.
            assert e.code == 1

        captured = capsys.readouterr()
        # The loop is handled one of two ways; both mention it, neither crashes.
        assert "unresolvable path" in captured.err or "failed to parse" in captured.out
