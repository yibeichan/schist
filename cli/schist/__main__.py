#!/usr/bin/env python3
"""schist CLI — agent-first knowledge graph."""

import argparse
import os
import sys

from . import commands


def main():
    parser = argparse.ArgumentParser(prog='schist', description='Agent-first knowledge graph CLI')
    parser.add_argument('--vault', default=os.environ.get('SCHIST_VAULT_PATH'), help='Path to vault root (or set SCHIST_VAULT_PATH)')
    parser.add_argument('--db', default=None, help='Path to SQLite database (default: <vault>/.schist/schist.db)')

    sub = parser.add_subparsers(dest='command')

    # add
    p_add = sub.add_parser('add', help='Create a new note')
    p_add.add_argument('--title', required=True)
    p_add.add_argument('--body', default=None)
    p_add.add_argument('--tags', default=None, help='Comma-separated tags')
    p_add.add_argument('--concepts', default=None, help='Comma-separated concept slugs')
    p_add.add_argument('--status', default='draft')
    p_add.add_argument('--dir', default='notes', dest='directory')

    # link
    p_link = sub.add_parser('link', help='Add a connection between documents')
    p_link.add_argument('--source', required=True)
    p_link.add_argument('--target', required=True)
    p_link.add_argument('--type', required=True, dest='link_type')
    p_link.add_argument('--context', default=None)

    # search
    p_search = sub.add_parser('search', help='Full-text search')
    p_search.add_argument('query', help='Search query')
    p_search.add_argument('--limit', type=int, default=20)
    p_search.add_argument('--status', default=None)
    p_search.add_argument('--tags', default=None, help='Comma-separated AND filter')

    # query
    p_query = sub.add_parser('query', help='Run a SELECT query')
    p_query.add_argument('sql', help='SQL query (SELECT only)')
    p_query.add_argument('--json', action='store_true', dest='as_json')

    # build
    p_build = sub.add_parser('build', help='Build graph and search index')
    p_build.add_argument('--out', default=None, help='Output directory (default: <vault>/.schist/data/)')

    # context
    p_context = sub.add_parser('context', help='Print vault context summary')
    p_context.add_argument('--depth', choices=['minimal', 'standard', 'full'], default='standard')

    # schema
    p_schema = sub.add_parser('schema', help='Print or validate schema')
    p_schema.add_argument('--validate', action='store_true')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    vault_path = args.vault
    if not vault_path:
        print('Error: --vault required or set SCHIST_VAULT_PATH', file=sys.stderr)
        sys.exit(1)

    db_path = args.db or os.path.join(vault_path, '.schist', 'schist.db')

    dispatch = {
        'add': commands.add,
        'link': commands.link,
        'search': commands.search,
        'query': commands.query,
        'build': commands.build,
        'context': commands.context,
        'schema': commands.schema,
    }
    dispatch[args.command](args, vault_path, db_path)


if __name__ == '__main__':
    main()
