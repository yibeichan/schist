# Getting Started

Linear setup guide. Each stage ends with `schist doctor` verification.

## Requirements

| Dependency | Minimum |
|-----------|---------|
| Python    | >=3.12  |
| Node.js   | >=20    |
| Git       | >=2.30  |
| SQLite    | >=3.39 (with FTS5) |

---

## Stage 1: Install prerequisites

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-pip git sqlite3

# Node.js 20+ via NodeSource
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

Or via nvm:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 20
```

### macOS (Homebrew)

```bash
brew install python@3.13 node git sqlite
```

### HPC (Singularity/Apptainer)

Save as `schist.def` and build with `singularity build schist.sif schist.def`:

```singularity
Bootstrap: docker
From: python:3.12-slim

%post
    apt-get update && apt-get install -y --no-install-recommends \
        git sqlite3 curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
    pip install --no-cache-dir schist

%environment
    export SCHIST_VAULT_PATH=/vault
    export PATH=/usr/local/bin:$PATH

%runscript
    exec "$@"
```

### Verify Stage 1

```bash
python3 --version   # expect 3.12+
node --version      # expect v20+
git --version       # expect 2.30+
sqlite3 --version   # expect 3.39+
```

---

## Stage 2: Install schist

### Option A: Published packages

```bash
pip install schist
npm install -g @schist/mcp-server
```

### Option B: From source (clone)

```bash
git clone https://github.com/yibeichan/schist.git
cd schist

pip install -e ./cli
cd mcp-server && npm install && npm run build && cd ..
```

### Verify Stage 2

```bash
schist --help
schist-ingest --help
command -v node
```

---

## Stage 3: Create vault + configure MCP

### Initialize vault

```bash
schist init ~/my-vault --name my-vault --identity local
```

This creates:
- Git repo at `~/my-vault` with `vault.yaml`, `notes/`, `concepts/`, `papers/`
- Post-commit hook that rebuilds SQLite on every commit
- Pre-commit hook that rejects staged secrets

Set the environment variable (add to shell profile):

```bash
export SCHIST_VAULT_PATH=~/my-vault
```

### Generate MCP server config

```bash
schist --vault ~/my-vault init --print-mcp-config --format claude --identity local
```

This prints a JSON block. Paste the `mcpServers` object into the appropriate config file:

**Claude Code** (global):
```
~/.claude/settings.json
```

**Claude Code** (project-scoped):
```
<project>/.claude/settings.json
```

**Claude Desktop** (macOS):
```
~/Library/Application Support/Claude/claude_desktop_config.json
```

**Claude Desktop** (Linux):
```
~/.config/claude/config.json
```

**Cursor**:
```
<project>/.cursor/mcp.json
```

For Cursor format, use `--format cursor`:
```bash
schist --vault ~/my-vault init --print-mcp-config --format cursor --identity local
```

If auto-detection of `mcp-server/dist/index.js` fails, specify the path explicitly:
```bash
schist --vault ~/my-vault init --print-mcp-config \
  --mcp-server-path /path/to/schist/mcp-server/dist/index.js
```

### Verify Stage 3

```bash
schist doctor --vault ~/my-vault
```

Expected: all checks PASS (except possibly MCP until config is pasted and client restarted).

---

## Stage 4: Verify end-to-end

```bash
# Full health check
schist doctor --vault ~/my-vault

# Create a note (triggers post-commit hook -> SQLite rebuild)
schist add --vault ~/my-vault --title "Test note" --body "Hello, knowledge graph."

# Search it back
schist search --vault ~/my-vault "test"

# View vault summary
schist context --vault ~/my-vault --depth minimal

# Connect from MCP: call get_context, then request_capabilities({capability: "write"})
```

All four commands should return without error. `schist doctor` should show PASS for every check.

---

## Troubleshooting

### 1. `SCHIST_VAULT_PATH not set`

```bash
export SCHIST_VAULT_PATH=/path/to/vault
```

Add to `~/.bashrc` or `~/.zshrc` for persistence.

### 2. `schist-ingest: command not found`

```bash
pip install -e ./cli    # from source
# or
pip install schist      # published
```

Verify: `which schist-ingest`. If installed but not found, check that pip's bin directory is on `$PATH`.

### 3. `Node.js not found`

```bash
which node || echo "install Node.js 20+"
```

If installed via nvm, make sure `nvm use default` runs in your shell profile.

### 4. `SQLite DB not found`

The post-commit hook should rebuild it automatically. If the hook hasn't fired:

```bash
schist-ingest --vault ~/my-vault --db ~/my-vault/.schist/schist.db
```

Or trigger a dummy commit:

```bash
cd ~/my-vault && git commit --allow-empty -m "trigger ingest"
```

### 5. `Post-commit hook not installed`

```bash
schist init ~/my-vault --name my-vault --identity local
```

Re-running `schist init` on an existing vault is not supported. Check the hook exists:
```bash
ls ~/my-vault/.git/hooks/post-commit
```

If missing, reinstall the vault or manually copy the hook script.

### 6. `MCP server fails to start`

Checklist:
- `schist.yaml` exists in vault root (created by `schist init`)
- `SCHIST_VAULT_PATH` in the MCP config is an absolute path
- `mcp-server/dist/index.js` exists at the path in the config
- Node.js >=20 is on the PATH that launches the MCP server

### 7. `better-sqlite3 build fails`

This npm native addon requires a C++ toolchain. Common on HPC and minimal containers.

```bash
# Install build tools
sudo apt install -y build-essential python3     # Debian
xcode-select --install                           # macOS

# Or use a prebuilt binary
npm install --build-from-source=false better-sqlite3
```

If building from source is not an option, use the Singularity image from Stage 1.

### 8. `Hub unreachable from spoke`

```bash
# Verify SSH connectivity
ssh -T git@<hub-host>

# Verify the hub URL
git ls-remote <hub-url>

# Check spoke config
cat ~/my-vault/.schist/spoke.yaml
```

Common causes: SSH key not loaded, hub URL wrong, or firewall blocking the connection.
