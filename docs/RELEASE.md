# Release Checklist

This document is for maintainers preparing a schist release.

## Pre-Tag Checklist

Before creating a release tag, verify the following:

### 1. Version Bumps

Update these files with the new version (e.g., `0.1.0` → `0.2.0`):

- `cli/pyproject.toml`: `version = "X.Y.Z"`
- `mcp-server/package.json`: `"version": "X.Y.Z"`
- `CHANGELOG.md`: Move `[Unreleased]` entry to `[X.Y.Z]` with release date
- Add new `[Unreleased]` section at top for next cycle

### 2. CHANGELOG Entry

Summarize changes since the last release in Keep a Changelog format:
- `Added` — new features
- `Changed` — changes to existing functionality
- `Fixed` — bug fixes
- `Security` — security-relevant changes

### 3. CI Status

Ensure all CI checks pass on `main`:
- [ ] CLI tests (`python -m pytest cli/tests viewer/tests`)
- [ ] MCP server tests (`npm test` in `mcp-server/`)
- [ ] Type-check passes (`npm run build` completes cleanly)

### 4. GitHub Environments

Verify these environments exist in the repository (Settings → Environments):
- `pypi` — for PyPI Trusted Publishing (OIDC)
- `npm` — for npm publish with `NPM_TOKEN` secret

Optionally enable "Require reviewers" for manual approval before publish.

## Tagging and Publishing

### Dry-Run (First Release or Big Changes)

For a dry-run that publishes to TestPyPI but not npm:

```bash
# Tag release candidate
git tag v0.1.0-rc.1
git push origin v0.1.0-rc.1
```

Modify `release.yml` `publish-pypi` job to use `repository-url: https://test.pypi.org/legacy/`
and a separate `TESTPYPI_TOKEN` secret for the dry-run. Revert after verification.

### Production Release

Once dry-run succeeds:

```bash
# Ensure CHANGELOG is updated (version + date)
# Verify version numbers in pyproject.toml and package.json
git tag vX.Y.Z
git push origin vX.Y.Z
```

The `release.yml` workflow will:
1. Build CLI wheel and publish to PyPI via Trusted Publishing (OIDC)
2. Build MCP server tarball and publish to npm via `NPM_TOKEN`

Both jobs run in parallel. If one fails, the other may still have succeeded — check
PyPI (`https://pypi.org/project/schist/`) and npm (`https://www.npmjs.com/package/@schist/mcp-server`).

## Post-Release

### 1. Verify Packages

- [ ] `pip install schist` in a fresh venv works
- [ ] `schist --help` runs
- [ ] `npm install -g @schist/mcp-server` works
- [ ] `schist-ingest` console script exists on PATH after pip install

### 2. GitHub Release (Optional)

Create a GitHub Release at <https://github.com/yibeichan/schist/releases/new>:
- Tag: the just-pushed tag
- Title: `vX.Y.Z`
- Body: copy relevant section from CHANGELOG.md

### 3. Post-Release Cleanup Tasks

Items tracked for future releases:
- [ ] Migrate npm publish from `NPM_TOKEN` to OIDC Trusted Publishing (post-v0.1.0)
- [ ] Add `list_concepts` alias inclusion (separate small PR)
- [ ] Cross-project memory migration pass (separate session)

## Rollback

If a release needs to be yanked:

- **PyPI**: Login to <https://pypi.org/manage/project/schist/releases/> and yank the version
- **npm**: `npm deprecate @schist/mcp-server@X.Y.Z "Reason for deprecation"`

For security issues, contact maintainers directly before filing public issues.
