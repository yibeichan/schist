"""Tests for markdown_io — title slugify cross-language parity (#338).

Python markdown_io.slugify and TS titleSlug (mcp-server/src/tools.ts) must
produce byte-identical slugs: the slug is embedded in the note id (filename),
so any skew means the two languages mint different ids for the same title.
schema/title-slug-parity.json is the single source of truth, consumed here
and by mcp-server/tests/title-slug-parity.test.ts. Modeled on the #318
concept-slug parity contract.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from schist.markdown_io import SLUG_WS_CHARS, slugify


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _title_slug_parity_cases() -> list[dict]:
    fixture = _repo_root() / "schema" / "title-slug-parity.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_title_slug_parity_fixture_is_nontrivial() -> None:
    """An emptied/mangled fixture must fail loudly, not skip-collect green
    (parametrize over an empty list collects as a single skipped test)."""
    assert len(_title_slug_parity_cases()) >= 20


@pytest.mark.parametrize("case", _title_slug_parity_cases(), ids=lambda c: c["name"])
def test_title_slug_matches_shared_parity_cases(case: dict) -> None:
    assert slugify(case["input"]) == case["slug"]


def test_title_slug_ws_chars_match_ingest() -> None:
    """markdown_io and ingest each carry a copy of the explicit whitespace
    union (ingest.py must stay import-free of markdown_io for its bare-script
    hook copy); this pins the two copies byte-identical."""
    from schist.ingest import _SLUG_WS_CHARS

    assert SLUG_WS_CHARS == _SLUG_WS_CHARS


def _connection_append_parity_cases() -> list[dict]:
    fixture = _repo_root() / "schema" / "connection-append-parity.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_connection_append_parity_fixture_is_nontrivial() -> None:
    """An emptied/mangled fixture must fail loudly, not skip-collect green."""
    assert len(_connection_append_parity_cases()) >= 12


@pytest.mark.parametrize(
    "case", _connection_append_parity_cases(), ids=lambda c: c["name"]
)
def test_insert_connection_line_matches_shared_parity_cases(case: dict) -> None:
    """insert_connection_line and TS insertConnectionLine must be
    byte-identical (#365/#366): both writers splitlines-scan, both output
    '\\n'-joined content, so an edge written by either language lands where
    BOTH readers (ingest.py parse_connections / TS parseConnections) index it.
    """
    from schist.markdown_io import insert_connection_line

    assert insert_connection_line(case["content"], case["line"]) == case["expected"]


@pytest.mark.parametrize(
    "name,sep",
    [
        ("NEL", "\x85"),
        ("LS", "\u2028"),
        ("PS", "\u2029"),
        ("VT", "\v"),
        ("FF", "\f"),
    ],
)
def test_append_connection_edge_is_indexed_on_splitlines_only_separators(
    tmp_path, name: str, sep: str
) -> None:
    """End-to-end #365: universal-newline reads translate \\r/\\r\\n but NOT
    these separators, so the old split('\\n') fused the note into one line and
    the appended edge landed after ## Notes — parse_connections then silently
    ignored it while `schist link` reported success."""
    from schist.ingest import parse_connections
    from schist.markdown_io import append_connection

    note = tmp_path / "note.md"
    note.write_text(
        f"## Connections{sep}- supports: notes/x.md{sep}## Notes{sep}Body.",
        encoding="utf-8",
        newline="",
    )
    append_connection(str(note), "extends", "notes/b.md")

    edges = parse_connections(note.read_text(encoding="utf-8"))
    pairs = {(e["type"], e["target"]) for e in edges}
    assert ("extends", "notes/b.md") in pairs, f"{name}-separated edge dropped"
    assert ("supports", "notes/x.md") in pairs


def test_append_connection_golden_path_and_context(tmp_path) -> None:
    """append_connection had ZERO coverage before #365 despite being the core
    write behind `schist link`. Pin the golden path + context quoting."""
    from schist.ingest import parse_connections
    from schist.markdown_io import append_connection

    note = tmp_path / "note.md"
    note.write_text(
        "---\ntitle: A\n---\n\n## Connections\n\n- supports: notes/x.md\n\n## Notes\nBody.\n",
        encoding="utf-8",
    )
    append_connection(str(note), "refutes", "notes/c.md", context="disagrees")

    content = note.read_text(encoding="utf-8")
    assert '- refutes: notes/c.md "disagrees"' in content
    assert content.endswith("\n")
    edges = parse_connections(content)
    assert {(e["type"], e["target"]) for e in edges} == {
        ("supports", "notes/x.md"),
        ("refutes", "notes/c.md"),
    }


def test_append_connection_creates_section_when_missing(tmp_path) -> None:
    from schist.ingest import parse_connections
    from schist.markdown_io import append_connection

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")
    append_connection(str(note), "extends", "notes/b.md")

    edges = parse_connections(note.read_text(encoding="utf-8"))
    assert {(e["type"], e["target"]) for e in edges} == {("extends", "notes/b.md")}


def test_append_connection_preserves_original_on_write_failure(tmp_path, monkeypatch) -> None:
    """A plain `open(path,'w')` truncates at open() — a crash between truncation
    and the write left a zero-byte note (#425). append_connection now writes to
    a temp file + os.replace, so a failure at the rename must leave the ORIGINAL
    content intact (never an empty file) and must not leak the temp file."""
    import schist.markdown_io as mio

    note = tmp_path / "note.md"
    original = "---\ntitle: A\n---\n\n## Connections\n\n- supports: notes/x.md\n"
    note.write_text(original, encoding="utf-8")

    # Simulate a kill/failure at the atomic-rename step (after temp is written).
    def boom(_src, _dst):
        raise OSError("simulated crash at rename")

    monkeypatch.setattr(mio.os, "replace", boom)
    with pytest.raises(OSError):
        mio.append_connection(str(note), "extends", "notes/b.md")

    # Original note survives byte-for-byte — no truncation, no partial write.
    assert note.read_text(encoding="utf-8") == original
    # The temp file was cleaned up — nothing left behind in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["note.md"]


def test_append_connection_writes_atomically_via_replace(tmp_path, monkeypatch) -> None:
    """The write-back must go through os.replace (atomic rename), not a direct
    truncating open() on the target."""
    import schist.markdown_io as mio

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")

    calls = {"replace": 0}
    real_replace = mio.os.replace

    def counting_replace(src, dst):
        calls["replace"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(mio.os, "replace", counting_replace)
    mio.append_connection(str(note), "extends", "notes/b.md")

    assert calls["replace"] == 1
    assert "- extends: notes/b.md" in note.read_text(encoding="utf-8")


def test_append_connection_preserves_file_mode(tmp_path) -> None:
    """os.replace renames the mkstemp temp (0600) over the note, so without mode
    preservation an append silently drops the note to 0600. git tracks only the
    exec bit, so the change would be invisible in history but real on disk."""
    import os
    import stat

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")
    os.chmod(note, 0o644)

    from schist.markdown_io import append_connection

    append_connection(str(note), "extends", "notes/b.md")

    assert stat.S_IMODE(os.stat(note).st_mode) == 0o644


def test_atomic_write_temp_lives_in_schist_tmp_when_vault_root_given(tmp_path) -> None:
    """#433: with a vault_root, the atomic-write temp is created under
    <vault>/.schist/tmp/ — gitignored (`.schist/`) and never a sync scope — so
    a hard kill in the write→replace window can't strand an orphan under a
    synced scope dir (notes/) that the next `schist sync push` would stage,
    commit, and fan out to the hub. Capture the rename source to assert location
    AND confirm the note content still lands correctly across the two dirs."""
    import os

    import schist.markdown_io as mio

    vault = tmp_path
    (vault / ".schist").mkdir()
    notes = vault / "notes"
    notes.mkdir()
    note = notes / "a.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")

    seen: dict[str, str] = {}
    real_replace = mio.os.replace

    def capturing_replace(src, dst):
        seen["src"] = src
        return real_replace(src, dst)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mio.os, "replace", capturing_replace)
    try:
        mio.append_connection(str(note), "extends", "notes/b.md", vault_root=str(vault))
    finally:
        monkeypatch.undo()

    # Temp came from .schist/tmp/, not from the note's own (synced) directory.
    assert seen["src"].startswith(str(vault / ".schist" / "tmp") + os.sep)
    # No *.tmp orphan left in the synced scope dir.
    assert list(notes.glob("*.tmp")) == []
    # Cross-directory os.replace still landed the new content atomically.
    assert "- extends: notes/b.md" in note.read_text(encoding="utf-8")


def test_orphaned_temp_on_hard_kill_is_confined_to_schist_tmp(tmp_path) -> None:
    """Models the exact #433 failure: a hard kill (SIGKILL/OOM/power-loss)
    between the temp write and os.replace runs NO cleanup — the `except`/unlink
    path never executes. The orphaned temp must be confined to .schist/tmp/
    (gitignored, non-scope), NOT left under notes/ where sync would commit it."""
    import schist.markdown_io as mio

    vault = tmp_path
    (vault / ".schist").mkdir()
    notes = vault / "notes"
    notes.mkdir()
    note = notes / "a.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")

    def crash(_src, _dst):
        raise OSError("simulated hard kill at rename")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mio.os, "replace", crash)
    # Neutralize the error-path cleanup so the temp survives, as it would after
    # a real SIGKILL (which never runs the `except` block at all).
    monkeypatch.setattr(mio.os, "unlink", lambda *_a, **_k: None)
    try:
        with pytest.raises(OSError):
            mio.append_connection(str(note), "extends", "notes/b.md", vault_root=str(vault))
    finally:
        monkeypatch.undo()

    # Orphan is in .schist/tmp/ (inert), never under the synced scope dir.
    assert list(notes.glob("*.tmp")) == [], "orphan leaked into a synced scope dir"
    assert list((vault / ".schist" / "tmp").glob("*.tmp")), "temp should orphan under .schist/tmp"
    # Original content is untouched (the write never reached the target).
    assert note.read_text(encoding="utf-8") == "---\ntitle: A\n---\nBody.\n"


def test_atomic_write_without_vault_root_falls_back_to_target_dir(tmp_path) -> None:
    """No vault_root (bare note / non-vault caller) keeps the original same-dir
    temp behavior — the fallback must stay same-filesystem so os.replace is
    still atomic, and must not require a .schist/ dir that doesn't exist."""
    import os

    import schist.markdown_io as mio

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")

    seen: dict[str, str] = {}
    real_replace = mio.os.replace

    def capturing_replace(src, dst):
        seen["src"] = src
        return real_replace(src, dst)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mio.os, "replace", capturing_replace)
    try:
        mio.append_connection(str(note), "extends", "notes/b.md")
    finally:
        monkeypatch.undo()

    assert os.path.dirname(seen["src"]) == str(tmp_path)
    assert "- extends: notes/b.md" in note.read_text(encoding="utf-8")


def test_atomic_write_falls_back_to_target_dir_on_cross_fs_exdev(tmp_path) -> None:
    """#433 review: if `.schist/` is on a different filesystem than the note
    (e.g. a vault on NFS with `.schist` symlinked to local disk), the replace
    from .schist/tmp raises EXDEV. The write must fall back to a same-dir temp
    (guaranteed same-fs) and SUCCEED — a rare orphan beats a total write outage.
    A non-EXDEV OSError must still propagate."""
    import errno
    import os

    import schist.markdown_io as mio

    vault = tmp_path
    (vault / ".schist").mkdir()
    notes = vault / "notes"
    notes.mkdir()
    note = notes / "a.md"
    note.write_text("---\ntitle: A\n---\nBody.\n", encoding="utf-8")

    schist_tmp = str(vault / ".schist" / "tmp")
    real_replace = mio.os.replace
    srcs: list[str] = []

    def exdev_from_schist(src, dst):
        srcs.append(src)
        # Only the .schist/tmp attempt "crosses" a filesystem boundary.
        if os.path.dirname(src) == schist_tmp:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mio.os, "replace", exdev_from_schist)
    try:
        mio.append_connection(str(note), "extends", "notes/b.md", vault_root=str(vault))
    finally:
        monkeypatch.undo()

    # First attempt was .schist/tmp (EXDEV), retry landed in the note's own dir.
    assert os.path.dirname(srcs[0]) == schist_tmp
    assert os.path.dirname(srcs[-1]) == str(notes)
    # The write ultimately succeeded — content is present, no leftover temp.
    assert "- extends: notes/b.md" in note.read_text(encoding="utf-8")
    assert list(notes.glob("*.tmp")) == []
    assert list((vault / ".schist" / "tmp").glob("*.tmp")) == []


