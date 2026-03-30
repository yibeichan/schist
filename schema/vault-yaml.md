# vault.yaml Specification

Located at the root of a schist vault. Optional — schist works without it.

## Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Human-readable vault name |
| domains | [string] | no | Valid domain taxonomy values |
| participants | [string] | no | Agent identity names (no human names) |
| scope_convention | string | no | How scope maps to filesystem: `subdirectory` (default), `flat`, `multi-vault` |

## Example

```yaml
name: my-team
domains: [ai, security, ops]
participants: [agent-a, agent-b]
scope_convention: subdirectory
```

## Notes

- `participants` uses agent names only. Human contributions use `source: human` on individual notes.
- `domains` defines the valid set for the `domain` field on notes. Tools may validate against this list.
- `scope_convention` tells tools how to derive scope from directory structure.
