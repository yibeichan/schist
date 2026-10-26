# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""
schist viewer build — produces the static viewer site from a vault SQLite db.

``schist build`` calls ``write_site``; ``python -m schist.viewer.build`` and
the repo-root ``viewer/build.py`` shim run ``main``.

Usage:
    python -m schist.viewer.build --db /path/to/vault/.schist/schist.db --out /path/to/output/dir

Output files:
    graph.json        — nodes (docs + concepts) and links for D3 force graph
    search-index.json — lunr.js document list + metadata store (index built client-side)
    index.html        — viewer UI (shipped as package data next to this module)

The JSON files carry every non-concept note in the database, whatever its
scope: titles, tags and full bodies. Treat a built site as vault content.
Stdlib-only, so the ``viewer/build.py`` shim can run it without an install.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sqlite3
from pathlib import Path

INDEX_HTML = Path(__file__).parent / "index.html"

# Stands in index.html's Content-Security-Policy for the hashes of the page's
# inline scripts, which render_page fills in. Unfilled it is not a valid
# source expression, so a page served without a build runs no inline script.
INLINE_SCRIPT_HASHES = "'inline-script-hashes'"
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
# Every script element without a src, however its tags are spelled. When the
# hashed spans do not match these one for one, a hash would cover the wrong
# text and the browser would block a script.
_INLINE_SCRIPT_OPEN = re.compile(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>", re.I)
_CSP_META = re.compile(r'<meta http-equiv="Content-Security-Policy" content="[^"]*"')


class ViewerPageMissing(FileNotFoundError):
    """index.html is missing, or cannot be built into a page whose scripts
    run: a broken install, or an edit to the page."""


def normalize_endpoint(endpoint: str) -> str:
    """Normalise an edge source/target to the canonical node ID.

    Edges may reference concepts as ``concepts/<slug>.md`` but concept
    nodes use the bare slug as their ``id``. Note nodes, however, keep
    their full ``notes/<file>.md`` path as their ``id`` (that IS the note
    id in the docs table).

    So only the ``concepts/`` prefix case gets normalised — both the
    prefix and the trailing ``.md`` are stripped to yield the bare slug.
    Everything else (note paths, papers, external refs) is left alone
    so it can match ``doc_ids`` byte-for-byte.

    Historical bug (fixed by PR #27): this used to strip ``.md``
    unconditionally, which silently broke every note-source edge —
    ``notes/foo.md`` became ``notes/foo`` and no longer matched the note
    node's id, so the edge was dropped into the skipped count. The
    graph was rendering with concepts but no note-to-concept links.
    """
    if endpoint.startswith("concepts/"):
        endpoint = endpoint[len("concepts/"):]
        if endpoint.endswith(".md"):
            endpoint = endpoint[: -len(".md")]
    return endpoint


def build_graph(db: sqlite3.Connection) -> dict:
    nodes = []
    links = []

    # Exclude concept markdown files from the docs query: the `docs` table
    # contains every ingested .md file, including concepts/<slug>.md, which
    # would produce a duplicate node alongside the canonical concept node from
    # the `concepts` table.
    docs = db.execute(
        "SELECT id, title, date, status, tags FROM docs"
        " WHERE id NOT LIKE 'concepts/%'"
        " ORDER BY date DESC"
    ).fetchall()
    for id_, title, date, status, tags_json in docs:
        tags = json.loads(tags_json) if tags_json else []
        nodes.append(
            {
                "id": id_,
                "label": title or id_,
                "type": "note",
                "date": date or "",
                "status": status or "draft",
                "tags": tags,
            }
        )

    concepts = db.execute(
        "SELECT slug, title, description FROM concepts"
    ).fetchall()
    for slug, title, desc in concepts:
        nodes.append(
            {
                "id": slug,
                "label": title or slug,
                "type": "concept",
                "description": desc or "",
            }
        )

    # doc_ids contains both note ids and concept slugs at this point.
    doc_ids = {n["id"] for n in nodes}
    edges = db.execute(
        "SELECT source, target, type, context FROM edges"
    ).fetchall()
    skipped = 0
    for source, target, etype, context in edges:
        # Normalise endpoints: edges may store paths like concepts/<slug>.md
        # but concept nodes use bare slug as their id.
        src = normalize_endpoint(source)
        tgt = normalize_endpoint(target)
        # Skip edges whose endpoints are not present in the graph
        # (e.g. free-text target references that weren't ingested as nodes)
        if src not in doc_ids or tgt not in doc_ids:
            skipped += 1
            continue
        links.append(
            {
                "source": src,
                "target": tgt,
                "type": etype,
                "context": context or "",
            }
        )

    if skipped:
        print(f"  NOTE: {skipped} edge(s) skipped - endpoint not found in graph nodes")

    return {"nodes": nodes, "links": links}


def build_search_index(db: sqlite3.Connection) -> dict:
    """
    Produce a document store + raw documents list for lunr.js.
    Lunr builds the index client-side from the documents array — no server needed.
    """
    docs = db.execute(
        "SELECT id, title, date, status, tags, body FROM docs"
        " WHERE id NOT LIKE 'concepts/%'"
        " ORDER BY date DESC"
    ).fetchall()

    documents = []
    store = {}
    for id_, title, date, status, tags_json, body in docs:
        tags = json.loads(tags_json) if tags_json else []
        # Truncate body for index — first 500 chars is enough for snippet generation
        body_excerpt = (body or "")[:500].replace("\n", " ").strip()
        doc = {
            "id": id_,
            "title": title or id_,
            "date": date or "",
            "status": status or "draft",
            "tags": " ".join(tags),
            "body": body_excerpt,
        }
        documents.append(doc)
        store[id_] = {
            "title": title or id_,
            "date": date or "",
            "status": status or "draft",
            "tags": tags,
            # Full body stored here for the detail panel; the truncated
            # body_excerpt above is only used by the lunr index.
            "body": (body or "").strip(),
        }

    return {"documents": documents, "store": store}


def render_page(html: str) -> str:
    """Fill the page's script-src with a SHA-256 hash of each inline script.

    The policy allows only the pinned CDN files and these exact inline
    scripts, so markup that gets past DOMPurify cannot run script (#761).
    Hashing at build time keeps the policy right after any edit to the page.
    """
    csp = _CSP_META.search(html)
    if not csp or INLINE_SCRIPT_HASHES not in csp.group(0):
        raise ViewerPageMissing(
            f"viewer page {INDEX_HTML} has no {INLINE_SCRIPT_HASHES} placeholder in its "
            "Content-Security-Policy, so its scripts could not run. In a checkout, put "
            f"{INLINE_SCRIPT_HASHES} back in the policy's script-src; otherwise reinstall "
            "the CLI (uv tool install --reinstall <path-to-cli>)."
        )
    scripts = _INLINE_SCRIPT.findall(html)
    opened = len(_INLINE_SCRIPT_OPEN.findall(html))
    if len(scripts) != opened or any("<script" in m.lower() for m in scripts):
        raise ViewerPageMissing(
            f"viewer page {INDEX_HTML} has {opened} inline script element(s), but only "
            f"{len(scripts)} written exactly as <script>…</script> with no other <script "
            "inside, so their hashes would not match what the browser runs. Write each "
            "inline script's tags as plain <script> and </script>."
        )
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(m.encode("utf-8")).digest()).decode() + "'"
        for m in scripts
    )
    filled = csp.group(0).replace(INLINE_SCRIPT_HASHES, hashes)
    return html[:csp.start()] + filled + html[csp.end():]