def test_atomic_write_non_exdev_oserror_still_propagates(tmp_path) -> None:
    """The EXDEV fallback must not swallow OTHER replace failures (disk full,
    permission): those are real errors and must raise, leaving the original
    note intact and no leaked temp."""
    import errno

    import schist.markdown_io as mio

    vault = tmp_path
    (vault / ".schist").mkdir()
    notes = vault / "notes"
    notes.mkdir()
    note = notes / "a.md"
    original = "---\ntitle: A\n---\nBody.\n"
    note.write_text(original, encoding="utf-8")

    def enospc(_src, _dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mio.os, "replace", enospc)
    try:
        with pytest.raises(OSError) as ei:
            mio.append_connection(str(note), "extends", "notes/b.md", vault_root=str(vault))
        assert ei.value.errno == errno.ENOSPC
    finally:
        monkeypatch.undo()

    assert note.read_text(encoding="utf-8") == original
    assert list((vault / ".schist" / "tmp").glob("*.tmp")) == []


def test_title_slug_is_linear_on_huge_whitespace() -> None:
    """Whitespace collapse is a single [class]+ pass and the edge strip is
    str.strip — never a `^[ws]+|[ws]+$` alternated anchored regex, which
    backtracks quadratically over interior runs (minutes at this size)."""
    big = "a" + " " * 1_000_000 + "b"
    t0 = time.perf_counter()
    assert slugify(big) == "a-b"
    assert time.perf_counter() - t0 < 5.0  # linear is a few ms; huge CI margin


