# vault.yaml Specification

Located at the root of a schist vault. Optional — schist works without it.

## Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| vault_version | integer | yes (v1+) | Schema version. Currently `1`. Omitting triggers a v0 deprecation warning. |
| name | string | yes | Human-readable vault name |
| participants | [string \| object] | yes | Agent identity names. Can be simple strings or objects (see below) |
| scope_convention | string | yes (v1) | How scope maps to filesystem: `flat` (default, recommended), `subdirectory`, `multi-vault` |
| access | {identity: {read, write}} | yes | Per-participant read/write scope grants |
| rate_limits | {identity: limits} | no | Per-participant rate limiting overrides |

## Participant Format

Participants can be simple strings (backward compatible):
```yaml
participants: [agent-a, agent-b]
```

Or objects with per-agent config:
```yaml
participants:
  - name: agent-a
    type: agent
    default_scope: project:rollup
    transport: ssh-and-git
    metadata:
      repo: https://github.com/example/repo
  - name: agent-b
    default_scope: global
```

### Participant Object Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Must match `^[a-z][a-z0-9-]*$` |
| type | string | no | `agent` (default) or `spoke` |
| default_scope | string | no | Scope resolved for `scope: "inherit"` queries. Defaults to `"global"`. Under `scope_convention: flat`, leave at `"global"` — authorship is recorded via the auto-filled `source_agent` frontmatter field, not via directory placement. |
| transport | string | no | `ssh-and-git` (default) or `git-only` |
| metadata | {string: string} | no | Arbitrary key-value pairs (both keys and values must be strings) |

## Access Format

Every participant must have an access entry. Scopes support `"*"` for all, exact match, and parent→child inheritance (e.g. `research` covers `research/mario`).

```yaml
access:
  agent-a:
    read: ["*"]
    write: [research]
  agent-b:
    read: [research, ops]
    write: [ops]
```

## Rate Limits Format

Optional per-participant rate limiting. Keys must match a participant name.

```yaml
rate_limits:
  agent-a:
    git_syncs_per_hour: 10
    mcp_writes_per_hour: 100
    notes_per_sync: 20
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| git_syncs_per_hour | integer | 10 | Max git sync operations per hour |
| mcp_writes_per_hour | integer | 100 | Max MCP write calls per hour |
| notes_per_sync | integer | 20 | Max notes per sync batch |

## Example

```yaml
vault_version: 1
name: my-team
scope_convention: flat
participants:
  - name: agent-a
    type: agent
    default_scope: global
    transport: ssh-and-git
    metadata:
      repo: https://github.com/example/repo
  - name: agent-b
    type: spoke
    default_scope: global
access:
  agent-a:
    read: ["*"]
    write: [research, ops]
  agent-b:
    read: [ops]
    write: [ops]
rate_limits:
  agent-a:
    git_syncs_per_hour: 5
```

## Notes

- `participants` uses agent names only. Human contributions use `source: human` on individual notes.
- `scope_convention` tells tools how to derive scope from directory structure. `flat` is the default; new schist deployments should prefer it. `subdirectory` and `multi-vault` are fully supported for existing deployments.
- `vault_version` is required for v1 vaults. Omitting it triggers a deprecation warning and defaults to v0 behavior.