def write_site(db_path: Path, out_dir: Path) -> dict:
    """Write graph.json, search-index.json and index.html into ``out_dir``.

    Returns the node, link and document counts. ``index.html`` is package
    data, so a missing copy means a broken install and raises rather than
    leaving a site the browser cannot open.
    """
    db_path = Path(db_path)
    out_dir = Path(out_dir)
    if not INDEX_HTML.exists():
        raise ViewerPageMissing(
            f"viewer page not found: {INDEX_HTML}. Reinstall the CLI "
            f"(uv tool install --reinstall <path-to-cli>), or in a checkout restore "
            f"it with: git checkout -- cli/schist/viewer/index.html"
        )

    page = render_page(INDEX_HTML.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(db_path)
    try:
        graph = build_graph(db)
        search = build_search_index(db)
    finally:
        db.close()

    graph_out = out_dir / "graph.json"
    search_out = out_dir / "search-index.json"
    graph_out.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
    search_out.write_text(json.dumps(search, indent=2, ensure_ascii=False), encoding="utf-8")
    # Written, not copied: the page's CSP gets its inline-script hashes here.
    # An installed page can be read-only (a Nix store file is 0444); writing
    # its text never carries that mode over to the built copy.
    (out_dir / "index.html").write_text(page, encoding="utf-8")

    counts = {
        "nodes": len(graph["nodes"]),
        "links": len(graph["links"]),
        "documents": len(search["documents"]),
    }
    # ASCII only: a non-UTF-8 stdout (a latin-1 locale, PYTHONIOENCODING)
    # would otherwise fail the command after the site is written.
    print(f"graph.json        -> {graph_out}  ({counts['nodes']} nodes, {counts['links']} links)")
    print(f"search-index.json -> {search_out}  ({counts['documents']} docs)")
    print(f"index.html        -> {out_dir / 'index.html'}")
    return counts


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Build schist static viewer assets from vault SQLite db"
    )
    parser.add_argument("--db", required=True, help="Path to schist.db")
    parser.add_argument("--out", required=True, help="Output directory for built assets")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        raise SystemExit(f"ERROR: database not found: {db_path}")

    write_site(db_path, Path(args.out).resolve())


if __name__ == "__main__":
    main()
