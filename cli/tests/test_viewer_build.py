"""Unit tests for schist.viewer.build — the static graph + search index builder.

These tests exercise the pure functions (`normalize_endpoint`, `build_graph`,
`build_search_index`) against an in-memory SQLite database shaped like the
real vault DB so that a schema drift in `cli/schist/schema.sql` or the builder
would be caught by CI instead of slipping through to a broken static build.

`TestShapeContract` checks the builder's output against what `index.html`
reads, and `TestSchistBuildCommand` runs `schist build` itself (#432: the CLI
used to carry its own builder, whose output the page could not render).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from schist.viewer import build

REPO_ROOT = Path(__file__).resolve().parents[2]


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
    def test_main_writes_graph_and_search_index_files(self, tmp_path, vault_db):
        """`python -m schist.viewer.build --db X --out Y` writes both JSON
        files with the expected top-level keys, plus the page."""
        # Persist the in-memory vault_db to disk
        db_path = tmp_path / "schist.db"
        disk_db = sqlite3.connect(str(db_path))
        try:
            vault_db.backup(disk_db)
        finally:
            disk_db.close()

        out_dir = tmp_path / "out"

        build.main(["--db", str(db_path), "--out", str(out_dir)])

        graph_out = out_dir / "graph.json"
        search_out = out_dir / "search-index.json"
        assert graph_out.is_file()
        assert search_out.is_file()
        assert (out_dir / "index.html").read_bytes() == build.INDEX_HTML.read_bytes()

        graph = json.loads(graph_out.read_text())
        assert "nodes" in graph and "links" in graph
        assert len(graph["nodes"]) >= 4  # 2 notes + 2 concepts

        search = json.loads(search_out.read_text())
        assert "documents" in search and "store" in search
        assert len(search["documents"]) >= 2  # 2 notes

    def test_main_errors_on_missing_db(self, tmp_path):
        """Missing --db path → clean SystemExit with a helpful message."""
        missing = tmp_path / "absent.db"
        out_dir = tmp_path / "out"
        with pytest.raises(SystemExit) as exc_info:
            build.main(["--db", str(missing), "--out", str(out_dir)])
        assert "database not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Shape contract: what the builder writes is what index.html reads
# ---------------------------------------------------------------------------


def _persist(conn: sqlite3.Connection, path: Path) -> Path:
    disk = sqlite3.connect(str(path))
    try:
        conn.backup(disk)
    finally:
        disk.close()
    return path


class TestShapeContract:
    """Pins both sides of the build → page interface. If the page starts
    reading a different key, or the builder stops writing one, a test here
    fails instead of the viewer rendering an empty graph."""

    PAGE = build.INDEX_HTML.read_text(encoding="utf-8")

    def test_page_reads_the_keys_the_builder_writes(self):
        assert 'fetch("graph.json")' in self.PAGE
        assert 'fetch("search-index.json")' in self.PAGE
        assert re.search(r"const\s*\{\s*nodes,\s*links\s*\}\s*=\s*graphData", self.PAGE)
        assert re.search(r"const\s*\{\s*documents,\s*store\s*\}\s*=\s*searchData", self.PAGE)

    def test_page_node_types_match_builder_node_types(self, vault_db):
        page_types = set(re.findall(r'\.type === "(\w+)"', self.PAGE))
        graph_types = {n["type"] for n in build.build_graph(vault_db)["nodes"]}
        assert graph_types == {"note", "concept"}
        assert page_types == graph_types

    def test_built_files_satisfy_the_page(self, tmp_path, vault_db):
        out = tmp_path / "site"
        build.write_site(_persist(vault_db, tmp_path / "schist.db"), out)
        graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
        search = json.loads((out / "search-index.json").read_text(encoding="utf-8"))

        assert set(graph) == {"nodes", "links"}
        assert set(search) == {"documents", "store"}
        ids = {n["id"] for n in graph["nodes"]}
        for link in graph["links"]:
            assert link["source"] in ids and link["target"] in ids
        # The detail panel looks up note metadata by node id in `store`.
        note_ids = {n["id"] for n in graph["nodes"] if n["type"] == "note"}
        assert set(search["store"]) == note_ids
        assert {d["id"] for d in search["documents"]} == note_ids
        assert (out / "index.html").is_file()

    def test_missing_page_raises(self, tmp_path, vault_db, monkeypatch):
        monkeypatch.setattr(build, "INDEX_HTML", tmp_path / "absent.html")
        with pytest.raises(FileNotFoundError, match="reinstall schist"):
            build.write_site(_persist(vault_db, tmp_path / "schist.db"), tmp_path / "site")


# ---------------------------------------------------------------------------
# `schist build` and the repo-root shim produce the same site
# ---------------------------------------------------------------------------


def _vault(root: Path) -> Path:
    (root / "notes").mkdir(parents=True)
    (root / "concepts").mkdir()
    (root / "notes" / "2026-04-16-hello.md").write_text(
        "---\ntitle: Hello\ndate: 2026-04-16\ntags: [smoke]\nstatus: draft\n"
        "concepts: [ingestion]\n---\n\nBody.\n\n## Connections\n"
        "- extends: concepts/ingestion.md\n",
        encoding="utf-8",
    )
    (root / "concepts" / "ingestion.md").write_text(
        "---\nconcept: ingestion\ntitle: Ingestion\n---\n\nMarkdown to SQLite.\n",
        encoding="utf-8",
    )
    return root


class TestSchistBuildCommand:
    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, *argv], capture_output=True, text=True, check=True
        )

    def test_schist_build_writes_a_site_the_page_can_render(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        out = tmp_path / "site"
        self._run("-m", "schist", "--vault", str(vault), "build", "--out", str(out))

        graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
        search = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        assert set(graph) == {"nodes", "links"}
        assert {n["type"] for n in graph["nodes"]} == {"note", "concept"}
        # The concepts/ingestion.md target arrives as the bare concept slug.
        assert {
            "source": "notes/2026-04-16-hello.md",
            "target": "ingestion",
            "type": "extends",
            "context": "",
        } in graph["links"]
        assert list(search["store"]) == ["notes/2026-04-16-hello.md"]
        assert (out / "index.html").read_bytes() == build.INDEX_HTML.read_bytes()

    def test_schist_build_default_out_is_vault_data_dir(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        self._run("-m", "schist", "--vault", str(vault), "build")
        assert (vault / ".schist" / "data" / "index.html").is_file()

    def test_repo_shim_ignores_an_installed_schist_of_another_version(self, tmp_path):
        """An older `schist` without `schist.viewer` first on the path must not
        stop the shim: it loads the checkout's builder by file path. `-S` keeps
        site-packages (and an editable install's import hook) out of the way,
        so only the fake package can answer `import schist`."""
        vault = _vault(tmp_path / "vault")
        self._run("-m", "schist", "--vault", str(vault), "build", "--out", str(tmp_path / "cli"))
        old = tmp_path / "old-install" / "schist"
        old.mkdir(parents=True)
        (old / "__init__.py").write_text("", encoding="utf-8")
        out = tmp_path / "shim"
        subprocess.run(
            [sys.executable, "-S", str(REPO_ROOT / "viewer" / "build.py"),
             "--db", str(vault / ".schist" / "schist.db"), "--out", str(out)],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONPATH": str(old.parent)},
        )
        assert (out / "index.html").read_bytes() == build.INDEX_HTML.read_bytes()

    def test_copied_page_is_writable_when_installed_page_is_not(self, tmp_path, vault_db, monkeypatch):
        page = tmp_path / "installed" / "index.html"
        page.parent.mkdir()
        page.write_text("<!doctype html>", encoding="utf-8")
        page.chmod(0o444)
        monkeypatch.setattr(build, "INDEX_HTML", page)
        db = _persist(vault_db, tmp_path / "schist.db")
        build.write_site(db, tmp_path / "site")
        build.write_site(db, tmp_path / "site")  # second build overwrites the copy
        assert os.access(tmp_path / "site" / "index.html", os.W_OK)

    def test_repo_shim_matches_schist_build(self, tmp_path):
        vault = _vault(tmp_path / "vault")
        via_cli, via_shim = tmp_path / "cli", tmp_path / "shim"
        self._run("-m", "schist", "--vault", str(vault), "build", "--out", str(via_cli))
        db = vault / ".schist" / "schist.db"
        self._run(str(REPO_ROOT / "viewer" / "build.py"), "--db", str(db), "--out", str(via_shim))
        for name in ("graph.json", "search-index.json", "index.html"):
            assert (via_shim / name).read_bytes() == (via_cli / name).read_bytes()
