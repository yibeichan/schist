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
