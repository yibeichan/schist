# Hub & Spoke Setup

Walk-through for wiring up a schist hub and one or more spokes so that agents
on different machines (laptop, HPC cluster, Raspberry Pi, etc.) share a single
knowledge graph.

## Topology

```
               ┌─────────────┐
               │  Hub (bare) │
               │  vault.git  │
               │  pre-receive│ ← enforces vault.yaml ACL
               └──────┬──────┘
                      │ ssh
         ┌────────────┼────────────┐
         │            │            │
    ┌────▼────┐  ┌────▼────┐  ┌────▼────┐
    │ laptop  │  │   HPC   │  │   Pi    │
    │ spoke   │  │  spoke  │  │  spoke  │
    │ (MCP)   │  │ (MCP)   │  │ (MCP)   │
    └─────────┘  └─────────┘  └─────────┘
```

- **Hub** — a bare git repo on any machine reachable by SSH. Holds the full
  vault and enforces ACLs via a `pre-receive` hook.
- **Spoke** — a sparse-checkout clone on each working machine. Has a declared
  scope (directory prefix) and identity, and is the only vault the local MCP
  server talks to.

Each spoke's MCP server auto-pushes after every `create_note` /
`add_connection` and auto-pulls before `get_context`, so agents see each
other's work without manual sync (see §4 for detail).

## Prerequisites

On **every** machine (hub and spokes):

```bash
git clone https://github.com/youruser/schist.git /path/to/schist
pip install -e /path/to/schist/cli
```

The pre-receive hook on the hub imports `schist.pre_receive`, so the package
must be installed for the `python3` on the hub's PATH. On spokes, the CLI is
what the MCP server shells out to for sync.

## Step 1. Create the hub

On the machine you've chosen as the hub (e.g. a Pi, a shared server, any SSH
box — not GitHub/GitLab SaaS, which don't run `pre-receive` hooks):

```bash
schist init --hub \
  --hub-path /srv/git/vault.git \
  --name research-graph \
  --participant laptop \
  --participant hpc-cluster \
  --participant pi
```

This creates `/srv/git/vault.git` as a bare repo, installs `hooks/pre-receive`,
and seeds an initial commit containing `vault.yaml`. Each participant gets a
default scope of `research/<name>` and a write grant for that scope; all
participants get `read: ["*"]`.

The seed uses `research/<participant>` as each participant's default scope —
that's a convention, not a hard requirement. If your vault is about anything
else (legal docs, ops runbooks, a cookbook), clone the hub after init, edit
`vault.yaml` to rename the scopes (e.g. `legal/alice`, `ops/hpc`), and push
the change — just keep in mind that root-level edits need `write: ["*"]`, so
make sure at least one participant has the wildcard before you move the
other spokes to their new scopes.

Edit `vault.yaml` afterward to customise participants, scopes, or rate limits.
Clone the hub, edit, commit, push — **but note** that root-level files like
`vault.yaml` require `write: ["*"]`, which no default participant has. Give
one participant (e.g. `laptop`) the wildcard if you want to manage the vault
from a spoke, or edit on the hub host directly.

Make the hub reachable by SSH. Pseudo-example:

```bash
# On the hub
sudo useradd -m git
sudo -u git mkdir -p /home/git/.ssh
# Add each spoke's public key to /home/git/.ssh/authorized_keys
sudo chown -R git:git /srv/git/vault.git
```

The URL that spokes clone from is then `git@hub.example.com:/srv/git/vault.git`.

## Step 2. Wire each spoke

On each spoke machine:

```bash
export SCHIST_IDENTITY=laptop   # must match a participant name in vault.yaml

schist --vault ~/vault init --spoke \
  --hub git@hub.example.com:/srv/git/vault.git \
  --scope research/laptop \
  --identity laptop
```

Repeat on the HPC cluster with `--identity hpc-cluster --scope research/hpc-cluster`,
on the Pi with `--identity pi --scope research/pi`, and so on.

