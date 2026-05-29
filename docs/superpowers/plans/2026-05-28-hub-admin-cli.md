# Hub ACL Admin CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `schist hub` admin CLI that edits a bare hub's `vault.yaml` in place (grant/revoke write scopes, add/rename/remove participants), plus a hub-mode `doctor` drift lint and docs.

**Architecture:** Admin authority is filesystem access to the bare repo. Pure mutation functions edit the parsed `vault.yaml` dict; one I/O helper commits the result directly via git plumbing (`hash-object`/`commit-tree`/`update-ref` with compare-and-swap) so `receive-pack`/`pre-receive` never fire. Every commit is gated by strict `parse_vault_data()` re-validation (fail-closed). `'*'` write grants are refused so no participant ever holds ACL-edit power.

**Tech Stack:** Python 3.12, `argparse`, `PyYAML`, `git` plumbing via `subprocess`, `pytest`. Reuses `schist.acl` (`parse_vault_data`, `NAME_RE`, `_validate_scope`, `_scope_matches`, `ACLError`).

**Spec:** `docs/superpowers/specs/2026-05-28-hub-admin-cli-design.md`

---

## File Structure

- **Create `cli/schist/hub_admin.py`** — pure mutation functions (`grant_write`, `revoke_write`, `participant_add`, `participant_rename`, `participant_remove`), the I/O layer (`read_hub_vault`, `commit_vault_yaml`, `apply_mutation`), and CLI entry functions (`cmd_grant`, `cmd_revoke`, `cmd_participant_add/rename/remove`). `HubAdminError` exception.
- **Modify `cli/schist/__main__.py`** — add `hub` subparser + subcommands; early-dispatch `hub` (no `--vault` needed); add `--hub-path` to the `doctor` subparser.
- **Modify `cli/schist/doctor.py`** — add `check_hub_acl_drift(hub_path)` + `_hub_expected_dirs(hub)`; wire into `run_doctor` when a hub path is supplied; read `--hub-path` in `doctor()`.
- **Create `cli/tests/test_hub_admin.py`** — unit tests (pure mutations) + integration tests (real bare repo via `init_hub`).
- **Modify `cli/tests/test_doctor.py`** — hub drift lint tests.
- **Modify `docs/hub-spoke-setup.md`** — "Administering ACLs" section.

**Test invocation (per CLAUDE.md):**
```bash
cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py -v
```

---

## Task 1: Scaffold `hub_admin.py` + `grant_write`

**Files:**
- Create: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing tests**

```python
# cli/tests/test_hub_admin.py
"""Tests for the schist hub admin CLI (hub_admin.py)."""

from __future__ import annotations

import copy

import pytest

from schist import hub_admin
from schist.hub_admin import HubAdminError


def _seed_data():
    """A minimal valid vault.yaml dict matching _build_seed_vault output."""
    return {
        "vault_version": 1,
        "name": "test-vault",
        "scope_convention": "flat",
        "participants": [
            {"name": "alpha", "type": "spoke", "default_scope": "global"},
            {"name": "beta", "type": "spoke", "default_scope": "global"},
        ],
        "access": {
            "alpha": {"read": ["*"], "write": ["research", "notes"]},
            "beta": {"read": ["*"], "write": ["research", "notes"]},
        },
    }


class TestGrantWrite:
    def test_adds_scope(self):
        data = _seed_data()
        changed = hub_admin.grant_write(data, "alpha", "ops")
        assert changed is True
        assert "ops" in data["access"]["alpha"]["write"]

    def test_idempotent_returns_false(self):
        data = _seed_data()
        changed = hub_admin.grant_write(data, "alpha", "research")
        assert changed is False
        assert data["access"]["alpha"]["write"].count("research") == 1

    def test_refuses_wildcard(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="refusing to grant"):
            hub_admin.grant_write(data, "alpha", "*")

    def test_unknown_participant(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.grant_write(data, "ghost", "ops")

    def test_invalid_scope_syntax(self):
        data = _seed_data()
        with pytest.raises(Exception):  # ACLError from _validate_scope
            hub_admin.grant_write(data, "alpha", "ops/")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'schist.hub_admin'`

- [ ] **Step 3: Create `hub_admin.py` with module header + `grant_write`**

