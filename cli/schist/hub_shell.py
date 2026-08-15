"""Forced-command SSH shell that pins SCHIST_IDENTITY to an authorized key.

Installed per key in the hub user's authorized_keys:

    restrict,command="schist-shell <participant>" ssh-ed25519 AAAA... comment

sshd then runs `schist-shell <participant>` for every connection made with
that key, regardless of what command the client requested. The wrapper:

1. sets SCHIST_IDENTITY to the pinned participant name, overwriting anything
   the client shipped via SendEnv — identity is now a property of the SSH
   credential, not a client assertion (issue #502);
2. sets SCHIST_IDENTITY_PINNED=1 so the pre-receive hook can distinguish a
   pinned push from a legacy env-asserted one and enforce
   security.require_pinned_identity;
3. confines the key to git transport commands (receive-pack / upload-pack /
   upload-archive) by validating SSH_ORIGINAL_COMMAND and exec'ing through
   `git shell -c`, which re-validates on its own.

The wrapper must be on the PATH sshd uses for forced commands (a plain
`pip install` / `uv pip install --system` of the schist CLI provides it).
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from schist.acl import NAME_RE

# Git transport verbs a spoke key may run. Anything else (interactive shell,
# scp/sftp, arbitrary commands) is rejected before git shell even sees it.
# git-upload-archive is deliberately absent: schist sync needs only
# push (receive-pack) and fetch (upload-pack), and `git archive --remote`
# is extra attack surface with no schist use.
ALLOWED_VERBS = {"git-receive-pack", "git-upload-pack"}

# Two-word spellings (`git receive-pack ...`) some clients send; normalized
# to the hyphenated verb before the whitelist check.
_TWO_WORD_VERBS = {"receive-pack", "upload-pack"}

# Env vars scrubbed before exec: dynamic-linker and shell-startup injection
# vectors a client could ship if the hub's AcceptEnv is loose. Kept narrow —
# PATH/PYTHONPATH have legitimate hub-side uses (venv hooks via
# ~/.ssh/environment), so those are flagged by `schist doctor`'s AcceptEnv
# check instead of being stripped here.
_SCRUB_ENV_PREFIXES = ("LD_", "DYLD_")
_SCRUB_ENV_EXACT = {"BASH_ENV", "ENV"}


class HubShellError(Exception):
    """Raised when the SSH command cannot be validated; message is user-safe."""


def scrub_environ(environ) -> list[str]:
    """Drop injection-vector env vars in place; return the removed names."""
    doomed = [
        k for k in environ
        if k in _SCRUB_ENV_EXACT or k.startswith(_SCRUB_ENV_PREFIXES)
    ]
    for k in doomed:
        del environ[k]
    return doomed


def _resolve_repo(path_str: str) -> Path:
    """Canonicalize a repo path the way git-over-SSH addresses it.

    Relative paths are anchored at the SSH user's home (git shell chdirs
    there), '~' is expanded, and symlinks resolved so the same repo compares
    equal regardless of how the client spelled the URL path.
    """
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        # Unresolvable (dangling symlink chain/loop): compare the lexical
        # form; a nonexistent repo fails in git shell anyway.
        return p


def build_exec(
    identity: str, ssh_original_command: str | None, allowed_repo: str | None = None
) -> tuple[list[str], dict[str, str]]:
    """Validate the pinned identity + client command; return (argv, env updates).

    When `allowed_repo` is set (baked into the authorized_keys entry by
    `schist hub key add`), the key is additionally confined to that one
    repository — without it, a key pinned as alice for vault-A could push to
    any other repo the hub user hosts, under vault-B's ACL for 'alice'.

    Pure so tests can cover the validation matrix without exec'ing anything.
    Raises HubShellError with a message safe to show the connecting client.
    """
    if not NAME_RE.match(identity or ""):
        # Misconfigured authorized_keys line, not a client error — but the
        # client is who sees stderr, so keep the message actionable for both.
        raise HubShellError(
            f"schist-shell: invalid pinned identity {identity!r} in "
            f"authorized_keys (must match {NAME_RE.pattern})"
        )

    if not ssh_original_command:
        raise HubShellError(
            f"schist-shell: interactive shell disabled for pinned key "
            f"'{identity}' — this key is confined to git push/pull."
        )

    try:
        parts = shlex.split(ssh_original_command)
    except ValueError as e:
        raise HubShellError(f"schist-shell: unparseable command: {e}")

    if len(parts) >= 2 and parts[0] == "git" and parts[1] in _TWO_WORD_VERBS:
        verb, rest = f"git-{parts[1]}", parts[2:]
    elif parts:
        verb, rest = parts[0], parts[1:]
    else:
        raise HubShellError("schist-shell: empty command")

    if verb not in ALLOWED_VERBS or len(rest) != 1:
        raise HubShellError(
            f"schist-shell: command not permitted for pinned key "
            f"'{identity}': {ssh_original_command!r}. Allowed: "
            f"{', '.join(sorted(ALLOWED_VERBS))} <repository>."
        )

    if allowed_repo is not None and _resolve_repo(rest[0]) != _resolve_repo(allowed_repo):
        raise HubShellError(
            f"schist-shell: key for '{identity}' is confined to repository "
            f"{allowed_repo} — access to {rest[0]!r} denied."
        )

    # Re-quote the repo path ourselves rather than passing the raw client
    # string through: shlex.split already collapsed the client's quoting, so
    # this rebuild cannot smuggle a second command into `git shell -c`.
    # ALWAYS single-quote (not shlex.quote, which skips quoting safe strings):
    # git shell sq_dequote()s the argument and hard-fails with "fatal: bad
    # argument" when the quotes are missing.
    quoted = "'" + rest[0].replace("'", "'\\''") + "'"
    argv = ["git", "shell", "-c", f"{verb} {quoted}"]
    env = {"SCHIST_IDENTITY": identity, "SCHIST_IDENTITY_PINNED": "1"}
    return argv, env


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point. Execs on success; returns an exit code on error."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2):
        print("usage: schist-shell <participant> [<allowed-repo>]", file=sys.stderr)
        return 2

    try:
        cmd, env = build_exec(
            argv[0],
            os.environ.get("SSH_ORIGINAL_COMMAND"),
            allowed_repo=argv[1] if len(argv) == 2 else None,
        )
    except HubShellError as e:
        print(str(e), file=sys.stderr)
        return 1

    scrub_environ(os.environ)
    os.environ.update(env)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"schist-shell: failed to exec git shell: {e}", file=sys.stderr)
        return 1
    raise AssertionError("unreachable: execvp returned")  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
