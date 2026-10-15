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


# Property reads in index.html, by the variable names the page uses for each
# kind of object. A trailing `(` marks a method call (`meta.appendChild(`), and
# D3 adds its own simulation fields to nodes and resolves link endpoints in
# place, so neither comes from the builder.
_D3_FIELDS = {"x", "y", "fx", "fy", "vx", "vy", "index", "source", "target"}


def _page_reads(page: str, receivers: tuple[str, ...]) -> set[str]:
    alt = "|".join(receivers)
    return set(re.findall(rf"\b(?:{alt})\??\.([A-Za-z_]\w*)\b(?!\s*\()", page))


class TestShapeContract:
    """Pins both sides of the build → page interface: the files and top-level
    keys the page loads, the node types it branches on, and every field it
    reads from a node, a store entry or a link. If the page starts reading a
    field the builder does not write, or the builder stops writing one the
    page reads, a test here fails instead of the viewer rendering blanks."""

    PAGE = build.INDEX_HTML.read_text(encoding="utf-8")

    def test_external_scripts_are_pinned_with_integrity(self):
        """The page holds every note's full text, so a CDN script it loads must
        be an exact version checked by Subresource Integrity, not a floating
        `d3@7` the CDN can change under it."""
        tags = re.findall(r"<script\b[^>]*\bsrc=\"(https?://[^\"]+)\"[^>]*>", self.PAGE)
        assert len(tags) >= 2  # d3 and lunr; guards against a regex that matches nothing
        for tag in re.findall(r"<script\b[^>]*\bsrc=\"https?://[^>]*>", self.PAGE):
            src = re.search(r'src="([^"]+)"', tag).group(1)
            assert re.search(r"@\d+\.\d+\.\d+/", src), f"unpinned version: {src}"
            assert re.search(r'integrity="sha(256|384|512)-[A-Za-z0-9+/=]+"', tag), f"no integrity: {src}"
            assert 'crossorigin="anonymous"' in tag, f"no crossorigin: {src}"

    def test_button_rows_reset_the_browser_button_look(self):
        """Result and connection rows are <button>s. Without a reset they take
        the browser's default button background and text colour, so on the
        dark page each row is a white box with dark text."""
        # The graph script's buttons are rows; the Ask panel's are styled
        # buttons, checked in test_ask_buttons_are_styled.
        main = self.PAGE[:self.PAGE.index("// ── Ask the vault")]
        classes = set(re.findall(r'el\("button",\s*"([\w-]+)', main))
        assert {"result-item", "conn-item"} <= classes  # guards against a regex that matches nothing
        css = re.sub(r"/\*.*?\*/", "", self.PAGE, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
        for cls in classes:
            decls = " ".join(body for sel, body in rules
                             if f".{cls}" in [s.strip() for s in sel.split(",")])
            assert re.search(r"background:\s*(transparent|none)", decls), f".{cls} keeps the button background"
            assert re.search(r"color:\s*inherit", decls), f".{cls} keeps the button text colour"
            assert re.search(r"text-align:\s*left", decls), f".{cls} keeps centred button text"

    def test_rebuild_bar_is_hidden_unless_the_server_answers(self):
        """The Rebuild button needs a server with /api/status; served as plain
        static files the page must look as before. The rebuild POST carries
        the X-Schist-Rebuild header the server requires, which a cross-site
        form cannot send."""
        assert re.search(r'<div id="build-bar" hidden>', self.PAGE)
        assert 'fetch("api/status"' in self.PAGE
        post = re.search(r'fetch\("api/rebuild",\s*\{([^}]*\{[^}]*\}[^}]*)\}', self.PAGE)
        assert post, "no POST to api/rebuild found"
        assert 'method: "POST"' in post.group(1)
        assert '"X-Schist-Rebuild": "1"' in post.group(1)

    def test_ask_panel_is_hidden_unless_the_server_has_an_agent(self):
        """The Ask button needs `schist serve`; served as plain static files the
        page must look as before. Every agent POST (chat, approve, reject)
        carries the X-Schist-Agent header the server requires."""
        assert re.search(r'<button id="ask-btn" type="button" hidden>', self.PAGE)
        assert 'fetch("api/agent/status"' in self.PAGE
        assert re.search(r'const HDR = \{[^}]*"X-Schist-Agent": "1"', self.PAGE)
        chat = re.search(r'fetch\("api/agent/chat",\s*\{([^}]*)\}', self.PAGE)
        assert chat and 'method: "POST"' in chat.group(1) and "headers: HDR" in chat.group(1)
        decide = re.search(r'fetch\(`api/proposals/\$\{encodeURIComponent\(p\.id\)\}/\$\{action\}`,\s*\{([^}]*)\}', self.PAGE)
        assert decide and 'method: "POST"' in decide.group(1) and "headers: HDR" in decide.group(1)

    def test_ask_answers_render_through_the_sanitized_renderer(self):
        """Agent answers quote vault text, so they render through renderBody
        (DOMPurify) and citations are added as DOM nodes, never as HTML."""
        assert "renderMarkdown: renderBody" in self.PAGE
        ask = self.PAGE[self.PAGE.index("// ── Ask the vault"):]
        assert "viewer.renderMarkdown(target, md)" in ask
        assert "innerHTML" not in ask and "insertAdjacentHTML" not in ask

    def test_ask_buttons_are_styled(self):
        """Every button the Ask panel builds sets its own background and colour,
        so none renders as a default white button on the dark page."""
        ask = self.PAGE[self.PAGE.index("// ── Ask the vault"):]
        classes = {c.split()[0] for c in re.findall(r'el\("button",\s*"([\w -]+)', ask)}
        assert {"cite", "src", "proposal-chip", "p-approve", "p-reject"} <= classes
        css = re.sub(r"/\*.*?\*/", "", self.PAGE, flags=re.S)
        rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
        for cls in classes:
            decls = " ".join(body for sel, body in rules
                             if any(re.search(rf"\.{cls}(?![\w-])", part) for part in sel.split(",")))
            assert re.search(r"background:", decls), f".{cls} keeps the button background"
            assert re.search(r"(?<![-\w])color:", decls), f".{cls} keeps the button text colour"

    def test_vault_html_only_through_dompurify(self):
        """Note bodies are rendered as Markdown into innerHTML. Every innerHTML
        write must be DOMPurify.sanitize(...) output, so a note cannot inject
        script into a page that holds the whole vault."""
        writes = re.findall(r"\.innerHTML\s*=\s*([^;]+);", self.PAGE)
        assert writes, "no innerHTML write found; update this test if rendering moved"
        for rhs in writes:
            assert rhs.strip().startswith("DOMPurify.sanitize("), f"unsanitized innerHTML: {rhs.strip()}"
        assert not re.search(r"insertAdjacentHTML|outerHTML\s*=|document\.write", self.PAGE)
        # Opening a note must not fetch from third-party hosts.
        forbid = re.search(r"FORBID_TAGS:\s*\[([^\]]*)\]", self.PAGE).group(1)
        for tag in ("img", "iframe", "video", "audio", "object", "embed"):
            assert f'"{tag}"' in forbid, f"<{tag}> is not forbidden"

    def test_page_reads_only_fields_the_builder_writes(self, vault_db):
        graph = build.build_graph(vault_db)
        search = build.build_search_index(vault_db)
        node_keys = set().union(*(n.keys() for n in graph["nodes"]))
        store_keys = set().union(*(v.keys() for v in search["store"].values()))
        link_keys = set().union(*(link.keys() for link in graph["links"]))

        node_reads = _page_reads(self.PAGE, ("n", "d", "node", "peer")) - _D3_FIELDS
        store_reads = _page_reads(self.PAGE, ("meta",))
        link_reads = _page_reads(self.PAGE, ("l",))
        # Non-empty, so a renamed variable cannot make the subset checks vacuous.
        assert {"label", "type"} <= node_reads
        assert "body" in store_reads
        assert "type" in link_reads
        assert node_reads <= node_keys, node_reads - node_keys
        assert store_reads <= store_keys, store_reads - store_keys
        assert link_reads <= link_keys, link_reads - link_keys

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

    def test_missing_page_raises_before_writing(self, tmp_path, vault_db, monkeypatch):
        monkeypatch.setattr(build, "INDEX_HTML", tmp_path / "absent.html")
        with pytest.raises(build.ViewerPageMissing, match="Reinstall the CLI"):
            build.write_site(_persist(vault_db, tmp_path / "schist.db"), tmp_path / "site")
        assert not (tmp_path / "site").exists()

    def test_output_is_ascii(self, tmp_path, vault_db, capsys):
        build.write_site(_persist(vault_db, tmp_path / "schist.db"), tmp_path / "site")
        out = capsys.readouterr().out
        assert "skipped" in out and out.isascii()


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

    def test_schist_build_missing_page_is_one_error_line(self, tmp_path):
        """A broken install prints `Error: ...` and exits 1, not a traceback (#603)."""
        vault = _vault(tmp_path / "vault")
        code = (
            "import sys, pathlib\n"
            "import schist.viewer.build as b\n"
            "b.INDEX_HTML = pathlib.Path(sys.argv[1])\n"
            "sys.argv = ['schist', '--vault', sys.argv[2], 'build', '--out', sys.argv[3]]\n"
            "from schist.__main__ import main\n"
            "main()\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path / "absent.html"), str(vault), str(tmp_path / "site")],
            capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert r.stderr.startswith("Error: viewer page not found")
        assert "Traceback" not in r.stderr

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