```python
# cli/schist/hub_admin.py
"""Hub-side ACL administration: edit a bare hub's vault.yaml in place.

Admin authority is filesystem access to the bare repo — see
docs/superpowers/specs/2026-05-28-hub-admin-cli-design.md. Mutations are
committed directly via git plumbing (no receive-pack), so the pre-receive hook
never fires. '*' write grants are refused so no participant ever holds the
ACL-edit power that '*' implies on the hub.
"""

from __future__ import annotations

import copy
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from schist.acl import NAME_RE, ACLError, _validate_scope, parse_vault_data


class HubAdminError(Exception):
    """Raised when a hub admin operation cannot be completed."""


def _participant_index(data: dict, name: str):
    """Return the index of participant `name` in data['participants'], or None.

    Participant entries may be strings or {'name': ...} mappings (acl.py accepts
    both), so handle each shape.
    """
    for i, p in enumerate(data.get("participants", []) or []):
        pname = p.get("name") if isinstance(p, dict) else p
        if pname == name:
            return i
    return None


def grant_write(data: dict, participant: str, scope: str) -> bool:
    """Add `scope` to participant's write list. Returns True if changed.

    Refuses '*' (it also authorizes editing vault.yaml over SSH). Raises
    HubAdminError on unknown participant; ACLError on invalid scope syntax.
    """
    if scope == "*":
        raise HubAdminError(
            "refusing to grant '*' write: it also authorizes editing vault.yaml "
            "over SSH (privilege escalation). Administer ACLs from the hub host "
            "and grant concrete directories, e.g. --write research."
        )
    _validate_scope(scope)  # raises ACLError on bad syntax
    if _participant_index(data, participant) is None:
        raise HubAdminError(
            f"unknown participant '{participant}'. Add it first with "
            f"`schist hub participant add {participant}`."
        )
    entry = data.setdefault("access", {}).setdefault(
        participant, {"read": ["*"], "write": []}
    )
    write = entry.setdefault("write", [])
    if scope in write:
        return False
    write.append(scope)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): grant_write mutation + module scaffold (#154)"
```

---

## Task 2: `revoke_write`

