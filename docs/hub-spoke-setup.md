# Hub & Spoke Setup

Walk-through for wiring up a schist hub and one or more spokes so that agents
on different machines (laptop, HPC cluster, Raspberry Pi, etc.) share a single
knowledge graph.

> **Setting up a Pi hub with an HPC cluster and a laptop?** See the [Pi/HPC/laptop topology guide](hub-spoke-pi-hpc-laptop.md)
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
content-axis dir the spoke should sparse-checkout). `--scope global` checks out every
content directory the hub's committed `schist.yaml` lists under `directories`, or the
packaged default list (`notes`, `papers`, `concepts`, `research`, `decisions`, `ops`,
`projects`, `logs`) when it lists none. A nested entry such as `research/ai` counts as
its top-level directory (`research`), as it does for the index; an entry that is
absolute, `..`, starts with `.`, or contains `:`, `*`, `?`, `[` or a backslash is
ignored. `schist sync push` stages the same directories, except one outside the spoke's
sparse checkout: a directory added to `schist.yaml` after the spoke was set up is not
checked out there, and each push skips it with a warning until you run
`git sparse-checkout add <dir>` in the spoke's vault. Authorship is recorded in the
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
  divergence. After a clean local commit and ingestion, the server pushes in
  the background, in two steps. If the vault has uncommitted files (a hand
  edit in an editor, say), it first runs `schist --vault <root> sync push
  --commit-only` while holding its own write lock, so those files are
  committed (as `sync(<identity>): N files`) without racing the server's next
  write. It then runs `schist --vault <root> sync push --no-commit` outside the
  lock, which only sends existing commits. A push with nothing to send still
  clears an earlier sync-error sentinel, even if some files could not be
  committed; `sync_status` reports those as `clean_working_tree: false`.
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

### "REJECTED: push contains files the hub refuses for every identity"

No vault.yaml change helps here: the hub refuses these files whoever pushes
them. Each violation says why:

- `(note must open with a plain --- line)`: the note's first line names a
  frontmatter language (`---js`, `---yaml`). Make it a bare `---`, then
  `git reset --soft origin/<branch>` (the message names the branch the note
  was pushed to) and
  recommit, so no unpushed commit keeps the old first line.
- `(path is not valid UTF-8)`: rename the file, then reset and recommit the
  same way.

A push can also be refused for its refs and its files at once, including
files on the refused ref itself; the message then lists both, under
separate `REJECTED:` lines.

### "REJECTED: cannot determine push identity"

`SCHIST_IDENTITY` (or `GL_USER` for gitolite) is not set in the environment
of whatever process ran `git push`. On HPC this usually means the SLURM job
wrapper didn't inherit your `.bashrc` exports — add an explicit
`export SCHIST_IDENTITY=hpc-cluster` to the job script.

### "REJECTED: failed to load vault.yaml"

The hub could not read the `vault.yaml` at its `HEAD`, so it refuses every
push rather than accept one unchecked. The rest of the message says why:

