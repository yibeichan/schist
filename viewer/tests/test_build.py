"""Unit tests for viewer/build.py — the static graph + search index builder.

These tests exercise the pure functions (`normalize_endpoint`, `build_graph`,
`build_search_index`) against an in-memory SQLite database shaped like the
real vault DB so that a schema drift in `cli/schist/schema.sql` or `build.py`
would be caught by CI instead of slipping through to a broken static build.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Make the viewer package importable without installing it.
VIEWER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VIEWER_DIR))

import build  # noqa: E402 — the path insert above is load-bearing


# ---------------------------------------------------------------------------
# Fixture: an in-memory DB mirroring the schist.db schema + a handful of rows
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_db() -> sqlite3.Connection:
    """Build an in-memory DB with the subset of tables `build.py` reads.

    Mirrors the production schema.sql for docs/concepts/edges. Kept minimal
    (no triggers, no FTS) because build.py doesn't touch those.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE docs (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            date        TEXT,
            status      TEXT DEFAULT 'draft',
            tags        TEXT,
            concepts    TEXT,
            body        TEXT NOT NULL,
            scope       TEXT DEFAULT 'global',
            source      TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE concepts (
            slug        TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT,
            tags        TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE edges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            target      TEXT NOT NULL,
            type        TEXT NOT NULL,
            context     TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(source, target, type)
        );
        """
    )
    # Two normal notes
    conn.execute(
        "INSERT INTO docs (id, title, date, status, tags, concepts, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "notes/2026-01-01-attention.md",
            "Attention is all you need",
            "2026-01-01",
            "final",
            '["transformer", "attention"]',
            '["attention"]',
            "Self-attention body text with enough content to excerpt.",
        ),
    )
    conn.execute(
        "INSERT INTO docs (id, title, date, status, tags, concepts, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "notes/2026-02-14-dropout.md",
            "Dropout",
            "2026-02-14",
            "draft",
            None,
            '["regularization"]',
            "Dropout notes body.",
        ),
    )
    # A concept markdown file (must be excluded from the note nodes)
    conn.execute(
        "INSERT INTO docs (id, title, date, status, tags, concepts, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "concepts/attention.md",
            "Attention",
            None,
            "final",
            None,
            None,
            "Concept body — should NOT appear as a note in the graph.",
        ),
    )
    # Concept nodes
    conn.execute(
        "INSERT INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, ?)",
        ("attention", "Attention", "Mechanism for weighting inputs", '["ml"]'),
    )
    conn.execute(
        "INSERT INTO concepts (slug, title, description, tags) VALUES (?, ?, ?, ?)",
        ("regularization", "Regularization", "Preventing overfitting", None),
    )
    # Edges
    conn.execute(
        "INSERT INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)",
        ("notes/2026-01-01-attention.md", "attention", "applies-method-of", "core"),
    )
    # An edge whose target is a concept stored as concepts/<slug>.md path —
    # build.py must normalise this to the bare slug.
    conn.execute(
        "INSERT INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)",
        ("notes/2026-02-14-dropout.md", "concepts/regularization.md", "extends", None),
    )
    # A dangling edge — target isn't in the graph, should be silently skipped
    conn.execute(
        "INSERT INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)",
        ("notes/2026-01-01-attention.md", "[external-ref]", "related", None),
    )
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# normalize_endpoint — pure function, no DB
# ---------------------------------------------------------------------------


class TestNormalizeEndpoint:
    def test_strips_concepts_prefix_and_md_suffix(self):
        assert build.normalize_endpoint("concepts/attention.md") == "attention"

    def test_strips_concepts_prefix_without_md(self):
        assert build.normalize_endpoint("concepts/attention") == "attention"

    def test_note_path_preserved_with_md(self):
        """Regression for the pre-PR#27 bug: note paths must keep their
        .md suffix so they can match the `id` in docs table."""
        assert (
            build.normalize_endpoint("notes/2026-01-01-foo.md")
            == "notes/2026-01-01-foo.md"
        )

    def test_papers_path_preserved(self):
        """Papers/ is a note-style directory — don't strip anything."""
        assert build.normalize_endpoint("papers/neurips.md") == "papers/neurips.md"

    def test_bare_slug_unchanged(self):
        assert build.normalize_endpoint("attention") == "attention"

    def test_external_reference_unchanged(self):
        """External refs like `[some-paper]` shouldn't be mangled either."""
        assert build.normalize_endpoint("[external-ref]") == "[external-ref]"