**Files:**
- Modify: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestRevokeWrite:
    def test_removes_scope(self):
        data = _seed_data()
        changed = hub_admin.revoke_write(data, "alpha", "notes")
        assert changed is True
        assert "notes" not in data["access"]["alpha"]["write"]

    def test_absent_scope_returns_false(self):
        data = _seed_data()
        changed = hub_admin.revoke_write(data, "alpha", "ops")
        assert changed is False

    def test_allows_emptying_write_list(self):
        data = _seed_data()
        hub_admin.revoke_write(data, "alpha", "research")
        hub_admin.revoke_write(data, "alpha", "notes")
        assert data["access"]["alpha"]["write"] == []

    def test_unknown_participant(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.revoke_write(data, "ghost", "notes")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestRevokeWrite -v`
Expected: FAIL — `AttributeError: module 'schist.hub_admin' has no attribute 'revoke_write'`

- [ ] **Step 3: Add `revoke_write`**

```python
def revoke_write(data: dict, participant: str, scope: str) -> bool:
    """Remove `scope` from participant's write list. Returns True if changed.

    Idempotent: returns False if the scope was not present. An empty write list
    is valid (a read-only participant).
    """
    entry = data.get("access", {}).get(participant)
    if entry is None:
        raise HubAdminError(f"unknown participant '{participant}'")
    write = entry.get("write", [])
    if scope not in write:
        return False
    write.remove(scope)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestRevokeWrite -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): revoke_write mutation (#154)"
```

---

## Task 3: `participant_add`

**Files:**
- Modify: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestParticipantAdd:
    def test_adds_participant_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_add(data, "gamma", write=["ops"])
        assert changed is True
        assert hub_admin._participant_index(data, "gamma") is not None
        assert data["access"]["gamma"] == {"read": ["*"], "write": ["ops"]}

    def test_default_write_is_empty(self):
        data = _seed_data()
        hub_admin.participant_add(data, "gamma")
        assert data["access"]["gamma"]["write"] == []
        assert data["access"]["gamma"]["read"] == ["*"]

    def test_rejects_existing_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="already exists"):
            hub_admin.participant_add(data, "alpha")

    def test_rejects_invalid_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="invalid participant name"):
            hub_admin.participant_add(data, "Bad_Name")

    def test_refuses_wildcard_write(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="refusing"):
            hub_admin.participant_add(data, "gamma", write=["*"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestParticipantAdd -v`
Expected: FAIL — `AttributeError: ... 'participant_add'`

- [ ] **Step 3: Add `participant_add`**

```python
def participant_add(
    data: dict,
    name: str,
    *,
    ptype: str = "spoke",
    write: list[str] | None = None,
    read: list[str] | None = None,
) -> bool:
    """Append a participant entry + create its access entry. Returns True.

    Validates the name against acl.NAME_RE, refuses duplicate names, and refuses
    '*' in the write list (read defaults to ['*'], which is the seeded default).
    """
    if not NAME_RE.match(name):
        raise HubAdminError(
            f"invalid participant name '{name}': must match {NAME_RE.pattern}"
        )
    if _participant_index(data, name) is not None:
        raise HubAdminError(f"participant '{name}' already exists")

    write = list(write or [])
    for s in write:
        if s == "*":
            raise HubAdminError(
                "refusing '*' write: it also authorizes editing vault.yaml over "
                "SSH. Grant concrete directories instead."
            )
        _validate_scope(s)

    read = list(read or ["*"])
    for s in read:
        if s != "*":
            _validate_scope(s)

    data.setdefault("participants", []).append(
        {"name": name, "type": ptype, "default_scope": "global"}
    )
    data.setdefault("access", {})[name] = {"read": read, "write": write}
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestParticipantAdd -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): participant_add mutation (#154)"
```

---

## Task 4: `participant_rename` + `participant_remove`

**Files:**
- Modify: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestParticipantRename:
    def test_rekeys_entry_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_rename(data, "alpha", "alpha-laptop")
        assert changed is True
        assert hub_admin._participant_index(data, "alpha") is None
        assert hub_admin._participant_index(data, "alpha-laptop") is not None
        assert "alpha" not in data["access"]
        assert data["access"]["alpha-laptop"]["write"] == ["research", "notes"]

    def test_unknown_old(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.participant_rename(data, "ghost", "new")

    def test_new_already_exists(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="already exists"):
            hub_admin.participant_rename(data, "alpha", "beta")

    def test_rejects_invalid_new_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="invalid participant name"):
            hub_admin.participant_rename(data, "alpha", "Bad_Name")


class TestParticipantRemove:
    def test_drops_entry_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_remove(data, "beta")
        assert changed is True
        assert hub_admin._participant_index(data, "beta") is None
        assert "beta" not in data["access"]

    def test_unknown_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.participant_remove(data, "ghost")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestParticipantRename tests/test_hub_admin.py::TestParticipantRemove -v`
Expected: FAIL — `AttributeError: ... 'participant_rename'`

- [ ] **Step 3: Add `participant_rename` and `participant_remove`**

```python
def participant_rename(data: dict, old: str, new: str) -> bool:
    """Rekey a participant entry + its access map key. Returns True.

    Hub-side only: the renamed spoke's local .schist/spoke.yaml and the
    source_agent in already-written notes are NOT touched (the CLI layer prints
    the operator warning). Raises on missing old / existing new / invalid new.
    """
    idx = _participant_index(data, old)
    if idx is None:
        raise HubAdminError(f"unknown participant '{old}'")
    if _participant_index(data, new) is not None:
        raise HubAdminError(f"participant '{new}' already exists")
    if not NAME_RE.match(new):
        raise HubAdminError(
            f"invalid participant name '{new}': must match {NAME_RE.pattern}"
        )

    entry = data["participants"][idx]
    if isinstance(entry, dict):
        entry["name"] = new
    else:
        data["participants"][idx] = new

    access = data.setdefault("access", {})
    if old in access:
        access[new] = access.pop(old)
    return True


def participant_remove(data: dict, name: str) -> bool:
    """Drop a participant entry + its access entry. Returns True.

    Their already-written notes remain (append-only); only their ability to push
    is removed. Raises on unknown name.
    """
    idx = _participant_index(data, name)
    if idx is None:
        raise HubAdminError(f"unknown participant '{name}'")
    data["participants"].pop(idx)
    data.get("access", {}).pop(name, None)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestParticipantRename tests/test_hub_admin.py::TestParticipantRemove -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): participant rename + remove mutations (#154)"
```

---

## Task 5: I/O layer — `read_hub_vault`, `commit_vault_yaml`, `apply_mutation`

**Files:**
- Modify: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing integration tests**

```python
import shutil
import subprocess
from types import SimpleNamespace


def _has_git():
    return shutil.which("git") is not None


needs_git = pytest.mark.skipif(not _has_git(), reason="git not available")


def _make_hub(tmp_path):
    """Build a real bare hub via init_hub and return its path."""
    from schist.sync import init_hub
    hub = tmp_path / "hub.git"
    init_hub(SimpleNamespace(name="test-vault", participant=["alpha", "beta"]), str(hub))
    return hub


def _hub_vault_text(hub):
    return subprocess.run(
        ["git", "--git-dir", str(hub), "show", "HEAD:vault.yaml"],
        capture_output=True, text=True, check=True,
    ).stdout


def _hub_head(hub):
    return subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@needs_git
class TestApplyMutation:
    def test_commits_change_and_roundtrips(self, tmp_path):
        from schist.acl import parse_vault_data
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)

        committed = hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "ops"), "hub: grant alpha write:ops"
        )
        assert committed is True
        assert _hub_head(hub) != before  # advanced

        acl = parse_vault_data(yaml.safe_load(_hub_vault_text(hub)))
        assert acl.can_write("alpha", "ops") is True

    def test_noop_does_not_commit(self, tmp_path):
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)
        committed = hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "research"), "hub: noop"
        )
        assert committed is False
        assert _hub_head(hub) == before  # unchanged

    def test_cas_aborts_on_stale_old_sha(self, tmp_path):
        hub = _make_hub(tmp_path)
        stale = _hub_head(hub)
        # Advance the ref so `stale` is no longer HEAD.
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "ops"), "advance"
        )
        # `stale` is a real (valid) commit, so commit-tree -p succeeds; the
        # update-ref compare-and-swap is what must fail because the ref moved.
        _, text = hub_admin.read_hub_vault(hub)
        with pytest.raises(HubAdminError, match="hub changed"):
            hub_admin.commit_vault_yaml(hub, text, "msg", expected_old_sha=stale)

    def test_invalid_result_is_rejected(self, tmp_path):
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)

        def break_it(d):
            d["participants"] = []  # acl.py rejects empty participants
            return True

        with pytest.raises(HubAdminError, match="invalid vault.yaml"):
            hub_admin.apply_mutation(hub, break_it, "hub: break")
        assert _hub_head(hub) == before  # nothing committed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestApplyMutation -v`
Expected: FAIL — `AttributeError: ... 'apply_mutation'`