**`SCHIST_IDENTITY` must be set in the shell profile** (`.bashrc`, `.zshrc`,
or the SLURM job wrapper on HPC) so every process the MCP server spawns — and
every interactive `schist sync push` — carries the right identity when it
talks to the hub. Without it, the pre-receive hook rejects every push.

## Step 3. Point the MCP server at the spoke

In the spoke's `~/.claude/settings.json` (or equivalent for your agent):

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/home/you/vault",
        "SCHIST_IDENTITY": "laptop"
      }
    }
  }
}
```

See [`mcp-setup.md`](./mcp-setup.md) for the full MCP wiring details.

## Step 4. How auto-sync works

Once wired up, the MCP server detects the spoke (by looking for
`.schist/spoke.yaml` in the vault root) and turns on two behaviours:

- **On write** (`create_note`, `add_connection`): after the local commit and
  ingestion, the server fires `python3 -m schist --vault <root> sync push` in
  the background. The call is detached and never blocks the agent; errors are
  logged to the MCP server's stderr.
- **On read** (`get_context` only): before querying SQLite, the server awaits
  `python3 -m schist --vault <root> sync pull` with a **5-second hard
  timeout**. If the hub is unreachable or slow, the pull is killed and the
  read falls through to whatever is in local SQLite — a flaky hub never
  stalls an agent. The killed pull self-heals on the next invocation (see
  §Troubleshooting).

`get_context` is the designated **session-refresh point**. Other read tools
(`search_notes`, `list_concepts`, `query_graph`) query the local SQLite
directly without pulling. Agents that want fresh cross-spoke data should call
`get_context` at session start — the minimal depth is cheap and costs one
bounded pull.

If you need more control (e.g. a batch HPC job that writes many notes and
should push at the end), the explicit `schist sync push` CLI still works.

## Step 5. Connecting notes across scopes

Because every participant has `read: ["*"]`, a spoke can always *reference*
notes from another scope in its own notes — but it can only *write* inside
its declared scope. A typical cross-machine flow:

1. Agent on the HPC spoke runs a training job and calls `create_note` with
   path `research/hpc-cluster/2026-04-12-training-run.md`. The spoke pushes.
2. Agent on the laptop spoke later calls `get_context`. The spoke pulls and
   sees the HPC note.
3. The laptop agent writes its own analysis at
   `research/laptop/2026-04-12-analysis.md` and uses `add_connection` to add
   an `extends` edge pointing to the HPC note. The laptop pushes.
4. On the next pull, the HPC spoke sees the connection.

Neither side can modify the other's files — but both see the full graph.

## Troubleshooting

### "REJECTED: push contains out-of-scope writes"

The pre-receive hook rejected the push because a file falls outside your
identity's write scope. Check:

- The exact violation is in the stderr output.
- `SCHIST_IDENTITY` matches a participant in `vault.yaml`.
- The file's parent directory is covered by your `write:` list in
  `vault.yaml`. Parent scopes grant child access (`write: [research]` covers
  `research/laptop/note.md`).

### "REJECTED: cannot determine push identity"

`SCHIST_IDENTITY` (or `GL_USER` for gitolite) is not set in the environment
of whatever process ran `git push`. On HPC this usually means the SLURM job
wrapper didn't inherit your `.bashrc` exports — add an explicit
`export SCHIST_IDENTITY=hpc-cluster` to the job script.

### "Pull timed out — falling through"

The 5-second cap on `maybeSpokePull` is intentional. If you see this
repeatedly, run `schist sync pull` in a shell to get the real error (SSH
auth failure, hub down, DNS). The MCP server will keep serving the stale
local view until the hub comes back.

### "pre-receive: ModuleNotFoundError: schist.pre_receive"

The schist package isn't installed for the `python3` the hook uses. On the
hub, `pip install -e /path/to/schist/cli` and re-test with a dry push.

### "Can I use GitHub / Gitea / GitLab SaaS as the hub?"

Not directly — hosted git providers do not run `pre-receive` hooks, so the
ACL is bypassed. Options: (a) self-host a small hub (a Pi works), (b) run the
hook in CI on every push (clunky — it's post-push by then), or (c) skip ACLs
and rely on branch protection + review. Schist is designed around (a).
