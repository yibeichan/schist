# Hub & Spoke Setup

Walk-through for wiring up a schist hub and one or more spokes so that agents
on different machines (laptop, HPC cluster, Raspberry Pi, etc.) share a single
knowledge graph.

> **Setting up Pi + ORCD + Dragonfly?** See the [Pi/ORCD/Dragonfly topology guide](hub-spoke-pi-orcd-dragonfly.md)
> for an opinionated, copy-paste-ready walkthrough for that specific setup.

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
uv pip install --system -e /path/to/schist/cli   # or: pip install -e /path/to/schist/cli
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
sudo chown -R git:git /srv/git/vault.git
```

The URL that spokes clone from is then `git@hub.example.com:/srv/git/vault.git`.

Then **pin each spoke's public key to its identity** instead of pasting keys
into `authorized_keys` by hand:

```bash
# As the hub user (the one spokes SSH in as)
schist hub key add laptop      --key-file laptop.pub      --hub-path /srv/git/vault.git
schist hub key add hpc-cluster --key-file hpc-cluster.pub --hub-path /srv/git/vault.git
schist hub key add pi          --key-file pi.pub          --hub-path /srv/git/vault.git
```

See "Pinning identities to SSH keys" below for why this matters and how to
turn on enforcement.

## Step 2. Wire each spoke

On each spoke machine:

```bash
export SCHIST_IDENTITY=laptop   # must match a participant name in vault.yaml

schist --vault ~/vault init --spoke \
  --hub git@hub.example.com:/srv/git/vault.git \
  --scope research \
  --identity laptop
```

Repeat on each spoke with `--identity <spoke-name> --scope research` (or whichever
content-axis dir the spoke should sparse-checkout). Authorship is recorded in the
auto-filled `source_agent` frontmatter, not via directory placement.

**`SCHIST_IDENTITY` must be set in the shell profile** (`.bashrc`, `.zshrc`,
or the SLURM job wrapper on HPC) so every process the MCP server spawns — and
every interactive `schist sync push` — carries the right identity when it
talks to the hub. Without it, the pre-receive hook rejects every push.

## Step 3. Point the MCP server at the spoke

Generate and register the MCP config for this spoke. `--print-mcp-config`
emits a runnable `claude mcp add` line that registers schist as a
user-scope server (stored in `~/.claude.json`):

```bash
schist --vault ~/vault init --print-mcp-config --format claude --identity laptop
# then run the printed `claude mcp add ...` line
```

The command bakes in `SCHIST_VAULT_PATH`, `SCHIST_AGENT_ID`, and
`SCHIST_IDENTITY` from the flags above. On older Claude Code CLIs that
predate `mcp add`, the same output also includes a commented JSON
fallback — uncomment that block and hand-merge it under the top-level
`mcpServers` key in `~/.claude.json`. The fallback shape is:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/home/you/vault",
        "SCHIST_AGENT_ID": "laptop",
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

- **On write** (`create_note`, `add_connection`): before mutating, the server
  checks for `.schist/last-sync-error`. If a prior background push failed, the
  write returns `SYNC_DIRTY` so the agent does not compound spoke/hub
  divergence. After a clean local commit and ingestion, the server fires
  `schist --vault <root> sync push` in the background.
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

If `SYNC_DIRTY` appears, call `sync_status` to inspect divergence and
`sync_retry` to retry the push. A successful retry or background push clears
the dirty sentinel; `get_context` only surfaces the warning and does not clear
it. The block applies only to spoke vaults — a standalone vault has no hub to
diverge from and is never blocked. If recovery keeps failing, delete
`.schist/last-sync-error` manually only as a last resort.

If you need more control (e.g. a batch HPC job that writes many notes and
should push at the end), the explicit `schist sync push` CLI still works.

## Step 5. Connecting notes across scopes

Because every participant has `read: ["*"]`, a spoke can always *reference*
notes from another scope in its own notes — but it can only *write* inside
its declared scope. A typical cross-machine flow:

1. Agent on the HPC spoke runs a training job and calls `create_note` with
   path `research/2026-04-12-training-run.md` (flat scope; `source_agent: orcd`
   is set automatically). The spoke pushes.
2. Agent on the laptop spoke later calls `get_context`. The spoke pulls and
   sees the HPC note.
3. The laptop agent writes its own analysis at
   `research/2026-04-12-analysis.md` and uses `add_connection` to add
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
hub, `uv pip install --system -e /path/to/schist/cli` (or `pip install -e /path/to/schist/cli`) and re-test with a dry push.

### "Can I use GitHub / Gitea / GitLab SaaS as the hub?"

Not directly — hosted git providers do not run `pre-receive` hooks, so the
ACL is bypassed. Options: (a) self-host a small hub (a Pi works), (b) run the
hook in CI on every push (clunky — it's post-push by then), or (c) skip ACLs
and rely on branch protection + review. Schist is designed around (a).

## Pinning identities to SSH keys

By default the pre-receive hook resolves the pushing identity from
`SCHIST_IDENTITY` — a variable **the client ships** (`SendEnv` on the spoke,
`AcceptEnv` on the hub). That value is an assertion, not a credential: any
spoke that can reach the hub can push as any participant, including one with
`write: ["*"]`. Key pinning closes that hole by making the SSH key itself
carry the identity (#502).

`schist hub key add` writes a forced-command entry into the hub user's
`~/.ssh/authorized_keys`:

```
restrict,command="schist-shell laptop /srv/git/vault.git" ssh-ed25519 AAAA... laptop@dev
```

For every connection made with that key, sshd runs the pinned command
regardless of what the client asked for. The wrapper sets
`SCHIST_IDENTITY=laptop` itself (overwriting anything the client sent), marks
the push as pinned (`SCHIST_IDENTITY_PINNED=1`), strips linker-injection env
(`LD_*`/`DYLD_*`/`BASH_ENV`), and confines the key to `git-receive-pack` /
`git-upload-pack` on **that one repository** via `git shell` — a key pinned
for vault-A cannot touch another vault the same account hosts (pass
`--any-repo` to `key add` if you really run one key across several vaults).
No `sshd_config` changes are needed — but `schist-shell` must be on the PATH
sshd uses, which a normal `pip install -e <schist>/cli` on the hub provides.

Rollout:

1. Pin every pushing spoke's key: `schist hub key add <participant>
   --key-file <pub> --hub-path <hub>`. Verify with `schist hub key list
   --hub-path <hub>`.
2. Turn on enforcement so the hook stops trusting client-sent identity for
   SSH pushes (root-level vault.yaml write, so it runs on the hub host like
   the other `schist hub` commands):

   ```bash
   schist hub security require-pinned-identity on --hub-path /srv/git/vault.git
   ```

   This commits `security: {require_pinned_identity: true}` into the hub's
   `vault.yaml`. Pushes over SSH without the pinned marker are then rejected. Local pushes
   on the hub host itself stay exempt: filesystem access to the bare repo is
   already admin authority (same trust model as `schist hub`).
3. Remove `AcceptEnv SCHIST_IDENTITY` from the hub's `sshd_config` — pinned
   keys don't need it, and an `AcceptEnv` that matches `SCHIST_*` would let a
   client fake the pinned marker on any un-pinned key. While you're there,
   don't forward `PATH`, `LD_*`, `PYTHON*`, `BASH_ENV`, or `GIT_*` either
   (`GIT_PROTOCOL` alone is fine) — those can redirect what the forced
   command executes.
4. Check the result: `schist doctor --hub-path <hub>` FAILs on un-pinned
   keys while enforcement is off (each one can push as any identity), and
   WARNs on unkeyed participants, stale pins, risky `AcceptEnv` entries, and
   enforcement being left off. Once enforcement is on, remaining un-pinned
   keys only WARN — an un-pinned shell-capable key is the normal shape of
   your own admin login key, and its pushes are rejected anyway.

Note the hub is assumed to be reachable over **SSH only** (that's also what
makes `pre-receive` run). Don't additionally expose a schist hub over
smart-HTTP or `git://` — those transports bypass the pinning gate.