- [ ] **Step 3: Add the I/O layer**

```python
_DEFAULT_AUTHOR = {
    "GIT_AUTHOR_NAME": "schist",
    "GIT_AUTHOR_EMAIL": "schist@local",
    "GIT_COMMITTER_NAME": "schist",
    "GIT_COMMITTER_EMAIL": "schist@local",
}


def _git(hub_path: Path, *args: str, input_text: str | None = None,
         env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git plumbing command against the bare repo at hub_path."""
    cmd = ["git", "--git-dir", str(hub_path), *args]
    full_env = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True, env=full_env)
    if check and r.returncode != 0:
        raise HubAdminError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def read_hub_vault(hub_path: Path) -> tuple[str, str]:
    """Return (HEAD commit sha, vault.yaml text) from the bare repo."""
    hub_path = Path(hub_path)
    if not (hub_path / "objects").is_dir():
        raise HubAdminError(f"not a git repository: {hub_path}")
    sha = _git(hub_path, "rev-parse", "HEAD").stdout.strip()
    text = _git(hub_path, "show", "HEAD:vault.yaml").stdout
    return sha, text


def commit_vault_yaml(hub_path: Path, new_text: str, message: str,
                      expected_old_sha: str) -> str:
    """Commit `new_text` as vault.yaml directly into the bare repo.

    Builds a new tree from HEAD's tree via a throwaway index, then advances the
    branch ref with a compare-and-swap (update-ref <new> <expected_old_sha>).
    Never invokes receive-pack, so pre-receive does not run. Raises HubAdminError
    if the ref moved since expected_old_sha was read.
    """
    hub_path = Path(hub_path)
    branch = _git(hub_path, "symbolic-ref", "--short", "HEAD").stdout.strip()
    blob = _git(hub_path, "hash-object", "-w", "--stdin", input_text=new_text).stdout.strip()

    with tempfile.NamedTemporaryFile(prefix="schist-hub-idx-") as idxf:
        idx_env = {"GIT_INDEX_FILE": idxf.name}
        _git(hub_path, "read-tree", "HEAD", env=idx_env)
        _git(hub_path, "update-index", "--add", "--cacheinfo",
             f"100644,{blob},vault.yaml", env=idx_env)
        tree = _git(hub_path, "write-tree", env=idx_env).stdout.strip()

    author_env = {k: v for k, v in _DEFAULT_AUTHOR.items() if k not in os.environ}
    commit = _git(hub_path, "commit-tree", tree, "-p", expected_old_sha,
                  "-m", message, env=author_env).stdout.strip()

    cas = _git(hub_path, "update-ref", f"refs/heads/{branch}", commit,
               expected_old_sha, check=False)
    if cas.returncode != 0:
        raise HubAdminError(
            "hub changed since vault.yaml was read (ref compare-and-swap failed); "
            "re-run the command to retry."
        )
    return commit


def apply_mutation(hub_path, mutate, message: str) -> bool:
    """Read HEAD vault.yaml, apply mutate(data)->bool, validate, commit if changed.

    `mutate` is one of the pure mutation functions, applied to the parsed dict.
    Returns True if a commit was made, False if mutate was a no-op. Fail-closed:
    a mutated dict that fails strict parse_vault_data() validation aborts before
    any write.
    """
    hub_path = Path(hub_path)
    old_sha, text = read_hub_vault(hub_path)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise HubAdminError("hub vault.yaml is not a YAML mapping")

    changed = mutate(data)
    if not changed:
        return False

    try:
        parse_vault_data(copy.deepcopy(data))
    except ACLError as e:
        raise HubAdminError(f"refusing to commit invalid vault.yaml: {e}")

    new_text = yaml.dump(data, default_flow_style=False, sort_keys=False)
    commit_vault_yaml(hub_path, new_text, message, old_sha)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestApplyMutation -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): plumbing commit layer with CAS + fail-closed validation (#154)"
```

---

## Task 6: CLI entry functions (`cmd_*`)

**Files:**
- Modify: `cli/schist/hub_admin.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing tests**

```python
@needs_git
class TestCmdFunctions:
    def test_cmd_grant_then_push_accepted_by_pre_receive(self, tmp_path, capsys):
        from schist.acl import parse_vault_data
        from schist.pre_receive import check_push
        hub = _make_hub(tmp_path)

        # Before: alpha has no write on ops -> a write there would violate.
        # check_push(identity, changed_files, acl, refname) -> list[Violation]
        acl_before = parse_vault_data(yaml.safe_load(_hub_vault_text(hub)))
        v_before = check_push("alpha", ["ops/2026-05-28-x.md"], acl_before, "refs/heads/main")
        assert len(v_before) == 1  # rejected

        hub_admin.cmd_grant(SimpleNamespace(participant="alpha", write="ops", hub_path=str(hub)))
        assert "Granted" in capsys.readouterr().out

        # After: the same write is now in-scope (would be accepted).
        acl_after = parse_vault_data(yaml.safe_load(_hub_vault_text(hub)))
        v_after = check_push("alpha", ["ops/2026-05-28-x.md"], acl_after, "refs/heads/main")
        assert v_after == []  # accepted

    def test_cmd_grant_idempotent_notice(self, tmp_path, capsys):
        hub = _make_hub(tmp_path)
        hub_admin.cmd_grant(SimpleNamespace(participant="alpha", write="research", hub_path=str(hub)))
        assert "no change" in capsys.readouterr().out.lower()

    def test_cmd_participant_rename_prints_warning(self, tmp_path, capsys):
        hub = _make_hub(tmp_path)
        hub_admin.cmd_participant_rename(
            SimpleNamespace(old="alpha", new="alpha-laptop", hub_path=str(hub))
        )
        out = capsys.readouterr().out
        assert "ACTION REQUIRED" in out
        assert "spoke.yaml" in out
        assert "source_agent: alpha" in out

    def test_cmd_participant_remove_requires_yes(self, tmp_path):
        hub = _make_hub(tmp_path)
        with pytest.raises(HubAdminError, match="--yes"):
            hub_admin.cmd_participant_remove(
                SimpleNamespace(name="beta", hub_path=str(hub), yes=False)
            )
