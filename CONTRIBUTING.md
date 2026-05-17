# Contributing to schist

Thanks for your interest. schist is an open-source, agent-first knowledge graph — we welcome contributions from humans and agents alike.

## Quick Start

```bash
# Clone
git clone https://github.com/yibeichan/schist.git
cd schist

# CLI (Python)
cd cli
uv pip install --system -e .
# or: pip install -e .

# MCP Server (Node.js)
cd ../mcp-server
npm install
npm run build
```

Verify everything works:

```bash
# CLI tests
python -m pytest cli/tests

# MCP server tests
cd mcp-server && npm test

# Integration check
schist doctor --vault /tmp/test-vault
```

## Development Setup

### Prerequisites

- Python ≥ 3.12
- Node.js ≥ 20
- Git ≥ 2.30
- SQLite ≥ 3.39 (with FTS5 support)

### Recommended

- [uv](https://docs.astral.sh/uv/) for Python dependency management
- Node via [nvm](https://github.com/nvm-sh/nvm) or [fnm](https://github.com/Schniz/fnm)

## How to Contribute

### Report Issues

Open a [GitHub Issue](https://github.com/yibeichan/schist/issues/new). Include:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (`schist doctor` output if possible)

### Submit Changes

1. **Fork** the repo
2. **Create a branch** from `main`: `git checkout -b your-name/short-description`
3. **Make your changes** with clear, focused commits
4. **Run tests** before pushing:
   ```bash
   python -m pytest cli/tests viewer/tests
   cd mcp-server && npm test
   ```
5. **Push** your branch and open a Pull Request against `main`

### PR Conventions

- **One concern per PR** — don't mix features, fixes, and refactors
- **Write a clear description** — what changed, why, and how to verify
- **Update docs** if you changed behavior (README, docs/, CHANGELOG.md)
- **Add tests** for new functionality
- **Keep CHANGELOG.md** updated under `[Unreleased]`

### Commit Messages

Use conventional format:

```
type: short description

Longer explanation if needed.
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`

If you're an AI agent, prefix with `[agent-name]:` — e.g. `[octopus]: update README`. This helps track who (or what) made the change.

## Project Structure

```
schist/
├── cli/              # Python CLI + ingestion
│   ├── schist/       # Source code
│   └── tests/        # CLI tests
├── mcp-server/       # Node.js MCP server
│   ├── src/          # TypeScript source
│   └── tests/        # Server tests
├── viewer/           # Static web viewer (D3.js + lunr.js)
│   ├── src/
│   ├── static/
│   └── tests/
├── hooks/            # Git hooks (pre-commit, post-commit)
├── schema/           # Markdown schema specification
├── docs/             # Documentation
└── PLAN.md           # Architecture document
```

## Architecture Overview

- **Git is truth** — all content is markdown + YAML in a git repo
- **SQLite is query** — rebuilt from markdown on every commit via post-commit hook
- **MCP Server** — the primary interface for agents (Node.js + TypeScript)
- **CLI** — fallback for agents + human command line (Python)
- **Viewer** — static HTML/JS, no server required

See [PLAN.md](./PLAN.md) for the full architecture.

## Code Style

- **TypeScript**: strict mode, no `any` without justification
- **Python**: follow existing patterns, type hints where practical
- **Markdown**: YAML frontmatter must conform to [schema/SCHEMA.md](./schema/SCHEMA.md)

## Security

- Never commit secrets, API keys, or tokens
- The pre-commit hook will reject commits containing common secret patterns
- If you find a security vulnerability, email the maintainer instead of opening a public issue

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](./LICENSE).

## Questions?

Open an issue with the `question` label, or start a [Discussion](https://github.com/yibeichan/schist/discussions) if those are enabled.
