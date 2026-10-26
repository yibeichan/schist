# Getting Started

Linear setup guide. Each stage ends with `schist doctor` verification.

## Requirements

| Dependency | Minimum |
|-----------|---------|
| Python    | >=3.12  |
| Node.js   | >=22    |
| npm       | >=11.11 (only to build `mcp-server/` from source; Node 22 bundles npm 10) |
| Git       | >=2.30  |
| SQLite    | >=3.39 (with FTS5) |

---

## Stage 1: Install prerequisites

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install -y python3 python3-pip git sqlite3

# Node.js 22+ via NodeSource
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

Or via nvm:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
```

### macOS (Homebrew)

```bash
brew install python@3.13 node git sqlite
```

### HPC (uv)

Most HPC clusters provide Python and Git via modules. Use `uv` to install schist without root or containers:

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

If Node.js >= 22 is not available via modules, install with nvm:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
```

If neither Python 3.12+ nor Node 22+ is available, use the Singularity fallback (see below).

### Verify Stage 1

```bash
python3 --version   # expect 3.12+
node --version      # expect v22+
git --version       # expect 2.30+
sqlite3 --version   # expect 3.39+
```

---

## Stage 2: Install schist

### Option A: Published packages (stable release)

```bash
pip install schist
npm install -g @schist/mcp-server
```

> **Note:** `pip install schist` installs the latest published release.
> For unreleased changes on `main`, install directly from git:
> ```bash
> pip install git+https://github.com/yibeichan/schist.git
> ```
> Or from a local clone (see Option B below).

### Option B: From source (latest development)