```

Note: this task uses `schist.pre_receive.check_push(identity, changed_files,
acl, refname) -> list[Violation]` (defined at `pre_receive.py:103`) to assert
the grant actually changes the push verdict — verified signature.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestCmdFunctions -v`
Expected: FAIL — `AttributeError: ... 'cmd_grant'`

- [ ] **Step 3: Add the `cmd_*` entry functions**

```python
def cmd_grant(args) -> None:
    p, scope, hub = args.participant, args.write, args.hub_path
    changed = apply_mutation(
        hub, lambda d: grant_write(d, p, scope), f"hub: grant {p} write:{scope}"
    )
    if changed:
        print(f"Granted write:{scope} to {p}.")
    else:
        print(f"{p} already has write:{scope}; no change.")


def cmd_revoke(args) -> None:
    p, scope, hub = args.participant, args.write, args.hub_path
    changed = apply_mutation(
        hub, lambda d: revoke_write(d, p, scope), f"hub: revoke {p} write:{scope}"
    )
    if changed:
        print(f"Revoked write:{scope} from {p}.")
    else:
        print(f"{p} did not have write:{scope}; no change.")


def cmd_participant_add(args) -> None:
    name, hub = args.name, args.hub_path
    apply_mutation(
        hub,
        lambda d: participant_add(d, name, ptype=args.type,
                                  write=args.write, read=args.read),
        f"hub: add participant {name}",
    )
    print(f"Added participant {name} (type={args.type}, write={args.write or []}).")


def cmd_participant_rename(args) -> None:
    old, new, hub = args.old, args.new, args.hub_path
    apply_mutation(
        hub, lambda d: participant_rename(d, old, new),
        f"hub: rename participant {old} -> {new}",
    )
    print(f"Renamed {old} -> {new} in vault.yaml.\n")
    print(f"⚠  ACTION REQUIRED on spoke '{old}':")
    print(f"   update .schist/spoke.yaml identity to '{new}', or its pushes")
    print(f"   will be rejected. Existing notes keep source_agent: {old}")
    print(f"   (history is append-only).")


def cmd_participant_remove(args) -> None:
    name, hub = args.name, args.hub_path
    if not getattr(args, "yes", False):
        raise HubAdminError(
            f"removing participant '{name}' is irreversible (their notes remain "
            f"but they lose push access). Re-run with --yes to confirm."
        )
    apply_mutation(
        hub, lambda d: participant_remove(d, name),
        f"hub: remove participant {name}",
    )
    print(f"Removed participant {name}.")
```

