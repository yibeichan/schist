# Hub & Spoke: Pi + HPC + Mac Topology

Opinionated setup guide for a three-node schist hub-spoke deployment:
- **Pi** as the hub (bare git repo with pre-receive hook ACL enforcement)
- **HPC** as a spoke (uv + venv, or Singularity as fallback)
- **Mac** (Apple Silicon) as a spoke
- **GitHub** as an optional backup mirror

For the general hub-spoke concepts and troubleshooting, see [hub-spoke-setup.md](./hub-spoke-setup.md).

## 1. Topology Overview

```
                  ┌─────────────┐
                  │   Pi (Hub)  │
                  │ bare repo + │
                  │ pre-receive │
                  └──────┬──────┘
                    SSH  │  SSH
              ┌─────────┼─────────┐
              │                   │
      ┌──────────────┐   ┌──────────────┐
      │  Mac (Spoke) │   │  HPC (Spoke) │
      │ scope:       │   │ scope:       │
      │ research/mac │   │ research/hpc │
      └──────────────┘   └──────────────┘
              │                   │
              └─────────┬─────────┘
                        │
                 GitHub (mirror)
```

Two options:

**Option A (recommended):** Pi bare repo as hub. Full ACL enforcement via pre-receive hook. The hub rejects any push that writes outside the spoke's declared scope.

**Option B (quick start):** GitHub as hub. Simpler setup but no ACL enforcement -- GitHub SaaS does not run custom hooks. Scope isolation is trust-based. Fine for a single user; do not use for multi-user deployments.

## 2. Hub Setup (Option A -- Pi)

```bash
# On Pi, create the bare hub
schist init --hub \
  --hub-path ~/git/schist-vault.git \
  --name research \
  --participant pi \
  --participant mac \
  --participant hpc
```

This creates `~/git/schist-vault.git` as a bare repo, installs `hooks/pre-receive`, and seeds an initial commit with `vault.yaml`. Each participant gets scope `research/<name>` and write access to that scope, plus read access to everything.

### SSH setup

```bash
# On each spoke, generate a dedicated key
ssh-keygen -t ed25519 -f ~/.ssh/schist_spoke

# On Pi, add the public keys
cat ~/.ssh/schist_spoke.pub >> ~/.ssh/authorized_keys

# On each spoke, add to ~/.ssh/config:
Host schist-hub
    HostName <pi-ip-or-hostname>
    User <pi-username>
    IdentityFile ~/.ssh/schist_spoke
```

Replace `<pi-ip-or-hostname>` and `<pi-username>` with your Pi's actual values. For example, if your Pi is at `192.168.1.42` with user `yibei`:

```
Host schist-hub
    HostName 192.168.1.42
    User yibei
    IdentityFile ~/.ssh/schist_spoke
```

Test it: `ssh schist-hub echo ok` should print `ok` without a password prompt.

### Optional GitHub mirror

```bash
# On Pi, add GitHub as a push mirror
cd ~/git/schist-vault.git
git remote add github https://github.com/yibeichan/schist-vault.git

# Push to mirror periodically via cron
# Add to crontab (every 15 minutes):
# */15 * * * * cd ~/git/schist-vault.git && git push github --mirror
```

The mirror is read-only from the spokes' perspective. It exists as a backup and for browsing. Spokes never push to or pull from GitHub directly -- the Pi hub is the single source of truth.

## 3. Hub Setup (Option B -- GitHub)

Skip the Pi hub entirely. Spokes clone directly from:

```
https://github.com/yibeichan/schist-vault.git
# or
git@github.com:yibeichan/schist-vault.git
```

No `schist init --hub` needed. No ACL enforcement. Scope isolation is trust-based -- each spoke's `spoke.yaml` declares its scope, but nothing prevents a rogue push outside it. This is acceptable for a single-user setup where you control all spokes.

To upgrade to Option A later, set up the Pi hub and change each spoke's remote URL. No data migration needed.

## 4. Mac Spoke Setup

### Prerequisites (Apple Silicon)

```bash
brew install python@3.13 node git
```

### Install schist

```bash
pip install schist
# Or from source: pip install -e /path/to/schist/cli
```

### Initialize spoke

For Option A (Pi hub):

```bash
schist init --spoke \
  --hub schist-hub:~/git/schist-vault.git \
  --scope research/mac \
  --identity mac
```

For Option B (GitHub hub):

```bash
schist init --spoke \
  --hub git@github.com:yibeichan/schist-vault.git \
  --scope research/mac \
  --identity mac
```

### Configure MCP for Claude Code

```bash
schist --vault ~/schist-vault init --print-mcp-config --format claude --identity mac
```

Paste the output into `~/.claude/settings.json`. The result looks like:

```json
{
  "mcpServers": {
    "schist": {
      "command": "node",
      "args": ["/path/to/schist/mcp-server/dist/index.js"],
      "env": {
        "SCHIST_VAULT_PATH": "/Users/yibei/schist-vault",
        "SCHIST_IDENTITY": "mac"
      }
    }
  }
}
```

### Verify

```bash
schist doctor --vault ~/schist-vault
```

All checks should pass (green). If `schist doctor` is not yet implemented, verify manually:

```bash
# Can we reach the hub?
git -C ~/schist-vault push --dry-run
# Is SQLite being built?
ls -la ~/schist-vault/.schist/schist.db
```

## 5. HPC Spoke Setup

### Option A: uv (recommended)

Most HPC clusters provide Python and Git via environment modules. Use `uv` to install schist into a venv — no root, no containers.

```bash
# Load available modules (adjust names to your cluster)
module load python/3.12 git sqlite 2>/dev/null || true

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# Create venv and install schist
uv venv ~/schist-venv
source ~/schist-venv/bin/activate
uv pip install git+https://github.com/yibeichan/schist.git
```

