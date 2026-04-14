#!/usr/bin/env python3
"""schist CLI — agent-first knowledge graph."""

import argparse
import os
import sys

from . import commands, sync


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

    # init
    p_init = sub.add_parser('init', help='Initialize a vault (standalone, hub, or spoke)')
    p_init.add_argument('path', nargs='?', default=None,
                        help='(standalone) Path to new vault (default: current dir)')
    p_init.add_argument('--spoke', action='store_true', help='Initialize as spoke vault')
    p_init.add_argument('--hub', help='(spoke) Hub repository URL')
    p_init.add_argument('--scope', help='(spoke) Scope to sync (directory path)')
    p_init.add_argument('--identity', default=os.environ.get('SCHIST_IDENTITY'),
                        help='(spoke/standalone) Identity name (or set SCHIST_IDENTITY)')
    p_init.add_argument('--hub-path', dest='hub_path',
                        help='(hub) Path to the bare repo to create')
    p_init.add_argument('--name', help='(hub/standalone) Vault name for vault.yaml')
    p_init.add_argument('--participant', action='append',
                        help='(hub) Participant name (repeatable)')

    # sync
    p_sync = sub.add_parser('sync', help='Sync spoke vault with hub')
    sync_sub = p_sync.add_subparsers(dest='sync_action')
    sync_sub.add_parser('pull', help='Pull updates from hub')
    sync_sub.add_parser('push', help='Push local changes to hub')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # init: all modes (hub, spoke, standalone) routed through one helper so
    # the conflict matrix lives in one place.
    if args.command == 'init':
        sync._dispatch_init(args)
        sys.exit(0)

    vault_path = args.vault
    if not vault_path:
        print('Error: --vault required or set SCHIST_VAULT_PATH', file=sys.stderr)
        sys.exit(1)

    db_path = args.db or os.path.join(vault_path, '.schist', 'schist.db')

    # sync sub-dispatch
    if args.command == 'sync':
        if args.sync_action == 'pull':
            sync.sync_pull(args, vault_path, db_path)
        elif args.sync_action == 'push':
            sync.sync_push(args, vault_path, db_path)
        else:
            print('Usage: schist sync {pull|push}', file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

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