- **"not valid UTF-8".** `vault.yaml` must be UTF-8. A hook that ran under a
  latin-1 locale read such a file, so this can appear right after upgrading
  the hub's CLI. Pushing a fixed file does not help, since the hook reads the
  current one first, and neither does `schist hub`, which reads it the same
  way. On the hub, as the hub user, write the re-encoded file as a new commit
  directly (this sets the branch without running the hook; change `latin1` to
  the file's real encoding):

  ```bash
  cd /srv/git/vault.git                     # the hub's bare repository
  t=$(mktemp -d)                            # private: the ACL is readable here
  git cat-file blob HEAD:vault.yaml > "$t/old.yaml"
  iconv -f latin1 -t utf-8 "$t/old.yaml" > "$t/vault.yaml"
  blob=$(git hash-object -w "$t/vault.yaml")
  GIT_INDEX_FILE="$t/index" git read-tree HEAD
  GIT_INDEX_FILE="$t/index" git update-index --cacheinfo "100644,$blob,vault.yaml"
  tree=$(GIT_INDEX_FILE="$t/index" git write-tree)
  commit=$(git -c user.name="hub admin" -c user.email=admin@localhost \
             commit-tree "$tree" -p HEAD -m "Re-encode vault.yaml as UTF-8")
  git update-ref "$(git symbolic-ref HEAD)" "$commit" HEAD
  rm -rf "$t"
  ```

  Before upgrading a hub, `schist doctor --hub-path <hub>` reads `vault.yaml`
  the way the upgraded hook will: its "Hub vault.yaml" line FAILs for this
  case and the ones below.
- **A YAML or ACL error** (for example "while parsing a flow sequence", or a
  message about a participant or access entry). The file at `HEAD` is UTF-8
  but is not valid YAML or not a valid `vault.yaml`. As above, pushing a fixed
  file does not help, even as a `*` writer, because the hook parses the
  current file before it looks at the push. Use the same recipe on the hub,
  with the `iconv` line replaced by an edit of the copy: `cp "$t/old.yaml"
  "$t/vault.yaml"`, then fix `$t/vault.yaml` in an editor and check it with
  `python3 -c 'import sys, yaml; from schist.acl import parse_vault_data;
  parse_vault_data(yaml.safe_load(open(sys.argv[1], encoding="utf-8")))'
  "$t/vault.yaml"` (run with the hub's schist environment) before
  `git hash-object`. Change the commit message to say what was fixed.
- **"git exited 128 reading it".** git ran but could not read the file: a
  missing or broken object. Check the repository with
  `git --git-dir <hub> fsck` and restore the object from a spoke's clone.
- **"HEAD names refs/heads/<branch>, which does not exist".** The branch the
  hub serves was deleted or renamed, or `HEAD` names a different branch from
  the one spokes push. Point `HEAD` at the branch spokes push (the
  branch a spoke's `git rev-parse --abbrev-ref @{upstream}` names after
  `origin/`, usually `main`):
  `git --git-dir <hub> symbolic-ref HEAD refs/heads/<branch>`.
- **"the hub has objects but no refs".** The repository holds commits but no
  branch, so its refs were lost (a crash after `git pack-refs`, a restore that
  copied `objects/` without `refs/` and `packed-refs`). `schist hub init`
  always creates the branch with a first commit, so the hook does not read
  this as a new hub, which would accept any first push and any `vault.yaml`
  it carries. Restore the served branch to the commit spokes last pulled. The
  branch is the one `HEAD` names (`git --git-dir <hub> symbolic-ref HEAD`
  prints `refs/heads/<branch>`). On a spoke, `git rev-parse origin/<branch>`
  gives the commit. On the hub, check that the commit is there
  (`git --git-dir <hub> cat-file -t <sha>` prints `commit`;
  `git --git-dir <hub> fsck --lost-found` lists the unreferenced commits) and
  run `git --git-dir <hub> update-ref refs/heads/<branch> <sha>`.
  One case is not lost refs: a hub made by hand with `git init --bare` (not
  `schist hub init`) whose first push failed after the pre-receive hook passed
  it (a ref lock, a full disk). A push the hook refuses leaves nothing, but
  one that fails later leaves its objects. Only on such a hub, one that never
  had a branch, remove them. First check that
  `git --git-dir <hub> fsck --lost-found` lists no commit any spoke has, and
  copy `objects/` aside. Then, with no push in progress, run
  `git --git-dir <hub> repack -a -d` and
  `git --git-dir <hub> prune --expire=now` (`prune` alone leaves packed
  objects). The next push is then read as the hub's first. On a hub that had
  a branch these two commands delete its whole history: a bare repository
  keeps no reflog to recover it from.

### "ERROR: the hub could not read vault.yaml" or "...the branch it serves"

The hub's own git timed out (or could not be started) reading `vault.yaml`
or `HEAD`, so it refused the push without checking it. This is the hub's
fault, not the push's: on a loaded hub a later push may succeed, and the CLI
and the MCP server treat it as transient. If it keeps happening, look at the
hub's load and at `git --git-dir <hub> fsck`.

### "REJECTED: push is too large for the hub to check"

A listing the hub reads for the push has more than 1,000,000 file versions
(each changed file counted for each commit against each parent, plus the
change from the branch's old tip) or more than 128 MB of paths. Push fewer
commits at a time, for example a range of the history at a time with
`git push origin <commit>:refs/heads/main`, or split a commit that changes
very many files. A single path longer than 4096 bytes is refused with
"push contains a path longer than 4096 bytes"; no checkout could hold it.

### "REJECTED: rate limit exceeded"

Two different limits produce this line, and only one of them clears by
waiting — check which name is in the parentheses:

- **`git_syncs_per_hour`** is a sliding window. The current hook prints a
  `Retry after: N seconds` line with the next available slot. Waiting works;
  `sync_retry` reports `retriable: true` from the limit name even if a hub
  omits or rewords that separate line.
- **`notes_per_sync`** is a cap on the whole **push range**, enforced
  statelessly with no retry window and no `Retry after:` line. The identical
  push is rejected identically forever — waiting never helps. `sync_retry`
  reports `retriable: false, reason: "Rate limit (no retry window)"`.

  Adding more commits does **not** help: the hook counts files across
  `oldrev..newrev` and dedupes, so every commit you add is still inside the
  range being pushed. What matters is the *range*, so the remedy depends on
  how the backlog was built:

  - **Many commits** (the usual MCP case — each `create_note` commits on its
    own): push the range in pieces. Each push is its own range and is counted
    separately.

    ```bash
    git log --oneline origin/main..main     # pick a sha under the cap
    git push origin <intermediate-sha>:main
    git push origin main
    ```

  - **One commit** (the CLI path — `schist sync push` stages everything
    pending and makes a single `sync(<identity>): N files` commit): there is
    no intermediate sha, so there is nothing to split. Either raise the limit,
    or rewind and re-commit in batches before pushing:

    ```bash
    git reset --soft origin/main   # keep the files, drop the one big commit
    # then re-add and commit in groups under the cap, pushing after each
    ```

  Raising the limit instead is a hub-side edit to `rate_limits.notes_per_sync`
  for that identity in the hub's `vault.yaml` — root `vault.yaml` writes are
  rejected from a spoke, so make it on the hub and let spokes fast-forward.

  Two things to know before you start splitting:

  - **The two limits trade against each other.** `git_syncs_per_hour` defaults
    to 10, so splitting a large backlog into N pushes spends N of those slots
    and can turn a `notes_per_sync` rejection into a `git_syncs_per_hour` one.
    If the backlog is large enough to need many pieces, raise the limit
    instead.
  - **A manual `git push` does not clear the sentinel.** `.schist/last-sync-error`
    is only cleared by `sync_retry` or a successful background push, so after
    pushing by hand your pushes are fine but MCP writes are still blocked. Run
    `sync_retry mode=push-only` afterwards to clear it.

Both are reported as `failure_class: "rate-limited"` — the class says what
happened, `retriable` says whether repeating the same command could work.

### "Pull timed out — falling through"

The 5-second cap on `maybeSpokePull` is intentional. If you see this
repeatedly, run `schist sync pull` in a shell to get the real error (SSH
auth failure, hub down, DNS). The MCP server will keep serving the stale
local view until the hub comes back.

### "cannot pull with rebase: You have unstaged changes"

A pull refuses while tracked files have uncommitted edits. Either commit
them, or pull with `schist sync pull --autostash`: git sets the edits aside,
rebases, and puts them back. Autostash is opt-in on purpose -- the MCP
server's automatic pre-read pull skips a dirty tree rather than stashing
anything, and `sync_retry mode=pull-rebase-push` is the only MCP call that
autostashes. Edits that were staged come back unstaged.

### "Pulled, but your uncommitted edits conflict with the incoming changes"

An autostash pull rebased, but could not put your edits back -- because the
hub changed the same lines, or because something changed those files while
the pull ran. The edits are saved in the git stash; the message names it.
When the tree was left with conflict markers and nothing else had touched
it, it is reset to the pulled commit so no markers can be pushed (and `sync
push` refuses while any conflicted file remains). To finish:

```bash
cd <vault>
git stash list          # the entry named "autostash"
git stash pop           # re-apply; resolve anything it reports
```

then sync again. Untracked files are never stashed: one that collides with
a file arriving from the hub still fails the pull, and git restores your
stashed edits before it stops.

### "pre-receive: ModuleNotFoundError: schist.pre_receive"

The schist package isn't installed for the `python3` the hook uses. On the
hub, `uv pip install --system -e /path/to/schist/cli` (or `pip install -e /path/to/schist/cli`) and re-test with a dry push.

### "Can I use GitHub / Gitea / GitLab SaaS as the hub?"

Not directly — hosted git providers do not run `pre-receive` hooks, so the
ACL is bypassed. Options: (a) self-host a small hub (a Pi works), (b) run the
hook in CI on every push (clunky — it's post-push by then), or (c) skip ACLs
and rely on branch protection + review. Schist is designed around (a).

## Limiting what one push can cost the hub

The pre-receive hook streams what it reads and refuses a push that lists more
than 1,000,000 file versions or 128 MB of paths. Both caps count the whole
push, every ref it updates together, since a push can name the same commits
under many refs for a few bytes each; a push of several new branches at the
same commits counts their history once per branch. A participant without `*`
write may update only the served branch, so the hook refuses a push of theirs
that updates more than one ref before reading any commits. A refusal prints,
and writes to `hooks/rejected-pushes.log`, at most 100 refused paths and the
number of the rest. But git itself does work before the hook runs (receiving
and unpacking the push, listing its changes), and a participant can push
trees that are small to send and large to list. On a hub that also runs
anything else, bound that work at the host:

- `git --git-dir <hub> config receive.maxInputSize 200m` refuses a push whose
  pack is larger than that.
- A memory limit for the hub user makes a runaway git or hook fail, so the
  push does not land, instead of exhausting the host: an `as` (address space)
  limit in `/etc/security/limits.conf` (applied when sshd runs with
  `UsePAM yes`), or on cgroup v2 `MemoryMax=` on the user's slice
  (`systemctl set-property user-<uid>.slice MemoryMax=4G`). `as` counts
  virtual memory, and git maps pack files in windows of up to 1 GB, so set it
  to a few GB and check that a normal push still lands.

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
the push as pinned (`SCHIST_IDENTITY_PINNED=1`), passes git only `PATH`,
`HOME`, `USER`, `LOGNAME`, `SHELL`, `PYTHONPATH`, `VIRTUAL_ENV`, the
`SSH_CONNECTION`/`SSH_CLIENT`/`SSH_ORIGINAL_COMMAND` variables sshd sets and
`GIT_PROTOCOL`, dropping everything else (so set anything else git needs in
the hub user's git config, not `~/.ssh/environment`), and confines the key to `git-receive-pack` /
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
   don't forward `PATH`, `HOME`, `VIRTUAL_ENV`, `PYTHON*`, `LD_*`,
   `BASH_ENV` or `GIT_*` either (`GIT_PROTOCOL` alone is fine). The wrapper
   passes the first four on, and they redirect what the forced command and
   the hook execute or read. `schist-shell` drops `LD_*`, `BASH_ENV` and the
   other `GIT_*` itself, but sshd starts the forced command through the hub
   user's login shell, which sees them first, and a client-forwarded one
   reaches anything sshd runs that is not `schist-shell`.
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

On a hub-only host (no `--vault`, no schist MCP entry) doctor reports the Node.js
check as SKIP: the hub runs only git and Python, so it does not need Node.

**Spotting drift.** Run `schist doctor --hub-path /srv/vault.git` on the hub to
flag directories in the schema that no participant can write, or directories
some participants can write but others cannot. Spokes get the matching
spoke-side warning automatically from `schist doctor` (it checks the local
schema against the spoke's hub write grant).