To rotate a key, run `schist hub key add` again with the new public key (the
old line for the same key blob is replaced; a new blob adds a second pinned
key — remove old ones with `schist hub key remove <participant>` and re-add
the current key). Renaming a participant (`schist hub participant rename`)
does **not** rewrite `authorized_keys` — re-pin the key after a rename;
`doctor` flags the stale pin.

## Administering ACLs

ACL changes are made **on the hub host**, against the bare repo, with the
`schist hub` commands. Admin authority is filesystem access to the bare repo —
the same trust level required to create the hub with `schist init --hub`. These
commands commit `vault.yaml` directly via git plumbing, so they never go through
the `pre-receive` hook.

```bash
# Grant / revoke a participant's write scope on a directory
schist hub grant   <participant> --write <dir> --hub-path /srv/vault.git
schist hub revoke  <participant> --write <dir> --hub-path /srv/vault.git

# Manage participants
schist hub participant add    <name> --write <dir> [--write <dir> ...] [--type spoke] --hub-path /srv/vault.git
schist hub participant rename <old> <new>                                --hub-path /srv/vault.git
schist hub participant remove <name> --yes                               --hub-path /srv/vault.git
```

Every participant must be granted at least one write scope; read-only participants are not supported by the schema.

**`'*'` write grants are refused.** On the hub, `'*'` write is also the gate for
editing `vault.yaml` itself, so granting it to a participant would let that
spoke rewrite the ACL over SSH. Administer ACLs from the hub host and grant
concrete directories instead.

**Renaming a participant is a two-part operation.** `schist hub participant
rename` rekeys the hub-side `vault.yaml` only. The renamed spoke must also
update `identity:` in its local `.schist/spoke.yaml`, or its pushes will be
rejected. Notes already written under the old name keep their `source_agent:`
value (history is append-only).

**Spotting drift.** Run `schist doctor --hub-path /srv/vault.git` on the hub to
flag directories in the schema that no participant can write, or directories
some participants can write but others cannot. Spokes get the matching
spoke-side warning automatically from `schist doctor` (it checks the local
schema against the spoke's hub write grant).