def test_line_boundary_chars_is_exactly_the_splitlines_set() -> None:
    """LINE_BOUNDARY_CHARS drives sanitize_context's flatten pass and mirrors
    the TS LINE_BOUNDARY set (#398/#405). Pin it against the REAL splitter by
    exhaustive scan so it can never drift from what ingest actually splits on
    — the same never-disagree property contains_line_boundary gets for free
    by calling splitlines() directly."""
    from schist.markdown_io import LINE_BOUNDARY_CHARS

    boundaries = {
        chr(c) for c in range(0x110000)
        if len(("a" + chr(c) + "a").splitlines()) > 1
    }
    assert set(LINE_BOUNDARY_CHARS) == boundaries


def test_contains_line_boundary_detects_each_boundary_and_nothing_else() -> None:
    from schist.markdown_io import LINE_BOUNDARY_CHARS, contains_line_boundary

    for ch in LINE_BOUNDARY_CHARS:
        assert contains_line_boundary(f"notes/b.md{ch}- supports: evil.md"), hex(ord(ch))
        assert contains_line_boundary(ch)  # boundary-only string
        assert contains_line_boundary(f"trailing{ch}")  # splitlines drops the
        # trailing empty segment, so a naive len()>1 check would miss this
    assert not contains_line_boundary("notes/b.md")
    assert not contains_line_boundary("")
    assert not contains_line_boundary("tab\tand\xa0nbsp are ws, not boundaries")