If Node.js >= 20 is not available via modules:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 20
```

#### Initialize the spoke

```bash
schist init --spoke \
  --hub schist-hub:~/git/schist-vault.git \
  --scope research/hpc \
  --identity hpc
```

#### Verify

```bash
schist doctor --vault ~/schist-vault
```

### Option B: Singularity/Apptainer (fallback)

Use this only if the cluster lacks Python 3.12+ or Node 20+ and you cannot install them.

#### Definition file

Create `schist.def`:

```
Bootstrap: docker
From: node:20-bookworm-slim

%post
    apt-get update && apt-get install -y \
        python3 python3-pip python3-venv git openssh-client
    pip3 install --break-system-packages schist

%environment
    export SCHIST_VAULT_PATH=/data/vault
    export SCHIST_IDENTITY=hpc

%runscript
    exec "$@"
```

Build on the login node (Apptainer needs root to build; use `--remote` or build on a machine where you have root, like the Pi):

```bash
# On Pi or local machine with root:
apptainer build schist-hpc.sif schist.def

# Copy to HPC:
scp schist-hpc.sif login-node:/scratch/$USER/
```

### SSH key handling for HPC

Applies to both uv and Singularity setups.

1. **Generate a key on the login node:**
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/schist_spoke
   cat ~/.ssh/schist_spoke.pub >> ~/.ssh/authorized_keys  # on Pi
   ```

2. **Add to `~/.ssh/config`:**
   ```
   Host schist-hub
       HostName <pi-ip-or-hostname>
       User <pi-username>
       IdentityFile ~/.ssh/schist_spoke
   ```

3. **Pre-populate known_hosts** (avoids interactive prompts):
   ```bash
   ssh-keyscan <pi-ip-or-hostname> >> ~/.ssh/known_hosts
   ```

### SLURM job wrappers

#### Option A (uv)

```bash
#!/bin/bash
#SBATCH --job-name=schist-note
#SBATCH --time=00:05:00
#SBATCH --output=schist-note-%j.log

source ~/schist-venv/bin/activate
export SCHIST_IDENTITY=hpc

schist add --vault ~/schist-vault \
  --title "Training results $(date +%F)" \
  --body "Loss: $TRAIN_LOSS, Accuracy: $TRAIN_ACC" \
  --dir research/hpc
```

#### Option B (Singularity)

```bash
#!/bin/bash
#SBATCH --job-name=schist-note
#SBATCH --time=00:05:00
#SBATCH --output=schist-note-%j.log

export SCHIST_IDENTITY=hpc

apptainer run --bind /scratch/$USER/vault:/data/vault \
              --bind $HOME/.ssh:/root/.ssh:ro \
              schist-hpc.sif \
              schist add --vault /data/vault \
                --title "Training results $(date +%F)" \
                --body "Loss: $TRAIN_LOSS, Accuracy: $TRAIN_ACC" \
                --dir research/hpc
```

Submit: `sbatch schist-note.sh`

For a multi-note batch job, write several notes then push once at the end:

```bash
#!/bin/bash
#SBATCH --job-name=schist-batch
#SBATCH --time=00:10:00

source ~/schist-venv/bin/activate  # for uv; remove if using Singularity
export SCHIST_IDENTITY=hpc

for run in /scratch/$USER/runs/*.log; do
    schist add --vault ~/schist-vault \
      --title "Run $(basename $run .log)" \
      --body "$(tail -5 "$run")" \
      --dir research/hpc
done

schist sync push --vault ~/schist-vault
```

### Verify (Option B)

```bash
apptainer run --bind /scratch/$USER/vault:/data/vault \
              --bind $HOME/.ssh:/root/.ssh:ro \
              schist-hpc.sif \
              schist doctor --vault /data/vault
```

## 6. How Scope Works

Scopes enforce **write isolation, read sharing**:

- HPC writes to `research/hpc/` (its scope)
- Mac writes to `research/mac/` (its scope)
- Both can read the full graph via `search_notes` and `get_context`
- ACL enforcement (Option A): the pre-receive hook on the Pi rejects any push that writes outside the spoke's declared scope

### Example cross-machine workflow

On HPC, write training results:

```bash
schist add --vault /data/vault --title "Training run 42" \
  --body "Loss: 0.03, Acc: 97.2%" --dir research/hpc
```

On Mac, pull and connect:

```bash
# Pull HPC's latest notes
schist sync pull

# Write your analysis in your own scope
schist add --vault ~/schist-vault --title "Analysis of training 42" \
  --body "Convergence looks good" --dir research/mac

# Link your analysis to the HPC training note
schist link --source research/mac/2026-04-24-analysis.md \
  --target research/hpc/2026-04-24-training-42.md --type extends

# Push to hub
schist sync push
```

Neither side can modify the other's files. Both see the full graph.

## 7. Cross-verification Checklist

Run these in order after initial setup. All five should pass before declaring the topology operational.

- [ ] **Pi hub reachable**: `git ls-remote schist-hub:~/git/schist-vault.git` returns refs
- [ ] **Mac spoke healthy**: `schist doctor --vault ~/schist-vault` all green
- [ ] **HPC spoke healthy**: `schist doctor --vault /data/vault` all green (inside container)
- [ ] **Mac writes, HPC reads**: `schist add` on Mac, then `schist sync pull` on HPC -- the note appears
- [ ] **HPC writes, Mac reads**: `schist add` on HPC, then `schist sync pull` on Mac -- the note appears

If the cross-write tests fail, check SSH connectivity first (`ssh schist-hub echo ok` from each spoke), then check that `SCHIST_IDENTITY` is set correctly in each environment.
