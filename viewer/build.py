#!/usr/bin/env python3
"""
schist viewer build — produces graph.json + search-index.json from a vault SQLite db.

Usage:
    python3 build.py --db /path/to/vault/.schist/schist.db --out /path/to/output/dir

Output files (generic, no vault-specific content):
    graph.json        — nodes (docs + concepts) and edges for D3 force graph
    search-index.json — lunr.js pre-built search index + document store
    index.html        — viewer UI (copied from src/index.html)
"""

import argparse
import json
import shutil
import sqlite3
from pathlib import Path


def build_graph(db: sqlite3.Connection) -> dict:
    nodes = []
    links = []

    docs = db.execute(
        "SELECT id, title, date, status, tags FROM docs ORDER BY date DESC"
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
    concept_slugs = set()
    for slug, title, desc in concepts:
        concept_slugs.add(slug)
        nodes.append(
            {
                "id": slug,
                "label": title or slug,
                "type": "concept",
                "description": desc or "",
            }
        )

    edges = db.execute(
        "SELECT source, target, type, context FROM edges"
    ).fetchall()
    for source, target, etype, context in edges:
        links.append(
            {
                "source": source,
                "target": target,
                "type": etype,
                "context": context or "",
            }
        )

    return {"nodes": nodes, "links": links}


def build_search_index(db: sqlite3.Connection) -> dict:
    """
    Produce a document store + raw documents list for lunr.js.
    Lunr builds the index client-side from the documents array — no server needed.
    """
    docs = db.execute(
        "SELECT id, title, date, status, tags, body FROM docs ORDER BY date DESC"
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

    graph_out.write_text(json.dumps(graph, indent=2))
    search_out.write_text(json.dumps(search, indent=2))

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