def test_sanitize_context_flattens_boundaries_and_strips_forgery() -> None:
    from schist.markdown_io import sanitize_context

    # Every boundary becomes a space — the payload tail stays INSIDE the
    # single quoted context instead of becoming its own line on read.
    assert sanitize_context("a\vb\fc") == "a b c"
    # Leading connection-entry-looking prefix is dropped (defense-in-depth).
    assert sanitize_context("- extends: evil.md tail") == "evil.md tail"
    # Double-quotes would terminate the "..." delimiter early; they become
    # single-quotes, matching TS sanitizeContext.
    assert sanitize_context('she said "no"') == "she said 'no'"
    # All-boundary context sanitizes to '' (caller then omits it).
    assert sanitize_context("\n\r\u2028") == ""
    # Edge strip uses the pinned SLUG_WS_CHARS union, so a pasted BOM
    # (U+FEFF - in JS's trim set but not Python's str.strip default) is
    # dropped like TS sanitizeContext does.
    assert sanitize_context("\ufeffhello") == "hello"


def test_append_connection_context_injection_yields_single_edge(tmp_path) -> None:
    """#405: a context payload carrying a boundary + forged entry must come
    back from ingest's parser as ONE edge, exactly like the MCP #398 fix."""
    from schist.ingest import parse_connections
    from schist.markdown_io import append_connection

    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A\n---\n\n## Connections\n", encoding="utf-8")
    append_connection(str(note), "extends", "notes/b.md",
                      context='x"\u2028- supports: notes/evil.md')

    edges = parse_connections(note.read_text(encoding="utf-8"))
    assert [(e["type"], e["target"]) for e in edges] == [("extends", "notes/b.md")]


def test_write_note_exclusive_refuses_existing_file_and_symlink(tmp_path) -> None:
    """Second /review batch on #406: exclusive=True is the race-safe collision
    check (O_CREAT|O_EXCL, mirroring MCP writeNote's "wx"). It must fail on an
    existing file instead of truncating, and must refuse a symlink at the path
    atomically — dangling included — instead of writing through it."""
    from schist.markdown_io import read_note, write_note

    existing = tmp_path / "existing.md"
    write_note(str(existing), {"title": "A"}, "first")
    with pytest.raises(FileExistsError):
        write_note(str(existing), {"title": "B"}, "second", exclusive=True)
    assert read_note(str(existing))["body"] == "first"

    outside = tmp_path / "outside.md"
    dangling = tmp_path / "dangling.md"
    dangling.symlink_to(outside)
    with pytest.raises(FileExistsError):
        write_note(str(dangling), {"title": "C"}, "escaped", exclusive=True)
    assert not outside.exists()