schist recommends [uv](https://docs.astral.sh/uv/) for source installs — faster
than pip and reproducible via the checked-in `cli/uv.lock`. pip still works as
a fallback.

```bash
git clone https://github.com/yibeichan/schist.git
cd schist

uv pip install --system -e ./cli   # or:  pip install -e ./cli
npm install -g npm@latest           # mcp-server/ needs npm >= 11.11; Node 22 bundles npm 10
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

This prints a runnable `claude mcp add` command — paste-and-run to register
schist as a user-scope MCP server (stored in `~/.claude.json`). It also emits
a commented-out JSON fallback below the command for older Claude Code CLIs
that predate `mcp add` / `--scope`; in that case, uncomment the block and
hand-merge it under the `mcpServers` key in `~/.claude.json`.

**Claude Desktop** (macOS) — uses a different config file; merge the
commented JSON under `mcpServers` in:
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

# Connect from MCP: call get_context to see the vault, then call any read/write tool directly
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
uv pip install --system -e ./cli   # from source (or: pip install -e ./cli)
# or, for end users:
pip install schist                 # published
```

Verify: `which schist-ingest`. If installed but not found, check that pip's bin directory is on `$PATH`.

### 3. `Node.js not found`

```bash
which node || echo "install Node.js 22+"
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

### 5. `Post-commit hook not installed` / `not executable`

Reinstall the hooks in place — re-running `schist init` on an existing vault is
not supported, but there is a dedicated command:

```bash
schist --vault ~/my-vault hooks reinstall
```

`doctor` reports two distinct failures here, and they need different fixes:

- **`not installed`** — the file is absent. Reinstall as above.
- **`installed but not executable — git skips it silently`** — the file is
  there but git will not run it, so nothing happens and nothing complains.
  `chmod +x` the path named in the message. This matters most for
  `pre-commit`, which is the staged-secret scanner: without the execute bit
  `git commit` stops scanning and says nothing.

`doctor` names the exact path it checked, and that is not always
`.git/hooks/` — if `core.hooksPath` is set, git looks in the configured
directory instead and schist's hooks there are the ones that count. Either
symlink them in, or unset the redirect:

```bash
git -C ~/my-vault config --unset core.hooksPath
schist --vault ~/my-vault hooks reinstall
```

### 6. `MCP server fails to start`

Checklist:
- `schist.yaml` exists in vault root (created by `schist init`)
- `SCHIST_VAULT_PATH` in the MCP config is an absolute path
- `mcp-server/dist/index.js` exists at the path in the config
- Node.js >=22 is on the PATH that launches the MCP server

### 7. `better-sqlite3 build fails`

Since better-sqlite3 v13 this should no longer happen: the npm package ships
prebuilt binaries for `darwin-arm64`, `darwin-x64`, `linux-arm64`, `linux-x64`,
`linuxmusl-arm64`, `linuxmusl-x64`, `win32-arm64` and `win32-x64` **inside the
tarball**, and declares `gypfile: false`, so a normal install copies a binary
into place and never invokes a compiler. On a supported platform the install
takes well under a second and leaves no `build/` directory.

Two things that look like this failure but are not:

- `npm warn allow-scripts better-sqlite3@13.x (install: node-gyp rebuild)` is
  inert. npm synthesizes that line from the presence of `binding.gyp`, but
  `gypfile: false` means it is never run.
- `EBADENGINE` naming `node: >=22` is a Node version problem, not a build
  problem — see the requirements table at the top of this page.

If you are on a platform with no bundled prebuild, a source build needs a C++
toolchain:

```bash
sudo apt install -y build-essential python3     # Debian
xcode-select --install                           # macOS
npm rebuild better-sqlite3 --build-from-source
```

(`--build-from-source=false` was the v12-era escape hatch, when the binary was
downloaded from GitHub releases at install time. v13 publishes no release
assets, so that flag no longer does anything.)

If building from source is not an option, use the Singularity fallback:

```singularity
Bootstrap: docker
From: python:3.12-slim

%post
    apt-get update && apt-get install -y --no-install-recommends \
        git sqlite3 curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
    pip install --no-cache-dir schist

%environment
    export SCHIST_VAULT_PATH=/vault
    export PATH=/usr/local/bin:$PATH

%runscript
    exec "$@"
```

Build with `singularity build schist.sif schist.def`.

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

### 9. `Vault access: MCP servers other than schist can read the vault`

A server whose command, arguments or environment name the vault, a folder inside it, or a folder that contains it (for example a filesystem server rooted at `~`) reads notes directly, without schist's scope and ACL checks. `schist doctor` reads the servers each client would start:

- Claude Code: user and local scope in `~/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json`), and project scope in `.mcp.json` in the project folder and every folder above it, leaving out servers listed in `disabledMcpjsonServers`;
- Codex: `${CODEX_HOME:-~/.codex}/config.toml`, and `.codex/config.toml` in projects with `trust_level = "trusted"`, leaving out servers with `enabled = false`; variables named in `env_vars` are read from the environment;
- Cursor: `~/.cursor/mcp.json`, and `.cursor/mcp.json` in projects;
- Claude Desktop and Claude Science configs.

Projects are the vault, every project Claude Code lists, and every project Codex trusts. Paths are found inside longer strings too (`--root=<path>`, `src=<path>,dst=...`, `-v <path>:/data`, quoted words in a shell script, `file://` URLs), after expanding the variables these clients expand (`${HOME}`, `${VAR:-default}`, `${env:NAME}`, `${userHome}`, `${workspaceFolder}`, and `$NAME` in a shell command's script); a variable the server's own `env` block defines is taken from there. `/` on its own counts as a folder that contains the vault, even where it is a URL path such as `--sse-path=/`; allow such a server by name. Relative paths (`.`, `./x`, `../x`) count against the server's `cwd` or its project folder, and so does a bare name such as `notes` when it is a positional argument that exists there; the folder a server starts in does not count by itself. Environment variables such as `HOME` and `TMPDIR` count only when they point into the vault. The result names each server and field, never the value.

The check WARNs when it cannot decide: a config file it cannot parse, a variable that is not set in the shell running `doctor` (in arguments, or in an environment variable whose name suggests a path), a `./` path with no known start folder, or a malformed allowlist. It cannot see a folder given to a server some other way, such as through the MCP roots a client offers or a Claude Desktop extension's own settings, and servers reached by URL have no local paths to check; the PASS message names those.

The schist server itself is recognized by what it runs: the `@schist/mcp-server` npm package, or a script whose `package.json` names that package, whatever its entry is called. Only when that cannot be told (a wrapper script, or a build missing from this machine) does the entry's name decide: `schist`, `schist-<name>` or `schist <name>`, with `SCHIST_VAULT_PATH` in its environment or a `mcp-server/dist/index.js` script.

Remove the vault path, or the folder that contains it, from that server. If a server should read the vault, list it by name in the vault's `schist.yaml`, quoting names YAML would read as something else (`'on'`, `'007'`):

```yaml
vault_access:
  allowed_mcp_servers: [obsidian]
```

The check then passes and names the allowed servers. The list applies to servers of that name on every machine that shares the vault. On a spoke, `schist.yaml` comes from the hub, and `schist sync push` does not send it: a participant who can write the vault root commits and pushes the change with git, and each spoke gets it on its next `schist sync pull`. An uncommitted edit to `schist.yaml` on a spoke stops that spoke from pulling.