For `cmd_participant_add`, argparse will supply `args.write`/`args.read` as
lists (nargs='*') or `None`; `participant_add` already normalizes `None` to a
default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestCmdFunctions -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/hub_admin.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): CLI entry functions for grant/revoke/participant (#154)"
```

---

## Task 7: Wire `hub` subparser into `__main__.py`

**Files:**
- Modify: `cli/schist/__main__.py`
- Test: `cli/tests/test_hub_admin.py`

- [ ] **Step 1: Write the failing test (subprocess smoke test)**

```python
@needs_git
class TestHubCLIDispatch:
    def test_grant_via_module_invocation(self, tmp_path):
        from schist.acl import parse_vault_data
        hub = _make_hub(tmp_path)
        r = subprocess.run(
            ["python", "-m", "schist", "hub", "grant", "alpha",
             "--write", "ops", "--hub-path", str(hub)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        assert "Granted" in r.stdout
        acl = parse_vault_data(yaml.safe_load(_hub_vault_text(hub)))
        assert acl.can_write("alpha", "ops") is True

    def test_grant_wildcard_rejected_via_cli(self, tmp_path):
        hub = _make_hub(tmp_path)
        r = subprocess.run(
            ["python", "-m", "schist", "hub", "grant", "alpha",
             "--write", "*", "--hub-path", str(hub)],
            capture_output=True, text=True,
        )
        assert r.returncode != 0
        assert "refusing to grant" in r.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestHubCLIDispatch -v`
Expected: FAIL — `invalid choice: 'hub'` (argparse) → returncode != 0 but for the wrong reason; the grant test fails on `"Granted" not in r.stdout`.

- [ ] **Step 3: Add the `hub` subparser**

In `cli/schist/__main__.py`, after the `hooks` subparser block (around line 96, before `args = parser.parse_args()`), add:

```python
    # hub: filesystem-side ACL administration of a bare hub repo
    p_hub = sub.add_parser('hub', help='Administer a bare hub vault.yaml (filesystem)')
    hub_sub = p_hub.add_subparsers(dest='hub_action')

    p_hub_grant = hub_sub.add_parser('grant', help='Grant a participant write on a directory')
    p_hub_grant.add_argument('participant', help='Participant name')
    p_hub_grant.add_argument('--write', required=True, help='Directory scope to grant')
    p_hub_grant.add_argument('--hub-path', dest='hub_path', required=True,
                             help='Path to the bare hub repo')

    p_hub_revoke = hub_sub.add_parser('revoke', help='Revoke a participant write on a directory')
    p_hub_revoke.add_argument('participant', help='Participant name')
    p_hub_revoke.add_argument('--write', required=True, help='Directory scope to revoke')
    p_hub_revoke.add_argument('--hub-path', dest='hub_path', required=True,
                              help='Path to the bare hub repo')

    p_hub_part = hub_sub.add_parser('participant', help='Manage participants')
    part_sub = p_hub_part.add_subparsers(dest='participant_action')

    p_part_add = part_sub.add_parser('add', help='Add a participant')
    p_part_add.add_argument('name', help='New participant name')
    p_part_add.add_argument('--type', default='spoke', help='Participant type (default: spoke)')
    p_part_add.add_argument('--write', nargs='*', default=None, help='Write scopes to grant')
    p_part_add.add_argument('--read', nargs='*', default=None, help="Read scopes (default: ['*'])")
    p_part_add.add_argument('--hub-path', dest='hub_path', required=True,
                            help='Path to the bare hub repo')

    p_part_rename = part_sub.add_parser('rename', help='Rename a participant')
    p_part_rename.add_argument('old', help='Current participant name')
    p_part_rename.add_argument('new', help='New participant name')
    p_part_rename.add_argument('--hub-path', dest='hub_path', required=True,
                               help='Path to the bare hub repo')

    p_part_remove = part_sub.add_parser('remove', help='Remove a participant')
    p_part_remove.add_argument('name', help='Participant name')
    p_part_remove.add_argument('--yes', action='store_true', help='Confirm removal')
    p_part_remove.add_argument('--hub-path', dest='hub_path', required=True,
                               help='Path to the bare hub repo')
```

Then add the early dispatch block. Place it right after the `doctor` early-dispatch block (after line 120, before `vault_path = args.vault`):

```python
    # hub: filesystem ACL admin — needs --hub-path, not --vault
    if args.command == 'hub':
        from schist import hub_admin
        try:
            if args.hub_action == 'grant':
                hub_admin.cmd_grant(args)
            elif args.hub_action == 'revoke':
                hub_admin.cmd_revoke(args)
            elif args.hub_action == 'participant':
                if args.participant_action == 'add':
                    hub_admin.cmd_participant_add(args)
                elif args.participant_action == 'rename':
                    hub_admin.cmd_participant_rename(args)
                elif args.participant_action == 'remove':
                    hub_admin.cmd_participant_remove(args)
                else:
                    print('Usage: schist hub participant {add|rename|remove}', file=sys.stderr)
                    sys.exit(1)
            else:
                print('Usage: schist hub {grant|revoke|participant}', file=sys.stderr)
                sys.exit(1)
        except hub_admin.HubAdminError as e:
            print(f'Error: {e}', file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_hub_admin.py::TestHubCLIDispatch -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/__main__.py cli/tests/test_hub_admin.py
git commit -m "feat(hub-admin): wire hub subparser + dispatch into CLI (#154)"
```

---

## Task 8: Hub-mode drift lint in `doctor.py`

**Files:**
- Modify: `cli/schist/doctor.py`
- Test: `cli/tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Add to `cli/tests/test_doctor.py`:

```python
class TestHubAclDrift:
    def _make_hub(self, tmp_path):
        import shutil
        if shutil.which("git") is None:
            import pytest as _pytest
            _pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha", "beta"]), str(hub))
        return hub

    def test_skip_without_hub_path(self):
        from schist.doctor import check_hub_acl_drift
        r = check_hub_acl_drift(None)
        assert r.status == "SKIP"

    def test_warns_on_dir_granted_to_nobody(self, tmp_path):
        # default.yaml has 'decisions'; seed grants only research/concepts/decisions/
        # notes/ops/papers, so revoke 'decisions' from BOTH to create signal (a).
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        hub_admin.apply_mutation(hub, lambda d: hub_admin.revoke_write(d, "alpha", "decisions"), "m")
        hub_admin.apply_mutation(hub, lambda d: hub_admin.revoke_write(d, "beta", "decisions"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "WARN"
        assert "decisions" in r.message

    def test_warns_on_cross_participant_inconsistency(self, tmp_path):
        # Grant 'ops2' to alpha only -> signal (b). Use a real dir name to pass
        # _validate_scope: grant 'logs' to alpha only (not in seed for either).
        from schist import hub_admin
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        hub_admin.apply_mutation(hub, lambda d: hub_admin.grant_write(d, "alpha", "logs"), "m")
        r = check_hub_acl_drift(str(hub))
        assert r.status == "WARN"
        assert "logs" in r.message

    def test_pass_when_consistent_and_covered(self, tmp_path):
        from schist.doctor import check_hub_acl_drift
        hub = self._make_hub(tmp_path)
        r = check_hub_acl_drift(str(hub))
        # Seed grants all 6 content dirs to both; infra dirs (logs/projects) are
        # excluded from expected set -> no drift.
        assert r.status == "PASS"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_doctor.py::TestHubAclDrift -v`
Expected: FAIL — `ImportError: cannot import name 'check_hub_acl_drift'`

- [ ] **Step 3: Add `_hub_expected_dirs` and `check_hub_acl_drift`**

In `cli/schist/doctor.py`, add after `check_spoke_acl_drift` (after line 421):

```python
def _hub_expected_dirs(hub: Path) -> list[str]:
    """Directories a hub's participants are expected to be granted.

    Prefer HEAD:schist.yaml if the hub has one; else fall back to the packaged
    default.yaml directory list MINUS infra dirs (logs/, projects/), which the
    seed deliberately does not grant (see sync.py:_build_seed_vault).
    """
    import subprocess
    import yaml

    INFRA = {"logs", "projects"}

    def _dirs_from(text: str) -> list[str]:
        d = yaml.safe_load(text) or {}
        dirs = d.get("directories") or {}
        vals = dirs.values() if isinstance(dirs, dict) else dirs
        return [str(v).rstrip("/") for v in vals]

    r = subprocess.run(
        ["git", "--git-dir", str(hub), "show", "HEAD:schist.yaml"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return _dirs_from(r.stdout)

    # Fallback: packaged default.yaml (sibling of this module), minus infra dirs.
    default_path = Path(__file__).resolve().parent / "default.yaml"
    dirs = _dirs_from(default_path.read_text())
    return [d for d in dirs if d not in INFRA]


def check_hub_acl_drift(hub_path: Optional[str]) -> CheckResult:
    """Flag ACL drift on a bare hub: schema dirs not granted (a), and dirs that
    some participants have but others lack (b).
    """
    label = "Hub ACL drift"
    if not hub_path:
        return CheckResult("SKIP", label, "no --hub-path supplied")

    hub = Path(hub_path)
    if not (hub / "objects").is_dir():
        return CheckResult("SKIP", label, f"not a git repository: {hub_path}")

    try:
        import subprocess
        import yaml
        from schist.acl import _scope_matches, parse_vault_data
        text = subprocess.run(
            ["git", "--git-dir", str(hub), "show", "HEAD:vault.yaml"],
            capture_output=True, text=True, check=True,
        ).stdout
        acl = parse_vault_data(yaml.safe_load(text))
    except Exception as e:  # noqa: BLE001 — surface as SKIP so doctor never crashes
        return CheckResult("SKIP", label, f"could not read/parse hub vault.yaml: {e}")

    names = sorted(acl.access.keys())

    # Signal (a): expected schema dir not granted to one-or-more participants.
    a_problems: list[str] = []
    for d in _hub_expected_dirs(hub):
        missing = [n for n in names if not _scope_matches(acl.access[n].write, d)]
        if missing:
            a_problems.append(f"'{d}' not granted to: {', '.join(missing)}")

    # Signal (b): a concrete dir some participants have in write but others lack.
    name_set = set(names)
    holders: dict[str, set] = {}
    for n in names:
        for s in acl.access[n].write:
            if s != "*":
                holders.setdefault(s, set()).add(n)
    b_problems: list[str] = []
    for s, who in sorted(holders.items()):
        lacking = name_set - who
        if lacking:
            b_problems.append(f"'{s}' held by {', '.join(sorted(who))} but not {', '.join(sorted(lacking))}")

    if not a_problems and not b_problems:
        return CheckResult("PASS", label, f"{len(names)} participants, no ACL drift")

    msg_parts = a_problems + b_problems
    return CheckResult(
        "WARN", label, "; ".join(msg_parts),
        fix="Grant missing scopes from the hub host, e.g. `schist hub grant <participant> --write <dir> --hub-path <hub>`.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_doctor.py::TestHubAclDrift -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/doctor.py cli/tests/test_doctor.py
git commit -m "feat(doctor): hub-mode ACL drift lint (schema-vs-grant + cross-participant) (#154)"
```

---

## Task 9: Wire `--hub-path` into the `doctor` CLI

**Files:**
- Modify: `cli/schist/__main__.py:59-61` (the `p_doctor` subparser)
- Modify: `cli/schist/doctor.py:682-731` (`run_doctor`, `doctor`)
- Test: `cli/tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

```python
class TestDoctorHubWiring:
    def test_run_doctor_includes_hub_check_when_path_given(self, tmp_path, capsys):
        import shutil
        if shutil.which("git") is None:
            import pytest as _pytest
            _pytest.skip("git not available")
        from types import SimpleNamespace
        from schist.sync import init_hub
        from schist.doctor import run_doctor
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="v", participant=["alpha"]), str(hub))

        results = run_doctor(None, None, as_json=False, hub_path=str(hub))
        labels = [r.label for r in results]
        assert "Hub ACL drift" in labels

    def test_run_doctor_omits_hub_check_without_path(self):
        from schist.doctor import run_doctor
        results = run_doctor(None, None, as_json=False)
        labels = [r.label for r in results]
        assert "Hub ACL drift" not in labels
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_doctor.py::TestDoctorHubWiring -v`
Expected: FAIL — `TypeError: run_doctor() got an unexpected keyword argument 'hub_path'`

- [ ] **Step 3a: Add `--hub-path` to the doctor subparser**

In `cli/schist/__main__.py`, in the `p_doctor` block (lines 59-61), add after the `--json` argument:

```python
    p_doctor.add_argument('--hub-path', dest='hub_path', default=None,
                          help='(hub) Path to a bare hub repo to run hub-mode checks against')
```

- [ ] **Step 3b: Thread `hub_path` through `run_doctor` and `doctor`**

In `cli/schist/doctor.py`, change the `run_doctor` signature and append the hub check conditionally:

```python
def run_doctor(vault_path: Optional[str], db_path: Optional[str],
               as_json: bool = False, hub_path: Optional[str] = None) -> list[CheckResult]:
    checks = [
        check_python(),
        check_node(),
        check_uv(),
        check_git(),
        check_vault_exists(vault_path),
        check_vault_is_git(vault_path),
        check_schist_yaml(vault_path),
        check_sqlite(vault_path, db_path),
        check_post_commit_hook(vault_path),
        check_hooks_freshness(vault_path),
        check_hooks_path(vault_path),
        check_ingest_available(vault_path),
        check_spoke(vault_path),
        check_spoke_acl_drift(vault_path),
        check_mcp_config(vault_path),
        check_mcp_schema_alignment(vault_path),
    ]
    if hub_path:
        checks.append(check_hub_acl_drift(hub_path))
    # ... (rest of the function unchanged)
```

And in `doctor(args)`:

```python
def doctor(args) -> None:
    vault_path = getattr(args, "vault", None)
    db_path = getattr(args, "db", None)
    if vault_path and not db_path:
        db_path = str(Path(vault_path) / ".schist" / "schist.db")
    as_json = getattr(args, "as_json", False)
    hub_path = getattr(args, "hub_path", None)

    results = run_doctor(vault_path, db_path, as_json, hub_path=hub_path)
    if any(r.status == "FAIL" for r in results):
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/test_doctor.py::TestDoctorHubWiring -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add cli/schist/__main__.py cli/schist/doctor.py cli/tests/test_doctor.py
git commit -m "feat(doctor): --hub-path flag runs hub-mode drift lint (#154)"
```

---

## Task 10: Full-suite regression + docs

**Files:**
- Modify: `docs/hub-spoke-setup.md`

- [ ] **Step 1: Run the full CLI test suite**

Run: `cd cli && uv run --with pytest --with . python -m pytest tests/ -q`
Expected: all PASS (no regressions). If any pre-existing test now fails, investigate before proceeding.

- [ ] **Step 2: Add the "Administering ACLs" docs section**

In `docs/hub-spoke-setup.md`, add a new top-level section (place it after the hub-setup section; match the file's existing heading style):

```markdown
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
schist hub participant add    <name> [--write <dir> ...] [--type spoke] --hub-path /srv/vault.git
schist hub participant rename <old> <new>                                --hub-path /srv/vault.git
schist hub participant remove <name> --yes                               --hub-path /srv/vault.git
```

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
```

- [ ] **Step 3: Verify docs render (no broken fences)**

Run: `python -c "import pathlib; t=pathlib.Path('docs/hub-spoke-setup.md').read_text(); assert t.count('\`\`\`') % 2 == 0, 'unbalanced code fences'"`
Expected: no assertion error.

- [ ] **Step 4: Commit**

```bash
git add docs/hub-spoke-setup.md
git commit -m "docs(hub): document schist hub admin commands + drift lint (#154)"
```

---

## Self-Review Notes (for the implementer)

- **Spec coverage:** Task 1-7 = item #1 (hub CLI). Task 8-9 = item #2 (hub-mode
  drift lint, both signals). Task 10 = item #4 (docs). Item #3 (spoke-side
  diagnostic) was already implemented in `doctor.py:check_spoke_acl_drift` — not
  re-done here.
- **`pre_receive.check_push` (Task 6):** verified — `check_push(identity,
  changed_files, acl, refname) -> list[Violation]` at `pre_receive.py:103`. Used
  read-only in a test to assert the grant flips the push verdict.
- **`init_hub` signature (Tasks 5,6,8,9):** confirmed via `test_hub.py` — it
  takes `(args, hub_path_str)` where `args` is a `SimpleNamespace(name=...,
  participant=[...])`.
- **No new TS/MCP work:** this plan is CLI/Python only. The MCP-side `vault-acl.ts`
  parity gap (#160) is explicitly out of scope (see spec "Out of scope").
```
