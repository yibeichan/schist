# vault.yaml Specification

Located at the root of a schist vault. Optional — schist works without it.

## Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Human-readable vault name |
| domains | [string] | no | Valid domain taxonomy values |
| participants | [string \| object] | no | Agent identity names. Can be simple strings or objects with `name` and `default_scope` |
| scope_convention | string | no | How scope maps to filesystem: `subdirectory` (default), `flat`, `multi-vault` |

## Participant Format

Participants can be simple strings (backward compatible):
```yaml
participants: [agent-a, agent-b]
```

Or objects with per-agent config:
```yaml
participants:
  - name: agent-a
    default_scope: project:rollup
  - name: agent-b
    default_scope: global
```

`default_scope` controls what scope the agent resolves when using `scope: "inherit"` in search queries. Defaults to `"global"` if omitted.

## Example

```yaml
name: my-team
domains: [ai, security, ops]
participants:
  - name: agent-a
    default_scope: global
  - name: agent-b
    default_scope: project:myapp
scope_convention: subdirectory
```

## Notes

- `participants` uses agent names only. Human contributions use `source: human` on individual notes.
- `domains` defines the valid set for the `domain` field on notes. Tools may validate against this list.
- `scope_convention` tells tools how to derive scope from directory structure.
