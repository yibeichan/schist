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


def test_title_slug_is_linear_on_huge_whitespace() -> None:
    """Whitespace collapse is a single [class]+ pass and the edge strip is
    str.strip — never a `^[ws]+|[ws]+$` alternated anchored regex, which
    backtracks quadratically over interior runs (minutes at this size)."""
    big = "a" + " " * 1_000_000 + "b"
    t0 = time.perf_counter()
    assert slugify(big) == "a-b"
    assert time.perf_counter() - t0 < 5.0  # linear is a few ms; huge CI margin
