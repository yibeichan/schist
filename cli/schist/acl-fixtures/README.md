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
