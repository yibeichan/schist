"""Tests for schist.commands.add and schist.commands.link.

These CLI write paths had zero coverage, which let three disk/DB parity gaps
with the MCP server slip in:

- #399: `add` wrote raw `#`-tags and mixed-case/spaced concept slugs to disk
  while ingest normalized them on read, so on-disk frontmatter and the indexed
  DB always diverged.
- #397: `link` accepted any connection type, bypassing the controlled
  vocabulary the MCP `add_connection` enforces since #304 — the root cause of
  #363 (notes made un-editable via `update_note`).

git_ops.commit is patched out so these tests exercise the pure write/validate
behavior without spawning real git or its hooks.
"""

from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from schist import commands, markdown_io


def _add_args(title, *, body="body", tags=None, concepts=None, file_ref=None,
              status="draft", directory="notes") -> Namespace:
    return Namespace(
        title=title, body=body, tags=tags, concepts=concepts,
        file_ref=file_ref, status=status, directory=directory,
    )


def _link_args(source, target, link_type, context=None) -> Namespace:
    return Namespace(source=source, target=target, link_type=link_type, context=context)


def _written_frontmatter(vault: Path) -> dict:
    md_files = list((vault / "notes").glob("*.md"))
    assert len(md_files) == 1, f"expected exactly one note, got {md_files}"
    return markdown_io.read_note(str(md_files[0]))["frontmatter"]


class TestAddNormalization:
    def test_strips_hash_prefix_from_tags(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(
                _add_args("Note", tags="#fmri, #hmm"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        # On-disk frontmatter must match what ingest._normalize_tag stores.
        assert _written_frontmatter(tmp_path)["tags"] == ["fmri", "hmm"]

    def test_normalizes_concept_slugs(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(
                _add_args("Note", concepts="Hidden Markov Model, Predictive Coding"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        assert _written_frontmatter(tmp_path)["concepts"] == [
            "hidden-markov-model", "predictive-coding",
        ]

    def test_drops_empty_tags_and_concepts(self, tmp_path, capsys):
        # A trailing comma or a bare "#" must not produce an empty entry that
        # ingest would normalize to "" and drop, re-creating the skew.
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(
                _add_args("Note", tags="fmri, , #", concepts="Foo, ,"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        fm = _written_frontmatter(tmp_path)
        assert fm["tags"] == ["fmri"]
        assert fm["concepts"] == ["foo"]


class TestLinkVocabulary:
    def _vault_with_types(self, tmp_path, types="[extends, supports]"):
        (tmp_path / "schist.yaml").write_text(
            f"connection_types: {types}\ndirectories:\n  notes: notes/\n",
            encoding="utf-8",
        )
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "src.md").write_text(
            "---\ntitle: Src\nstatus: draft\n---\n\nBody\n\n## Connections\n",
            encoding="utf-8",
        )

    def test_rejects_out_of_vocabulary_type(self, tmp_path, capsys):
        self._vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "made-up-type"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "made-up-type" in err
        assert "extends, supports" in err
        # Rejection must happen before any write — the source file is untouched.
        assert "made-up-type" not in (tmp_path / "notes" / "src.md").read_text()

    def test_accepts_in_vocabulary_type(self, tmp_path, capsys):
        self._vault_with_types(tmp_path)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        body = (tmp_path / "notes" / "src.md").read_text()
        assert "- extends: notes/dst.md" in body

    def test_empty_vocabulary_accepts_any_type(self, tmp_path, capsys):
        # An empty connection_types list means "no vocabulary configured" —
        # the CLI must not hard-fail every link in that vault.
        self._vault_with_types(tmp_path, types="[]")
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "anything-goes"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        assert "- anything-goes: notes/dst.md" in (tmp_path / "notes" / "src.md").read_text()
