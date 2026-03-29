#!/usr/bin/env python3
"""
schist viewer build — produces graph.json + search-index.json from a vault SQLite db.

Usage:
    python3 build.py --db /path/to/vault/.schist/schist.db --out /path/to/output/dir

Output files (generic, no vault-specific content):
    graph.json        — nodes (docs + concepts) and edges for D3 force graph
    search-index.json — lunr.js document list + metadata store (index built client-side)
    index.html        — viewer UI (copied from src/index.html)
"""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path


def normalize_endpoint(endpoint: str) -> str:
    """Normalise an edge source/target to the canonical node ID.

    Edges may store paths like ``concepts/<slug>.md`` but concept nodes use
    the bare slug as their ``id``. Strip the directory prefix and ``.md``
    suffix so the endpoint can be matched against ``doc_ids``.
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
        print(f"  NOTE: {skipped} edge(s) skipped — endpoint not found in graph nodes")

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


def main():
    parser = argparse.ArgumentParser(
        description="Build schist static viewer assets from vault SQLite db"
    )
    parser.add_argument("--db", required=True, help="Path to schist.db")
    parser.add_argument("--out", required=True, help="Output directory for built assets")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    out_dir = Path(args.out).resolve()

    if not db_path.exists():
        raise SystemExit(f"ERROR: database not found: {db_path}")

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

    # Copy viewer UI
    src_html = Path(__file__).parent / "src" / "index.html"
    if src_html.exists():
        shutil.copy(src_html, out_dir / "index.html")
        print(f"Copied index.html → {out_dir / 'index.html'}")
    else:
        print(f"WARNING: {src_html} not found — skipping index.html copy")

    node_count = len(graph["nodes"])
    link_count = len(graph["links"])
    doc_count = len(search["documents"])

    print(f"graph.json     → {graph_out}  ({node_count} nodes, {link_count} links)")
    print(f"search-index.json → {search_out}  ({doc_count} docs)")


if __name__ == "__main__":
    main()
