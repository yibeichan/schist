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


def test_title_slug_is_linear_on_huge_whitespace() -> None:
    """Whitespace collapse is a single [class]+ pass and the edge strip is
    str.strip — never a `^[ws]+|[ws]+$` alternated anchored regex, which
    backtracks quadratically over interior runs (minutes at this size)."""
    big = "a" + " " * 1_000_000 + "b"
    t0 = time.perf_counter()
    assert slugify(big) == "a-b"
    assert time.perf_counter() - t0 < 5.0  # linear is a few ms; huge CI margin
