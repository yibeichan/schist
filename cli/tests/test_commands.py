"""Tests for schist.commands.add and schist.commands.link.

These CLI write paths had zero coverage, which let three disk/DB parity gaps
with the MCP server slip in:

- #399: `add` wrote raw `#`-tags and mixed-case/spaced concept slugs to disk
  while ingest normalized them on read, so on-disk frontmatter and the indexed
  DB always diverged.
- #397: `link` accepted any connection type, bypassing the controlled
  vocabulary the MCP `add_connection` enforces since #304 — the root cause of
  #363 (notes made un-editable via `update_note`).

The second parity batch (#405–#407) closed the same class of gap for the
remaining write-side guards: line-boundary injection through `link`'s
target/context (MCP #398), same-day same-title silent overwrite in `add`
(MCP collision suffix), and unvalidated `--status` (MCP #276).

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


def _vault_with_types(tmp_path, types="[extends, supports]"):
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


class TestLinkVocabulary:
    def test_rejects_out_of_vocabulary_type(self, tmp_path, capsys):
        _vault_with_types(tmp_path)
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
        _vault_with_types(tmp_path)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        body = (tmp_path / "notes" / "src.md").read_text()
        assert "- extends: notes/dst.md" in body

    def test_explicit_empty_vocabulary_rejects_all_types(self, tmp_path, capsys):
        # An explicit `connection_types: []` is used verbatim and therefore
        # rejects every type — matching MCP's connectionTypes.includes() where
        # an empty array admits nothing. (A MISSING key is different: see below.)
        _vault_with_types(tmp_path, types="[]")
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "extends" not in (tmp_path / "notes" / "src.md").read_text().split("## Connections")[1]

    def test_missing_connection_types_key_falls_back_to_default_vocabulary(self, tmp_path, capsys):
        # A partial hand-edited schist.yaml with no connection_types key must
        # NOT silently disable the check (that would reopen #363); it falls
        # back to the packaged default vocabulary, exactly as MCP's
        # loadVaultConfig applies its default. "extends" is in the default set;
        # "made-up" is not.
        (tmp_path / "schist.yaml").write_text(
            "directories:\n  notes: notes/\n", encoding="utf-8",
        )
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "src.md").write_text(
            "---\ntitle: Src\nstatus: draft\n---\n\nBody\n\n## Connections\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "made-up"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert "- extends: notes/dst.md" in (tmp_path / "notes" / "src.md").read_text()


def _parsed_edges(vault: Path) -> list:
    """Parse the source note back through ingest's own connection parser —
    the read path a forged edge would have to survive to reach the index."""
    from schist.ingest import parse_connections

    body = markdown_io.read_note(str(vault / "notes" / "src.md"))["body"]
    return parse_connections(body)


class TestLinkInjection:
    """#405 — CLI sibling of the MCP #398 line-boundary injection fix."""

    def test_rejects_newline_in_target_before_any_write(self, tmp_path, capsys):
        _vault_with_types(tmp_path)
        payload = "notes/b.md\n- supports: notes/evil.md"
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", payload, "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "line-break" in capsys.readouterr().err
        assert "evil" not in (tmp_path / "notes" / "src.md").read_text()

    @pytest.mark.parametrize("sep", list(markdown_io.LINE_BOUNDARY_CHARS))
    def test_rejects_every_splitlines_boundary_in_target(self, tmp_path, capsys, sep):
        # The guard must cover the FULL splitlines set, not just \n — the
        # same bypass the MCP fix's adversarial review caught (#402).
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit):
            commands.link(
                _link_args("notes/src.md", f"notes/b.md{sep}- supports: notes/evil.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        assert "evil" not in (tmp_path / "notes" / "src.md").read_text()

    def test_context_injection_is_flattened_not_indexed(self, tmp_path, capsys):
        # Context is the parallel vector: it is sanitized (like MCP's
        # buildConnectionLine), not rejected — the payload must come back
        # from ingest's parser as ONE edge with the payload inert inside
        # the context string.
        _vault_with_types(tmp_path)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/b.md", "extends",
                           context='ignored"\n- supports: notes/evil.md'),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        edges = _parsed_edges(tmp_path)
        assert [(e["type"], e["target"]) for e in edges] == [("extends", "notes/b.md")]

    def test_context_quotes_become_single_quotes(self, tmp_path, capsys):
        # The serialized format delimits context with "..." — an embedded
        # double-quote would truncate what CONNECTION_RE captures.
        _vault_with_types(tmp_path)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/b.md", "extends",
                           context='she said "no"'),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        edges = _parsed_edges(tmp_path)
        assert edges[0]["context"] == "she said 'no'"