# ---------------------------------------------------------------------------
# build_graph — nodes, links, deduplication, dangling edge skip
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_notes_and_concepts_become_nodes(self, vault_db):
        graph = build.build_graph(vault_db)
        ids = {n["id"] for n in graph["nodes"]}
        # Two notes + two concept nodes
        assert "notes/2026-01-01-attention.md" in ids
        assert "notes/2026-02-14-dropout.md" in ids
        assert "attention" in ids
        assert "regularization" in ids

    def test_concept_markdown_excluded_from_note_nodes(self, vault_db):
        """`concepts/attention.md` row in docs MUST NOT produce a second
        node — the canonical node comes from the concepts table."""
        graph = build.build_graph(vault_db)
        ids = [n["id"] for n in graph["nodes"]]
        # Only one "attention" node, not two
        assert ids.count("attention") == 1
        assert "concepts/attention.md" not in ids

    def test_note_node_shape(self, vault_db):
        graph = build.build_graph(vault_db)
        note = next(
            n for n in graph["nodes"] if n["id"] == "notes/2026-01-01-attention.md"
        )
        assert note["label"] == "Attention is all you need"
        assert note["type"] == "note"
        assert note["date"] == "2026-01-01"
        assert note["status"] == "final"
        assert note["tags"] == ["transformer", "attention"]

    def test_concept_node_shape(self, vault_db):
        graph = build.build_graph(vault_db)
        concept = next(n for n in graph["nodes"] if n["id"] == "attention")
        assert concept["type"] == "concept"
        assert concept["label"] == "Attention"
        assert concept["description"] == "Mechanism for weighting inputs"

    def test_edge_endpoints_normalised(self, vault_db):
        """An edge whose target is stored as `concepts/regularization.md`
        must be normalised to the bare slug `regularization` so the link
        connects to the concept node."""
        graph = build.build_graph(vault_db)
        matching = [
            l for l in graph["links"]
            if l["source"] == "notes/2026-02-14-dropout.md"
            and l["target"] == "regularization"
        ]
        assert len(matching) == 1
        assert matching[0]["type"] == "extends"

    def test_dangling_edge_skipped(self, vault_db):
        """Edges whose endpoint isn't in the node set (e.g. `[external-ref]`)
        must be silently skipped, not emitted with a broken target."""
        graph = build.build_graph(vault_db)
        # No link should reference "[external-ref]"
        for link in graph["links"]:
            assert link["target"] != "[external-ref]"
            assert link["source"] != "[external-ref]"

    def test_link_context_preserved(self, vault_db):
        graph = build.build_graph(vault_db)
        core_link = next(
            l for l in graph["links"]
            if l["target"] == "attention" and l["type"] == "applies-method-of"
        )
        assert core_link["context"] == "core"

    def test_output_json_serializable(self, vault_db):
        """Whatever build_graph returns must round-trip through JSON."""
        graph = build.build_graph(vault_db)
        serialized = json.dumps(graph)
        restored = json.loads(serialized)
        assert restored == graph


# ---------------------------------------------------------------------------
# build_search_index — document store + excerpts
# ---------------------------------------------------------------------------


class TestBuildSearchIndex:
    def test_documents_and_store_for_notes_only(self, vault_db):
        index = build.build_search_index(vault_db)
        doc_ids = {d["id"] for d in index["documents"]}
        assert "notes/2026-01-01-attention.md" in doc_ids
        assert "notes/2026-02-14-dropout.md" in doc_ids
        # concepts/ files MUST be excluded from the search index
        assert "concepts/attention.md" not in doc_ids
        assert set(index["store"].keys()) == doc_ids

    def test_document_shape(self, vault_db):
        index = build.build_search_index(vault_db)
        doc = next(
            d for d in index["documents"] if d["id"] == "notes/2026-01-01-attention.md"
        )
        # lunr expects the tags field to be a space-joined string, not an array
        assert doc["tags"] == "transformer attention"
        assert doc["title"] == "Attention is all you need"
        assert doc["date"] == "2026-01-01"

    def test_store_keeps_full_body_tags_array(self, vault_db):
        """The store section keeps the full body and tags-as-array for the
        detail panel, even though `documents` section has truncated body."""
        index = build.build_search_index(vault_db)
        store = index["store"]["notes/2026-01-01-attention.md"]
        assert store["tags"] == ["transformer", "attention"]
        assert store["body"].startswith("Self-attention body text")

    def test_body_excerpt_truncated_and_flattened(self, vault_db):
        """The documents section's body is truncated to 500 chars and has
        newlines flattened to spaces (so lunr indexes cleanly)."""
        # Insert a doc with a long, multi-line body
        vault_db.execute(
            "INSERT INTO docs (id, title, date, status, tags, concepts, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "notes/2026-03-01-long.md",
                "Long Note",
                "2026-03-01",
                "draft",
                None,
                None,
                "line1\nline2\n" + ("x" * 600),
            ),
        )
        vault_db.commit()
        index = build.build_search_index(vault_db)
        doc = next(d for d in index["documents"] if d["id"] == "notes/2026-03-01-long.md")
        assert len(doc["body"]) <= 500
        assert "\n" not in doc["body"]

    def test_output_json_serializable(self, vault_db):
        index = build.build_search_index(vault_db)
        serialized = json.dumps(index)
        restored = json.loads(serialized)
        assert restored == index


# ---------------------------------------------------------------------------
# End-to-end: run build.main() against a real DB file
# ---------------------------------------------------------------------------


class TestBuildE2E:
    def test_main_writes_graph_and_search_index_files(self, tmp_path, vault_db, monkeypatch):
        """`python build.py --db X --out Y` writes both JSON files with
        the expected top-level keys."""
        # Persist the in-memory vault_db to disk
        db_path = tmp_path / "schist.db"
        disk_db = sqlite3.connect(str(db_path))
        try:
            vault_db.backup(disk_db)
        finally:
            disk_db.close()

        out_dir = tmp_path / "out"

        monkeypatch.setattr(sys, "argv", ["build.py", "--db", str(db_path), "--out", str(out_dir)])
        build.main()

        graph_out = out_dir / "graph.json"
        search_out = out_dir / "search-index.json"
        assert graph_out.is_file()
        assert search_out.is_file()

        graph = json.loads(graph_out.read_text())
        assert "nodes" in graph and "links" in graph
        assert len(graph["nodes"]) >= 4  # 2 notes + 2 concepts

        search = json.loads(search_out.read_text())
        assert "documents" in search and "store" in search
        assert len(search["documents"]) >= 2  # 2 notes

    def test_main_errors_on_missing_db(self, tmp_path, monkeypatch):
        """Missing --db path → clean SystemExit with a helpful message."""
        missing = tmp_path / "absent.db"
        out_dir = tmp_path / "out"
        monkeypatch.setattr(
            sys, "argv", ["build.py", "--db", str(missing), "--out", str(out_dir)]
        )
        with pytest.raises(SystemExit) as exc_info:
            build.main()
        assert "database not found" in str(exc_info.value)
