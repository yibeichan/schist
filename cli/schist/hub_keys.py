"""authorized_keys management: pin hub SSH keys to schist identities.

Each spoke key gets a forced-command entry so the SSH credential — not a
client-sent environment variable — determines the push identity (issue #502):

    restrict,command="schist-shell <participant>" ssh-ed25519 AAAA... comment

`restrict` (OpenSSH 7.2+) disables pty/forwarding/agent/X11 in one token and
fails closed on capabilities added to future OpenSSH releases.

The pure functions here operate on authorized_keys text; the cmd_* wrappers
at the bottom do file I/O (atomic replace, 0600) and CLI output.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from schist.acl import NAME_RE, ACLError, parse_vault_data
from schist.hub_admin import HubAdminError, read_hub_vault

# Matches the pinned forced command, tolerating an absolute path to the
# wrapper (command="/usr/local/bin/schist-shell pi") and an optional baked
# repo-confinement argument (command="schist-shell pi '/srv/git/vault.git'").
_PINNED_COMMAND_RE = re.compile(
    r'command="(?:[^"]*/)?schist-shell ([a-z][a-z0-9-]*)( [^"]+)?"'
)

# authorized_keys key types (OpenSSH sshkey.c): ssh-*, ecdsa-*, and the
# FIDO sk-* variants. Used to decide whether a line's first field is an
# options string or already the key type.
_KEYTYPE_RE = re.compile(r"^(ssh-[a-z0-9-]+|ecdsa-[a-z0-9-]+@?[a-z0-9.-]*|sk-[a-z0-9-]+@[a-z0-9.-]+)$")


@dataclass
class KeyEntry:
    """One non-comment line of an authorized_keys file."""

    options: str | None  # raw options field, None if absent
    keytype: str
    blob: str  # base64 key material
    comment: str  # may be ""
    line_no: int  # 1-based position in the file

    @property
    def pinned_identity(self) -> str | None:
        """Participant name from a schist-shell forced command, if pinned."""
        if not self.options:
            return None
        m = _PINNED_COMMAND_RE.search(self.options)
        return m.group(1) if m else None

    @property
    def fingerprint(self) -> str:
        """OpenSSH-style SHA256 fingerprint (no trailing '=' padding)."""
        try:
            raw = base64.b64decode(self.blob, validate=True)
        except (binascii.Error, ValueError):
            return "(invalid base64)"
        digest = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        return "SHA256:" + digest.rstrip("=")

    def render(self) -> str:
        parts = [self.options] if self.options else []
        parts += [self.keytype, self.blob]
        if self.comment:
            parts.append(self.comment)
        return " ".join(parts)


def _split_options(line: str) -> tuple[str, str]:
    """Split a leading options field from the rest of the line.

    Options may contain commas inside double quotes and backslash-escaped
    quotes (sshd's auth-options.c rules), so a naive .split() misparses
    command="schist-shell pi",restrict entries. Returns (options, rest).
    """
    in_quote = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and in_quote and i + 1 < len(line):
            i += 2
            continue
        if c == '"':
            in_quote = not in_quote
        elif c in (" ", "\t") and not in_quote:
            return line[:i], line[i:].lstrip()
        i += 1
    return line, ""


def parse_authorized_keys(text: str) -> list[KeyEntry]:
    """Parse authorized_keys text into KeyEntry rows, skipping comments/blanks.

    Unparseable lines are skipped (sshd would skip them too); callers that
    care about them should diff rendered output against the original.
    """
    entries: list[KeyEntry] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        first = line.split(None, 1)[0]
        if _KEYTYPE_RE.match(first):
            options: str | None = None
            rest = line
        else:
            options, rest = _split_options(line)
            if not rest:
                continue

        fields = rest.split(None, 2)
        if len(fields) < 2 or not _KEYTYPE_RE.match(fields[0]):
            continue
        keytype, blob = fields[0], fields[1]
        comment = fields[2] if len(fields) > 2 else ""
        entries.append(KeyEntry(options, keytype, blob, comment, line_no))
    return entries


def parse_public_key(pubkey: str) -> tuple[str, str, str]:
    """Extract (keytype, blob, comment) from a public key line.

    Accepts a bare `<keytype> <blob> [comment]` line or a full
    authorized_keys line (any existing options are dropped — pinning
    replaces them). Raises HubAdminError if no key is recognizable.
    """
    parsed = parse_authorized_keys(pubkey.strip())
    if len(parsed) != 1:
        raise HubAdminError(
            "could not parse a public key from the input — expected one "
            "'<keytype> <base64> [comment]' line (as in id_ed25519.pub)"
        )
    entry = parsed[0]
    try:
        base64.b64decode(entry.blob, validate=True)
    except (binascii.Error, ValueError):
        raise HubAdminError("public key base64 blob failed to decode")
    return entry.keytype, entry.blob, entry.comment


def pinned_options(identity: str, repo: str | None = None) -> str:
    """Forced-command options pinning `identity` (and optionally one repo).

    The command option's value is parsed by the login shell (`shell -c`), so
    the repo argument is ALWAYS single-quoted — unquoted, a path containing
    `` ` `` or `$` would be expanded by that shell as code (#514). Rejected
    outright: double quotes and backslashes (they'd need a second escaping
    layer inside sshd's double-quoted option value that sshd versions treat
    differently) and control characters (authorized_keys is line-based — an
    embedded newline would terminate the entry and start a new, attacker-
    shaped key line).
    """
    if repo is None:
        return f'restrict,command="schist-shell {identity}"'
    if '"' in repo or "\\" in repo:
        raise HubAdminError(
            f"repo path {repo!r} contains a quote/backslash — cannot be baked "
            f"into an authorized_keys command option. Use --any-repo or a "
            f"saner path."
        )
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in repo):
        raise HubAdminError(
            f"repo path {repo!r} contains control characters — cannot be "
            f"baked into an authorized_keys command option. Use --any-repo "
            f"or a saner path."
        )
    quoted = "'" + repo.replace("'", "'\\''") + "'"
    return f'restrict,command="schist-shell {identity} {quoted}"'


def key_add(
    text: str, identity: str, pubkey: str, repo: str | None = None
) -> tuple[str, str]:
    """Pin `pubkey` to `identity` (confined to `repo` if given); return
    (new_text, action).

    action is 'added' for a new key or 'repinned' when the same key blob was
    already present (its line is replaced — including any previous pin or
    hand-written options). Identity validity against vault.yaml participants
    is the CLI layer's job; this validates only the name shape.
    """
    if not NAME_RE.match(identity):
        raise HubAdminError(
            f"invalid participant name '{identity}': must match {NAME_RE.pattern}"
        )
    keytype, blob, comment = parse_public_key(pubkey)
    new_entry = KeyEntry(pinned_options(identity, repo), keytype, blob, comment, 0)

    lines = text.splitlines()
    matches = [e for e in parse_authorized_keys(text) if e.blob == blob]
    if matches:
        # Replace the first line carrying this blob and drop any duplicates —
        # a leftover duplicate would keep whatever stale pin (or no pin) it
        # had, and sshd uses the FIRST matching key line, so the stale copy
        # could shadow or survive the re-pin.
        if not new_entry.comment:
            new_entry.comment = matches[0].comment
        lines[matches[0].line_no - 1] = new_entry.render()
        doomed = {e.line_no for e in matches[1:]}
        if doomed:
            lines = [
                line for i, line in enumerate(lines, start=1) if i not in doomed
            ]
        return _join(lines), "repinned"

    lines.append(new_entry.render())
    return _join(lines), "added"


def key_remove(text: str, identity: str) -> tuple[str, int]:
    """Drop every key pinned to `identity`; return (new_text, removed_count).

    Un-pinned keys and keys pinned to other identities are untouched.
    """
    doomed = {
        e.line_no for e in parse_authorized_keys(text) if e.pinned_identity == identity
    }
    if not doomed:
        return text, 0
    lines = [
        line
        for line_no, line in enumerate(text.splitlines(), start=1)
        if line_no not in doomed
    ]
    return _join(lines), len(doomed)


def _join(lines: list[str]) -> str:
    out = "\n".join(lines)
    return out + "\n" if out else ""


# ---------------------------------------------------------------------------
# File I/O + CLI wrappers
# ---------------------------------------------------------------------------

def default_authorized_keys_path() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


@contextlib.contextmanager
def _locked(path: Path):
    """Serialize read→mutate→replace cycles on an authorized_keys file.

    Without this, parallel `schist hub key add` runs (e.g. scripted
    provisioning with xargs -P) silently drop each other's keys: both read
    the same base text, both write, last replace wins. The flock lives on a
    stable sidecar — locking the target itself would pin the lock to an
    inode that os.replace is about to swap out from under the next waiter.
    """
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    lock_path = path.with_name(f".{path.name}.schist-lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


def _read_authorized_keys(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
    except OSError as e:
        raise HubAdminError(f"cannot read {path}: {e}")


def _write_authorized_keys(path: Path, text: str) -> None:
    """Atomic replace with 0600 perms (and 0700 on a freshly created ~/.ssh)."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
    try:
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".authorized_keys.schist-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise HubAdminError(f"cannot write {path}: {e}")


def _hub_participants(hub_path: str) -> set[str]:
    """Participant names from the bare hub's HEAD vault.yaml."""
    _, text = read_hub_vault(Path(hub_path))
    try:
        acl = parse_vault_data(yaml.safe_load(text))
    except ACLError as e:
        raise HubAdminError(f"hub vault.yaml failed validation: {e}")
    return {p.name for p in acl.participants}


def _resolve_ak_path(args) -> Path:
    override = getattr(args, "authorized_keys", None)
    return Path(override) if override else default_authorized_keys_path()


def cmd_key_add(args) -> None:
    identity = args.participant
    participants = _hub_participants(args.hub_path)
    if identity not in participants:
        raise HubAdminError(
            f"unknown participant '{identity}' — not in hub vault.yaml. "
            f"Add it first with `schist hub participant add {identity} "
            f"--write <dir> --hub-path {args.hub_path}`."
        )

    if getattr(args, "key_file", None):
        try:
            pubkey = Path(args.key_file).read_text()
        except OSError as e:
            raise HubAdminError(f"cannot read key file {args.key_file}: {e}")
    elif getattr(args, "key", None):
        pubkey = args.key
    else:
        raise HubAdminError("pass the public key via --key-file <path> or --key '<line>'")

    # Bake the hub repo into the forced command so the key can't reach other
    # repos this account hosts; --any-repo restores the identity-only pin.
    repo = None
    if not getattr(args, "any_repo", False):
        repo = str(Path(args.hub_path).resolve())

    ak_path = _resolve_ak_path(args)
    with _locked(ak_path):
        new_text, action = key_add(
            _read_authorized_keys(ak_path), identity, pubkey, repo=repo
        )
        _write_authorized_keys(ak_path, new_text)
    keytype, blob, _ = parse_public_key(pubkey)
    fp = KeyEntry(None, keytype, blob, "", 0).fingerprint
    print(f"{action}: {fp} pinned to '{identity}' in {ak_path}")
    if action == "added":
        print(
            "Reminder: schist-shell must be on the PATH sshd uses "
            "(pip install -e <schist>/cli on this host), and once every key "
            "is pinned, set security.require_pinned_identity: true in "
            "vault.yaml so pre-receive stops trusting client-sent identity."
        )


def cmd_key_list(args) -> None:
    ak_path = _resolve_ak_path(args)
    text = _read_authorized_keys(ak_path)
    entries = parse_authorized_keys(text)
    if not entries:
        print(f"No keys in {ak_path}")
        return

    participants: set[str] | None = None
    if getattr(args, "hub_path", None):
        participants = _hub_participants(args.hub_path)

    for e in entries:
        identity = e.pinned_identity
        if identity is None:
            tag = "UNPINNED"
        elif participants is not None and identity not in participants:
            tag = f"{identity} (NOT IN vault.yaml)"
        else:
            tag = identity
        comment = f" {e.comment}" if e.comment else ""
        print(f"{tag:32} {e.fingerprint} {e.keytype}{comment}")


def cmd_key_remove(args) -> None:
    identity = args.participant
    ak_path = _resolve_ak_path(args)
    with _locked(ak_path):
        text = _read_authorized_keys(ak_path)
        new_text, removed = key_remove(text, identity)
        if removed == 0:
            print(f"No keys pinned to '{identity}' in {ak_path}; no change.")
            return
        _write_authorized_keys(ak_path, new_text)
    print(f"Removed {removed} key(s) pinned to '{identity}' from {ak_path}.")