class TestAddCollision:
    """#406 — same-day same-title must never silently overwrite."""

    def test_second_add_mints_suffixed_file_and_keeps_first(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Daily note", body="first"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
            commands.add(_add_args("Daily note", body="second"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        files = sorted((tmp_path / "notes").glob("*.md"))
        assert len(files) == 2, f"expected two notes, got {[f.name for f in files]}"
        bodies = {markdown_io.read_note(str(f))["body"] for f in files}
        assert bodies == {"first", "second"}
        # Suffix shape mirrors the MCP guard: <date>-<slug>-HH-MM-SS.md.
        import re
        suffixed = [f.name for f in files if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}-daily-note-\d{2}-\d{2}-\d{2}\.md", f.name)]
        assert len(suffixed) == 1


class TestAddStatusVocabulary:
    """#407 — `add --status` validates against the vault vocabulary."""

    def _vault_with_statuses(self, tmp_path, statuses="[draft, wip]"):
        (tmp_path / "schist.yaml").write_text(
            f"statuses: {statuses}\n", encoding="utf-8",
        )

    def test_rejects_out_of_vocabulary_status_before_any_write(self, tmp_path, capsys):
        self._vault_with_statuses(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note", status="arbitrary-value"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "arbitrary-value" in err
        assert "draft, wip" in err
        assert not (tmp_path / "notes").exists()

    def test_accepts_configured_status(self, tmp_path, capsys):
        self._vault_with_statuses(tmp_path)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note", status="wip"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert _written_frontmatter(tmp_path)["status"] == "wip"

    def test_default_falls_back_to_first_status_when_draft_excluded(self, tmp_path, capsys):
        # Mirror MCP create_note's resolved-default (#276): a bare
        # `schist add` (argparse default None) on a vault whose vocabulary
        # excludes 'draft' must write the first configured status, not fail
        # and not write 'draft'.
        self._vault_with_statuses(tmp_path, statuses="[wip, done]")
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note", status=None),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert _written_frontmatter(tmp_path)["status"] == "wip"

    def test_missing_statuses_key_falls_back_to_default_vocabulary(self, tmp_path, capsys):
        # A schist.yaml without a statuses key must NOT disable the check —
        # same fallback rule as connection_types (#363 lesson).
        (tmp_path / "schist.yaml").write_text("directories:\n  notes: notes/\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            commands.add(_add_args("Note", status="bogus"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note", status="review"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert _written_frontmatter(tmp_path)["status"] == "review"

    def test_explicit_empty_vocabulary_rejects_all(self, tmp_path, capsys):
        self._vault_with_statuses(tmp_path, statuses="[]")
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note", status=None),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "(none configured)" in capsys.readouterr().err


class TestLinkTargetRoundTrip:
    """Review findings on #405: an empty or whitespace-carrying target writes
    a line CONNECTION_RE can never round-trip — silent success, no edge."""

    def test_rejects_empty_target(self, tmp_path, capsys):
        # With an empty target the quoted context slides into the target slot
        # on read — context text promoted to an edge target.
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", "", "extends", context="evil.md"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "non-empty" in capsys.readouterr().err
        assert "evil" not in (tmp_path / "notes" / "src.md").read_text()

    @pytest.mark.parametrize("target", ["notes/a b.md", "notes/a\tb.md", "notes/a\xa0b.md"])
    def test_rejects_whitespace_carrying_target(self, tmp_path, capsys, target):
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", target, "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "whitespace" in capsys.readouterr().err
        assert target not in (tmp_path / "notes" / "src.md").read_text()


class TestAddCollisionHardening:
    """Review findings on #406: the guard must survive same-second collisions
    and must not read a dangling symlink as 'no file'."""

    def test_third_same_second_add_gets_counter_suffix(self, tmp_path, capsys):
        # Freeze the clock so all three adds land "in the same second": a
        # single MCP-style exists-check would re-mint add #2's suffix for
        # add #3 and truncate it.
        from datetime import datetime as real_datetime

        class _FixedDatetime:
            @staticmethod
            def now(tz=None):
                return real_datetime(2026, 7, 16, 12, 0, 0)

        with patch("schist.commands.git_ops.commit", return_value=(True, "")), \
             patch("schist.commands.datetime", _FixedDatetime):
            for body in ("first", "second", "third"):
                commands.add(_add_args("Daily note", body=body),
                             str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        files = sorted(f.name for f in (tmp_path / "notes").glob("*.md"))
        assert len(files) == 3, files
        assert any(f.endswith("-12-00-00.md") for f in files)
        assert any(f.endswith("-12-00-00-2.md") for f in files)
        bodies = {markdown_io.read_note(str(tmp_path / "notes" / f))["body"] for f in files}
        assert bodies == {"first", "second", "third"}

    def test_dangling_symlink_counts_as_collision(self, tmp_path, capsys):
        # os.path.exists is False for a dangling symlink; writing "through" it
        # would create the note body at the symlink's target — outside the
        # vault if the link points there. lexists must treat it as taken.
        from datetime import date as real_date
        notes = tmp_path / "notes"
        notes.mkdir()
        base = notes / f"{real_date.today().isoformat()}-daily-note.md"
        outside_target = tmp_path / "outside" / "escape.md"
        base.symlink_to(outside_target)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Daily note", body="content"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert not outside_target.exists(), "note body escaped through the symlink"
        real_files = [f for f in notes.glob("*.md") if not f.is_symlink()]
        assert len(real_files) == 1
        assert markdown_io.read_note(str(real_files[0]))["body"] == "content"


class TestAddTitleDatePrefix:
    """Parity with MCP create_note's #118 guard: a YYYY-MM-DD-prefixed title
    would mint a doubled-date note id the MCP write path refuses."""

    def test_rejects_date_prefixed_title(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("2026-07-16 Meeting notes"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "date prefix" in capsys.readouterr().err
        assert not (tmp_path / "notes").exists()

    def test_accepts_title_with_interior_date(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Meeting notes 2026-07-16"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert _written_frontmatter(tmp_path)["title"] == "Meeting notes 2026-07-16"


class TestSchemaConfigRobustness:
    """Review finding: a non-mapping schist.yaml (hand-edited top-level list)
    must degrade to the packaged defaults — like MCP's loadVaultConfig, where
    raw[key] on a non-mapping is undefined — not crash every command."""

    def test_non_mapping_schist_yaml_falls_back_to_defaults(self, tmp_path, capsys):
        (tmp_path / "schist.yaml").write_text("- extends\n- supports\n", encoding="utf-8")
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert _written_frontmatter(tmp_path)["status"] == "draft"

    def test_falsy_but_stringifiable_vocab_entry_is_kept(self, tmp_path, capsys):
        # getStringList parity: .map(String).filter(Boolean) coerces BEFORE
        # filtering, so an unquoted 0 in YAML is the status "0" on both
        # writers — filtering raw values would make the CLI reject what MCP
        # accepts (#363's skew class).
        (tmp_path / "schist.yaml").write_text("statuses: [draft, 0]\n", encoding="utf-8")
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note", status="0"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert str(_written_frontmatter(tmp_path)["status"]) == "0"

    def test_malformed_schist_yaml_exits_cleanly(self, tmp_path, capsys):
        # Second /review batch: a syntactically INVALID schist.yaml (unclosed
        # quote — distinct from the non-mapping case above) must die with a
        # clean message, not a raw yaml.YAMLError traceback, and must NOT
        # silently widen the vocabulary by falling back to the default.
        (tmp_path / "schist.yaml").write_text('statuses: ["unclosed\n', encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "could not read schist.yaml" in capsys.readouterr().err
        assert not (tmp_path / "notes").exists()


class TestAddCollisionRace:
    """Second /review batch on #406: the lexists loop is only the sequential
    fix — the AUTHORITATIVE guard is the O_EXCL ('x') write. Simulate the
    concurrent-writer race by making the pre-probe lie ("path free") while
    the path exists: a probe-then-open('w') design truncates; the exclusive
    write must step to the next candidate instead."""

    def test_probe_lied_existing_file_is_not_truncated(self, tmp_path, capsys):
        from datetime import date as real_date
        notes = tmp_path / "notes"
        notes.mkdir()
        base = notes / f"{real_date.today().isoformat()}-daily-note.md"
        base.write_text("---\ntitle: Winner\n---\n\nfirst\n", encoding="utf-8")
        with patch("schist.commands.git_ops.commit", return_value=(True, "")), \
             patch("schist.commands.os.path.lexists", return_value=False):
            commands.add(_add_args("Daily note", body="second"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert "first" in base.read_text(encoding="utf-8"), "concurrent winner was truncated"
        files = sorted(notes.glob("*.md"))
        assert len(files) == 2, [f.name for f in files]
        bodies = {markdown_io.read_note(str(f))["body"] for f in files}
        assert "second" in bodies

    def test_probe_lied_dangling_symlink_is_not_written_through(self, tmp_path, capsys):
        # A dangling symlink planted in the probe→write window: open('w')
        # would create the note body AT THE SYMLINK TARGET (outside the vault
        # included); open('x') refuses atomically and the loop steps around.
        from datetime import date as real_date
        notes = tmp_path / "notes"
        notes.mkdir()
        outside_target = tmp_path / "outside" / "escape.md"
        base = notes / f"{real_date.today().isoformat()}-daily-note.md"
        base.symlink_to(outside_target)
        with patch("schist.commands.git_ops.commit", return_value=(True, "")), \
             patch("schist.commands.os.path.lexists", return_value=False):
            commands.add(_add_args("Daily note", body="content"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert not outside_target.exists(), "note body escaped through the symlink"
        real_files = [f for f in notes.glob("*.md") if not f.is_symlink()]
        assert len(real_files) == 1
        assert markdown_io.read_note(str(real_files[0]))["body"] == "content"


class TestAddTitleEmptySlug:
    """Second /review batch: parity with MCP create_note's empty-slug guard
    (the check right beside the #118 date-prefix guard already ported)."""

    def test_rejects_all_symbol_title(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("!!!"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "alphanumeric" in capsys.readouterr().err
        assert not (tmp_path / "notes").exists()


class TestAddDateConsistency:
    """Second /review batch: filename date and frontmatter date come from ONE
    today_str computation — a second date.today() call could disagree across
    midnight, minting an id/frontmatter skew MCP can't produce."""

    def test_frontmatter_date_matches_filename_prefix(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        (note,) = (tmp_path / "notes").glob("*.md")
        fm = markdown_io.read_note(str(note))["frontmatter"]
        assert note.name.startswith(f"{fm['date']}-")


class TestLinkBracketTarget:
    """Second /review batch: a '['-leading target passes the boundary and
    round-trip guards and matches CONNECTION_RE, but ingest.parse_connections
    SKIPS bracket references — success reported, edge never indexed (#415
    tracks the read-side TS/Python divergence)."""

    @pytest.mark.parametrize("target", ["[moltbook]", "[[some-note]]"])
    def test_rejects_bracket_leading_target(self, tmp_path, capsys, target):
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", target, "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "bracket" in capsys.readouterr().err
        assert target not in (tmp_path / "notes" / "src.md").read_text()


class TestAddDirContainment:
    """#411 — `add --dir` must not write outside the vault: textual check
    on the raw argument (os.path.join discards the vault prefix for an
    absolute value), then a physical realpath check for in-vault symlinks
    pointing outside. MCP has enforced both since #323."""

    def test_rejects_absolute_dir(self, tmp_path, capsys):
        outside = tmp_path / "outside"
        vault = tmp_path / "vault"
        vault.mkdir()
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note", directory=str(outside)),
                         str(vault), str(vault / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "invalid directory" in capsys.readouterr().err
        assert not outside.exists()

    def test_rejects_dotdot_dir(self, tmp_path, capsys):
        vault = tmp_path / "vault"
        vault.mkdir()
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note", directory="../escape"),
                         str(vault), str(vault / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "invalid directory" in capsys.readouterr().err
        assert not (tmp_path / "escape").exists()

    def test_rejects_symlinked_dir_escaping_vault(self, tmp_path, capsys):
        # 'evil' passes every textual check — only resolving what is actually
        # on disk can see the escape.
        outside = tmp_path / "outside"
        outside.mkdir()
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "evil").symlink_to(outside)
        with pytest.raises(SystemExit) as exc:
            commands.add(_add_args("Note", directory="evil"),
                         str(vault), str(vault / ".schist" / "schist.db"))
        assert exc.value.code == 1
        assert "resolves outside the vault" in capsys.readouterr().err
        assert list(outside.iterdir()) == []

    def test_nested_relative_dir_still_works(self, tmp_path, capsys):
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.add(_add_args("Note", directory="projects/sub"),
                         str(tmp_path), str(tmp_path / ".schist" / "schist.db"))
        capsys.readouterr()
        assert len(list((tmp_path / "projects" / "sub").glob("*.md"))) == 1


class TestLinkSourceContainment:
    """#411 — `link`'s source is joined onto the vault with the same two-layer
    guard as add --dir; a symlinked note pointing outside must not route the
    append through itself."""

    def test_rejects_dotdot_source(self, tmp_path, capsys):
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("../outside.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "invalid source" in capsys.readouterr().err

    def test_rejects_absolute_source(self, tmp_path, capsys):
        _vault_with_types(tmp_path)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args(str(tmp_path / "notes" / "src.md"), "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "invalid source" in capsys.readouterr().err

    def test_rejects_symlinked_source_escaping_vault(self, tmp_path, capsys):
        _vault_with_types(tmp_path)
        outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
        outside.write_text(
            "---\ntitle: Out\nstatus: draft\n---\n\nBody\n\n## Connections\n",
            encoding="utf-8",
        )
        before = outside.read_text(encoding="utf-8")
        (tmp_path / "notes" / "evil.md").symlink_to(outside)
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/evil.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        assert "resolves outside the vault" in capsys.readouterr().err
        assert outside.read_text(encoding="utf-8") == before


class TestLinkConcurrentAppend:
    """#412 — append_connection is read → insert → rewrite; without the vault
    write lock two concurrent links both read the same base content and the
    loser's edge silently vanishes."""

    def test_concurrent_links_keep_both_edges(self, tmp_path, capsys):
        import threading
        import time

        _vault_with_types(tmp_path)
        real_insert = markdown_io.insert_connection_line

        def slow_insert(content, line):
            # Widen the read→write window so an unserialized second link
            # deterministically reads the pre-append content.
            time.sleep(0.05)
            return real_insert(content, line)

        def run(target):
            commands.link(
                _link_args("notes/src.md", target, "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )

        with patch("schist.commands.git_ops.commit", return_value=(True, "")), \
             patch("schist.markdown_io.insert_connection_line", side_effect=slow_insert):
            threads = [threading.Thread(target=run, args=(t,))
                       for t in ("notes/b.md", "notes/c.md")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        capsys.readouterr()

        edges = _parsed_edges(tmp_path)
        assert sorted(e["target"] for e in edges) == ["notes/b.md", "notes/c.md"]


class TestConnectionTypeVocabTokens:
    """#413 — connection-type vocabulary entries are validated as
    round-trippable tokens at config load (an entry serializes into
    `- {type}: target` lines, so a whitespace-carrying entry passes the
    membership check and then writes a dead or forgeable line). Warn-and-
    drop; pinned against MCP loadVaultConfig by schema/vocab-token-parity.json."""

    @staticmethod
    def _fixture():
        import json
        fixture_path = Path(__file__).resolve().parents[2] / "schema" / "vocab-token-parity.json"
        return json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_fixture_parity(self, tmp_path, capsys):
        import json
        fixture = self._fixture()
        entries = [e["entry"] for e in fixture["entries"]]
        # JSON is valid YAML flow syntax, and json.dumps escapes the exotic
        # codepoints — no invisible literals land in this file or on disk.
        (tmp_path / "schist.yaml").write_text(
            f"connection_types: {json.dumps(entries)}\n", encoding="utf-8",
        )
        kept = commands._connection_types(str(tmp_path))
        err = capsys.readouterr().err
        assert kept == [e["entry"] for e in fixture["entries"] if e["kept"]]
        # Every dropped NON-EMPTY entry warns; the empty string is dropped
        # silently (getStringList .filter(Boolean) parity).
        dropped_warned = [e for e in fixture["entries"] if not e["kept"] and e["entry"]]
        assert err.count("ignoring connection_types entry") == len(dropped_warned)

    def test_dropped_junk_type_is_rejected_at_link(self, tmp_path, capsys):
        import json
        (tmp_path / "schist.yaml").write_text(
            f"connection_types: {json.dumps(['extends', 'in review'])}\n"
            "directories:\n  notes: notes/\n",
            encoding="utf-8",
        )
        notes = tmp_path / "notes"
        notes.mkdir()
        (notes / "src.md").write_text(
            "---\ntitle: Src\nstatus: draft\n---\n\nBody\n\n## Connections\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "in review"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        # The advertised vocabulary is exactly the filtered list.
        assert "connection_types: extends" in err
        with patch("schist.commands.git_ops.commit", return_value=(True, "")):
            commands.link(
                _link_args("notes/src.md", "notes/dst.md", "extends"),
                str(tmp_path), str(tmp_path / ".schist" / "schist.db"),
            )
        capsys.readouterr()
        assert "- extends: notes/dst.md" in (notes / "src.md").read_text(encoding="utf-8")
