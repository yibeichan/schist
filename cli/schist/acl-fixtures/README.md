# ACL parity fixtures

Each case is a pair of files: `<name>.yaml` is a `vault.yaml` snippet, and
`<name>.cases.json` is a list of `{ identity, scope, canWrite }` tuples.

Both `cli/tests/test_acl_parity.py` (Python) and
`mcp-server/tests/vault-acl.test.ts` (TypeScript) load every pair and assert
their respective `can_write` / `canWrite` implementations return the
expected boolean for every case.

To add a new ACL rule (e.g. a new wildcard syntax or a new scope shape),
add a new fixture pair here — both implementations will pick it up
automatically.

These are SHIPPED in the schist wheel as package data so the fixtures are
available to consumers of the installed CLI. The TS side reads them via a
relative path from the schist source tree.

`identity-resolution.cases.json` is a standalone parity matrix for the
hub-vs-MCP identity precedence chain. It is not paired with a vault.yaml
fixture. Python asserts `pre_receive.resolve_identity()` returns
`hubIdentity`; TypeScript asserts `resolveAclIdentity(fallback)` returns
`mcpIdentity`. The one intentional asymmetry is the MCP-side owner fallback
when neither `SCHIST_IDENTITY` nor `GL_USER` is set.

## Case-file shapes

`<name>.cases.json` has one of two shapes:

- **Accept** — a JSON *array* of `{ identity, scope, canWrite }` tuples. Both
  parsers must accept the `<name>.yaml` (non-null / no raise) and agree on
  `can_write` / `canWrite` for every tuple.
- **Reject** — a JSON *object* `{ "reject": true, "reason": "<why>" }`. The
  strict Python parser (`parse_vault_yaml`) must raise `ACLError`, and the TS
  reader (`loadVaultAcl`) must return `null` (fail-open). Used to pin the
  participant-identity invariants both parsers enforce (#160).
