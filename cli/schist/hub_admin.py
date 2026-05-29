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


# ---------------------------------------------------------------------------
# Git plumbing I/O layer
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CLI entry functions (thin wrappers over apply_mutation + pure mutations)
# ---------------------------------------------------------------------------

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
