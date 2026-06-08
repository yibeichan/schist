# Security Model

schist is designed for first-party agents operating on a vault that belongs to
one human operator or one trusted team. The MCP server is not a sandbox and is
not a multi-tenant authorization layer.

## Trust Boundary

Any process that can call the schist MCP server is treated as trusted with read
access to the configured vault and the configured agent-memory database.

Read tools do not perform access-control checks:

| Tool | Exposure |
|------|----------|
| `get_context` | Vault metadata and summary context |
| `sync_status` | Local spoke/hub sync state, working-tree cleanliness, and background sync errors |
| `search_notes` | Full-text and metadata search across indexed vault notes |
| `get_note` | Full content for any note path inside the vault |
| `list_concepts` | Concept metadata and graph counts |
| `query_graph` | SELECT/WITH SQL over the vault SQLite database, including indexed document content |
| `search_memory` | Cross-project agent memory entries, optionally filtered by owner/type/date |
| `get_agent_state` | Agent state values by key |

`query_graph` rejects non-SELECT SQL, but a trusted caller can still use
SELECT queries to read any indexed vault content. Do not expose the MCP server
to untrusted callers.

## Write Authorization

MCP write tools require a caller identity argument (`owner`, or `created_by` for
`add_concept_alias`) and validate it against the local agent identity
configuration:

- `SCHIST_ALLOWED_AGENTS` set: the caller identity must appear in the
  comma-separated allowlist. This supports several mutually trusted agents
  sharing one MCP server process.
- `SCHIST_ALLOWED_AGENTS` unset and `SCHIST_AGENT_ID` set: the caller identity
  must match `SCHIST_AGENT_ID`.
- Neither configured: writes fail with `CONFIG_ERROR`.

Markdown note writes (`create_note` and `add_connection`) are also checked
against `vault.yaml` scope grants when that file is present and locally
parseable. The hub pre-receive hook is the authoritative enforcement point for
git pushes; the MCP-side ACL check is an early local guard for the note-write
path.

The ACL model is write-side only today. `vault.yaml` read grants document scope
intent for hub policy, but local MCP read tools do not enforce per-caller read
ACLs.

## Deployment Constraints

Supported deployment shapes:

- Single user, single local agent, stdio MCP transport.
- Single user or trusted team, multiple trusted agents sharing one MCP server
  through `SCHIST_ALLOWED_AGENTS`.
- Hub-and-spoke git synchronization where the hub pre-receive hook enforces
  `vault.yaml` write grants on pushes.

Unsupported deployment shapes:

- Multiple untrusted human users sharing one MCP server process.
- Untrusted agents sharing one MCP server process.
- A network-exposed MCP server without an external authentication and
  authorization proxy.
- Treating `vault.yaml` read grants as a privacy boundary for MCP reads.

If you need multi-tenant read isolation, add a trusted proxy or implement
read-side authorization before deploying schist in that shape.

## Data Sensitivity

Vault notes may contain sensitive operational data, personal observations,
incident details, financial information, credentials accidentally pasted by an
agent, or other private material. Treat the entire vault, its SQLite index, and
its git history as sensitive.

Important persistence properties:

- Git history retains historical note content. Removing a secret from the
  current file does not remove it from prior commits.
- schist's append-only workflow prefers superseding notes over rewriting old
  conclusions.
- The SQLite vault database under `.schist/` is derived from markdown and can be
  rebuilt, but it may contain indexed copies of note content while present.
- The agent-memory database is user-home-level by default and is shared across
  projects that point at the same memory DB.

## Multi-Agent Trust Model

`SCHIST_ALLOWED_AGENTS` is an equal-trust allowlist, not a role hierarchy.
Every listed agent can submit write calls under its own caller identity, and
read tools remain available to any caller that can reach the server.

For vault notes, `source_agent` frontmatter records the agent that originally
created the note. Later mutations such as adding connections preserve that
frontmatter and attribute the mutator in the git commit subject. For memory
entries, `agent_memory.owner` is the attribution field.

## Related Documentation

- MCP server setup and tool exposure: `docs/mcp-setup.md`
- Cross-project memory and identity: `docs/cross-project-memory.md`
- Vault ACL schema: `schema/vault-yaml.md`
- Markdown/frontmatter schema: `schema/SCHEMA.md`
