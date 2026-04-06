"""vault.yaml ACL parser and scope resolution."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_VERSIONS = {1}
VALID_TYPES = {"agent", "spoke"}
VALID_TRANSPORTS = {"ssh-and-git", "git-only"}
VALID_SCOPE_CONVENTIONS = {"subdirectory", "flat", "multi-vault"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")

DEFAULT_RATE_LIMITS = {
    "git_syncs_per_hour": 10,
    "mcp_writes_per_hour": 100,
    "notes_per_sync": 20,
}


class ACLError(Exception):
    """Raised when vault.yaml fails validation."""


@dataclass
class Participant:
    name: str
    type: str = "agent"
    default_scope: str = "global"
    transport: str = "ssh-and-git"
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class AccessEntry:
    read: list[str]
    write: list[str]


@dataclass
class RateLimits:
    git_syncs_per_hour: int = 10
    mcp_writes_per_hour: int = 100
    notes_per_sync: int = 20


@dataclass
class VaultACL:
    name: str
    vault_version: int
    domains: list[str]
    participants: list[Participant]
    scope_convention: str
    access: dict[str, AccessEntry]
    rate_limits: dict[str, RateLimits]

    def get_participant(self, name: str) -> Participant | None:
        for p in self.participants:
            if p.name == name:
                return p
        return None

    def can_read(self, identity: str, scope: str) -> bool:
        """Check if identity can read the given scope.

        Parent scope grants child access: read:[research] -> can read research/mario/*.
        "*" matches all scopes.

        NOTE: Read intentionally uses the same parent→child inheritance as write.
        A parent read grant covers all child scopes by design.
        """
        entry = self.access.get(identity)
        if entry is None:
            return False
        return _scope_matches(entry.read, scope)

    def can_write(self, identity: str, scope: str) -> bool:
        """Check if identity can write to the given scope.

        Parent scope grants child access: write:[research] -> can write research/mario/*.
        "*" matches all scopes.
        """
        entry = self.access.get(identity)
        if entry is None:
            return False
        return _scope_matches(entry.write, scope)


def _scope_matches(allowed: list[str], target: str) -> bool:
    """Check if target scope is covered by any scope in the allowed list.

    Rules:
    - "*" matches everything
    - Exact match: "research" matches "research"
    - Parent grants child: "research" matches "research/mario"
    """
    for scope in allowed:
        if scope == "*":
            return True
        if target == scope:
            return True
        # Parent grants child: scope "research" covers "research/mario"
        if target.startswith(scope + "/"):
            return True
    return False


def _validate_scope(scope: str) -> None:
    """Validate a single scope string."""
    if scope == "*":
        return
    # No partial wildcards
    if "*" in scope:
        raise ACLError(
            f"Partial wildcards not allowed: '{scope}'. Use '*' for all scopes."
        )
    # Must be valid path segments
    if not scope or scope.startswith("/") or scope.endswith("/"):
        raise ACLError(f"Invalid scope syntax: '{scope}'")
    # Reject path traversal
    if ".." in scope.split("/"):
        raise ACLError(f"Path traversal not allowed in scope: '{scope}'")
    # Reject double slashes
    if "//" in scope:
        raise ACLError(f"Invalid scope syntax: '{scope}'")


def parse_vault_yaml(path: str | Path) -> VaultACL:
    """Parse and validate a vault.yaml file, returning a VaultACL."""
    path = Path(path)
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ACLError("vault.yaml must be a YAML mapping")

    return parse_vault_data(data)


def parse_vault_data(data: dict[str, Any]) -> VaultACL:
    """Parse and validate vault.yaml data from a dict."""
    if not isinstance(data, dict):
        raise ACLError("vault.yaml must be a YAML mapping")

    errors: list[str] = []
    is_v0 = False

    # --- vault_version ---
    version = data.get("vault_version")
    if version is None:
        is_v0 = True
        version = 0
        warnings.warn(
            "vault.yaml has no vault_version — consider upgrading to v1",
            UserWarning,
            stacklevel=2,
        )
    elif not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
        raise ACLError(
            f"Unsupported vault_version: {version!r}. Supported: {sorted(SUPPORTED_VERSIONS)}"
        )

    # --- name ---
    name = data.get("name")
    if not name or not isinstance(name, str):
        errors.append("'name' is required and must be a string")

    # --- scope_convention ---
    scope_convention = data.get("scope_convention")
    if not is_v0:
        if scope_convention not in VALID_SCOPE_CONVENTIONS:
            errors.append(
                f"'scope_convention' must be one of {sorted(VALID_SCOPE_CONVENTIONS)}, got {scope_convention!r}"
            )
    else:
        scope_convention = scope_convention or "subdirectory"

    # --- participants ---
    raw_participants = data.get("participants")
    if not raw_participants or not isinstance(raw_participants, list):
        raise ACLError("'participants' must be a non-empty list")

    participants: list[Participant] = []
    seen_names: set[str] = set()
    for i, p in enumerate(raw_participants):
        if isinstance(p, str):
            p = {"name": p}
        elif not isinstance(p, dict):
            errors.append(f"participants[{i}]: must be a string or mapping")
            continue

        pname = p.get("name")
        if not pname or not isinstance(pname, str):
            errors.append(f"participants[{i}]: 'name' is required")
            continue

        if not NAME_RE.match(pname):
            errors.append(
                f"participants[{i}]: name '{pname}' must match ^[a-z][a-z0-9-]*$"
            )

        if pname in seen_names:
            errors.append(f"participants[{i}]: duplicate name '{pname}'")
        seen_names.add(pname)

        ptype = p.get("type", "agent")
        if ptype not in VALID_TYPES:
            errors.append(
                f"participant '{pname}': type must be one of {sorted(VALID_TYPES)}, got '{ptype}'"
            )

        transport = p.get("transport", "ssh-and-git")
        if transport not in VALID_TRANSPORTS:
            errors.append(
                f"participant '{pname}': transport must be one of {sorted(VALID_TRANSPORTS)}, got '{transport}'"
            )

        default_scope = p.get("default_scope", "global")
        if default_scope != "global":
            try:
                _validate_scope(default_scope)
            except ACLError as e:
                errors.append(f"participant '{pname}': invalid default_scope: {e}")

        metadata = p.get("metadata") or {}
        if not isinstance(metadata, dict):
            errors.append(
                f"participant '{pname}': metadata must be a mapping, got {type(metadata).__name__}"
            )
            metadata = {}

        validated_metadata: dict[str, str] = {}
        for mk, mv in metadata.items():
            if not isinstance(mk, str):
                errors.append(
                    f"participant '{pname}': metadata key must be a string, got {type(mk).__name__}"
                )
                continue
            if not isinstance(mv, str):
                errors.append(
                    f"participant '{pname}': metadata value for '{mk}' must be a string, got {type(mv).__name__}"
                )
                continue
            validated_metadata[mk] = mv

        participants.append(Participant(
            name=pname,
            type=ptype,
            default_scope=default_scope,
            transport=transport,
            metadata=validated_metadata,
        ))

    # --- access ---
    raw_access = data.get("access")
    if not raw_access or not isinstance(raw_access, dict):
        raise ACLError("'access' must be a non-empty mapping")

    access: dict[str, AccessEntry] = {}
    participant_names = {p.name for p in participants}

    # Every participant must have an access entry
    for pname in participant_names:
        if pname not in raw_access:
            errors.append(f"participant '{pname}' has no access entry")

    # Every access key must have a corresponding participant
    for aname in raw_access:
        if aname not in participant_names:
            errors.append(f"access key '{aname}' has no corresponding participant")

    for aname, aval in raw_access.items():
        if not isinstance(aval, dict):
            errors.append(f"access.{aname}: must be a mapping")
            continue

        read = aval.get("read")
        write = aval.get("write")

        if not read or not isinstance(read, list):
            errors.append(f"access.{aname}.read must be a non-empty list")
            read = []
        if not write or not isinstance(write, list):
            errors.append(f"access.{aname}.write must be a non-empty list")
            write = []

        # Validate each scope
        for scope in read + write:
            if not isinstance(scope, str):
                errors.append(f"access.{aname}: scope must be a string, got {type(scope).__name__}")
                continue
            try:
                _validate_scope(scope)
            except ACLError as e:
                errors.append(f"access.{aname}: {e}")

        access[aname] = AccessEntry(
            read=[str(s) for s in read],
            write=[str(s) for s in write],
        )

    # --- domains ---
    domains = data.get("domains") or []
    if not isinstance(domains, list):
        errors.append("'domains' must be a list")
        domains = []
    else:
        for i, d in enumerate(domains):
            if not isinstance(d, str):
                errors.append(f"domains[{i}]: must be a string, got {type(d).__name__}")

    # --- rate_limits ---
    raw_limits = data.get("rate_limits") or {}
    rate_limits: dict[str, RateLimits] = {}
    if isinstance(raw_limits, dict):
        for lname in raw_limits:
            if lname not in participant_names:
                errors.append(f"rate_limits key '{lname}' has no corresponding participant")
        for lname, lval in raw_limits.items():
            if not isinstance(lval, dict):
                errors.append(f"rate_limits.{lname}: must be a mapping")
                continue
            for key in ("git_syncs_per_hour", "mcp_writes_per_hour", "notes_per_sync"):
                val = lval.get(key)
                if val is not None:
                    if not isinstance(val, int) or isinstance(val, bool) or val < 1:
                        errors.append(
                            f"rate_limits.{lname}.{key}: must be a positive integer, got {val!r}"
                        )
            rate_limits[lname] = RateLimits(
                git_syncs_per_hour=lval.get("git_syncs_per_hour", DEFAULT_RATE_LIMITS["git_syncs_per_hour"]),
                mcp_writes_per_hour=lval.get("mcp_writes_per_hour", DEFAULT_RATE_LIMITS["mcp_writes_per_hour"]),
                notes_per_sync=lval.get("notes_per_sync", DEFAULT_RATE_LIMITS["notes_per_sync"]),
            )

    if errors:
        raise ACLError("vault.yaml validation failed:\n  - " + "\n  - ".join(errors))

    return VaultACL(
        name=name or "",
        vault_version=version,
        domains=domains,
        participants=participants,
        scope_convention=scope_convention,
        access=access,
        rate_limits=rate_limits,
    )
