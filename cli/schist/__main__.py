#!/usr/bin/env python3
"""schist CLI — agent-first knowledge graph."""

import argparse
import os
import sys

from . import commands, doctor as doctor_mod, sync


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
    p_add.add_argument('--file-ref', default=None, dest='file_ref', help='External file path/reference to index in frontmatter')
    # Default resolves in commands.add (#407): 'draft' when the vault's
    # vocabulary includes it, else the first configured status — mirroring
    # MCP create_note's resolved-default so a bare `schist add` can't write
    # an out-of-vocabulary status on a vault with custom statuses.
    p_add.add_argument('--status', default=None,
                       help="Note status (default: draft, or the vault's first configured status)")
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

    # doctor
    p_doctor = sub.add_parser('doctor', help='Health check for schist setup')
    p_doctor.add_argument('--json', action='store_true', dest='as_json',
                          help='Output results as JSON')
    p_doctor.add_argument('--hub-path', dest='hub_path', default=None,
                          help='(hub) Path to a bare hub repo to run hub-mode checks against')

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
    p_init.add_argument('--scope-prefix', default=sync._SCOPE_PREFIX_LEGACY_DEFAULT,
                        help='(deprecated, ignored) Retained for backward-compat; flat-convention hubs do not use a per-participant scope prefix')
    p_init.add_argument('--print-mcp-config', action='store_true', dest='print_mcp_config',
                        help='Print MCP server config and exit (no vault creation)')
    p_init.add_argument('--format', choices=['claude', 'cursor'], default='claude',
                        dest='mcp_format',
                        help='MCP config format (default: claude)')
    p_init.add_argument('--mcp-server-path', default=None, dest='mcp_server_path',
                        help='Path to mcp-server/dist/index.js (auto-detected if omitted)')

    # sync
    p_sync = sub.add_parser('sync', help='Sync spoke vault with hub')
    p_sync.add_argument(
        '--force', action='store_true',
        help='Clear stale git rebase/merge/index-lock state before syncing',
    )
    p_sync.add_argument(
        '--pull', action='store_true',
        help='With --force and no subcommand, pull before pushing',
    )
    sync_sub = p_sync.add_subparsers(dest='sync_action')
    p_sync_pull = sync_sub.add_parser('pull', help='Pull updates from hub')
    p_sync_pull.add_argument(
        '--force', action='store_true', default=argparse.SUPPRESS,
        help='Clear stale git rebase/merge/index-lock state before pulling',
    )
    p_sync_push = sync_sub.add_parser('push', help='Push local changes to hub')
    p_sync_push.add_argument(
        '--force', action='store_true', default=argparse.SUPPRESS,
        help='Clear stale git rebase/merge/index-lock state before pushing',
    )

    # hooks
    p_hooks = sub.add_parser('hooks', help='Manage installed git hooks')
    hooks_sub = p_hooks.add_subparsers(dest='hooks_action')
    p_hooks_reinstall = hooks_sub.add_parser(
        'reinstall',
        help='Refresh pre-commit and post-commit hooks from canonical templates',
    )
    p_hooks_reinstall.add_argument(
        '--force', action='store_true',
        help='Also overwrite hooks marked `# schist-hook-version: pinned` (opt-out marker)',
    )

    # hub: filesystem-side ACL administration of a bare hub repo
    p_hub = sub.add_parser('hub', help='Administer a bare hub vault.yaml (filesystem)')
    hub_sub = p_hub.add_subparsers(dest='hub_action')

    p_hub_grant = hub_sub.add_parser('grant', help='Grant a participant write on a directory')
    p_hub_grant.add_argument('participant', help='Participant name')
    p_hub_grant.add_argument('--write', required=True, help='Directory scope to grant')
    p_hub_grant.add_argument('--hub-path', dest='hub_path', required=True,
                             help='Path to the bare hub repo')

    p_hub_revoke = hub_sub.add_parser('revoke', help='Revoke a participant write on a directory')
    p_hub_revoke.add_argument('participant', help='Participant name')
    p_hub_revoke.add_argument('--write', required=True, help='Directory scope to revoke')
    p_hub_revoke.add_argument('--hub-path', dest='hub_path', required=True,
                              help='Path to the bare hub repo')

    p_hub_part = hub_sub.add_parser('participant', help='Manage participants')
    part_sub = p_hub_part.add_subparsers(dest='participant_action')

    p_part_add = part_sub.add_parser('add', help='Add a participant')
    p_part_add.add_argument('name', help='New participant name')
    p_part_add.add_argument('--type', default='spoke', help='Participant type (default: spoke)')
    p_part_add.add_argument('--write', nargs='*', default=None, help='Write scopes to grant')
    p_part_add.add_argument('--read', nargs='*', default=None, help="Read scopes (default: ['*'])")
    p_part_add.add_argument('--hub-path', dest='hub_path', required=True,
                            help='Path to the bare hub repo')

    p_part_rename = part_sub.add_parser('rename', help='Rename a participant')
    p_part_rename.add_argument('old', help='Current participant name')
    p_part_rename.add_argument('new', help='New participant name')
    p_part_rename.add_argument('--hub-path', dest='hub_path', required=True,
                               help='Path to the bare hub repo')

    p_part_remove = part_sub.add_parser('remove', help='Remove a participant')
    p_part_remove.add_argument('name', help='Participant name')
    p_part_remove.add_argument('--yes', action='store_true', help='Confirm removal')
    p_part_remove.add_argument('--hub-path', dest='hub_path', required=True,
                               help='Path to the bare hub repo')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # init: all modes (hub, spoke, standalone) routed through one helper so
    # the conflict matrix lives in one place.
    if args.command == 'init':
        sync._dispatch_init(args)
        sys.exit(0)

    # doctor: tolerates missing vault, runs before vault-required check
    if args.command == 'doctor':
        doctor_mod.doctor(args)
        sys.exit(0)

    # hub: filesystem ACL admin — needs --hub-path, not --vault
    if args.command == 'hub':
        from schist import hub_admin
        try:
            if args.hub_action == 'grant':
                hub_admin.cmd_grant(args)
            elif args.hub_action == 'revoke':
                hub_admin.cmd_revoke(args)
            elif args.hub_action == 'participant':
                if args.participant_action == 'add':
                    hub_admin.cmd_participant_add(args)
                elif args.participant_action == 'rename':
                    hub_admin.cmd_participant_rename(args)
                elif args.participant_action == 'remove':
                    hub_admin.cmd_participant_remove(args)
                else:
                    print('Usage: schist hub participant {add|rename|remove}', file=sys.stderr)
                    sys.exit(1)
            else:
                print('Usage: schist hub {grant|revoke|participant}', file=sys.stderr)
                sys.exit(1)
        except hub_admin.HubAdminError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
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
        elif args.force:
            if args.pull:
                sync.sync_pull(args, vault_path, db_path)
            sync.sync_push(args, vault_path, db_path)
        else:
            print('Usage: schist sync [--force [--pull]] {pull|push}', file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # hooks sub-dispatch
    if args.command == 'hooks':
        if args.hooks_action == 'reinstall':
            sync.hooks_reinstall(args, vault_path, db_path)
        else:
            print('Usage: schist hooks reinstall', file=sys.stderr)
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
