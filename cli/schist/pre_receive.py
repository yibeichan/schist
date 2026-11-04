# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git pre-receive hook for enforcing vault.yaml ACL on pushes.

Validates that each pushed file is within the pusher's write scope.
Identity is resolved from environment variables (SCHIST_IDENTITY or GL_USER).

Usage as a git hook:
    GIT_DIR/hooks/pre-receive calls this module's main().
    stdin provides lines of: oldrev newrev refname
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from schist.acl import VaultACL, parse_vault_yaml
from schist.rate_limit import RateLimitResult, check_rate_limit

logger = logging.getLogger("schist.pre_receive")

# SHA representing a zero ref (new branch or deleted branch)
ZERO_SHA = "0" * 40

# What the hook lists from git must not depend on the hub user's git config:
# `log.showRoot=false` hid a root commit's files from the scope and
# frontmatter checks, `log.diffMerges` changes what `-m` shows,
# `log.showSignature=true` wrote gpg's output into the path listing, and
# `diff.ignoreSubmodules=all` dropped added or changed gitlinks from the scope
# listing. Older git ignores keys it does not know.
_SCAN_GIT = ["git", "-c", "log.showRoot=true", "-c", "log.diffMerges=separate",
             "-c", "log.showSignature=false", "-c", "diff.ignoreSubmodules=none"]

# A push may list at most this many entries in any one listing the hook reads
# (paths for the scope check, file versions for the frontmatter scan), counted
# over every pushed commit's diff against each parent and the net diff.
# Commits that switch between two large trees list every entry each time (a
# few hundred KB of objects gave 4M entries), so without a cap a participant
# could hold the hub's CPU for a minute per push; the listings are streamed,
# so memory stays bounded by distinct paths either way.
SCAN_ENTRY_LIMIT = 1_000_000

# Memory grows with entries x path length, and nested directories make each
# path kilobytes long while every tree object stays small (about 2 MB pushed
# held gigabytes), so the listings also stop at a total of path bytes, and a
# path longer than Linux's PATH_MAX, which no spoke could check out, is
# refused outright.
SCAN_BYTE_LIMIT = 128 << 20
MAX_PATH_BYTES = 4096

# The caps above hold for the whole push: main() keeps one running total for
# the scope listings and one for the frontmatter listings across every ref
# line, since each extra ref line naming the same commits costs the pusher
# about 100 bytes and the hub a whole listing. A refusal prints and logs at
# most this many refused paths, so it cannot fill the hub's disk either.
MAX_REPORTED_VIOLATIONS = 100


class ScanLimitExceeded(Exception):
    """A listing the hook reads is past SCAN_ENTRY_LIMIT entries or
    SCAN_BYTE_LIMIT path bytes, or holds a path over MAX_PATH_BYTES. The
    message is the refusal the pusher sees."""


def _too_large() -> ScanLimitExceeded:
    return ScanLimitExceeded(
        f"push is too large for the hub to check: its commits list more than "
        f"{SCAN_ENTRY_LIMIT} file versions or {SCAN_BYTE_LIMIT >> 20} MB of paths. "
        f"Push fewer commits at a time, or split a commit that changes very many files")


def _count_listed(path: bytes, totals: list[int]) -> None:
    """Add one listed path to [entries, bytes]; raise ScanLimitExceeded past
    either cap. Path length is checked separately, against the pushed tip
    (_refuse_long_paths), so a push may still delete a long path."""
    totals[0] += 1
    totals[1] += len(path)
    if totals[0] > SCAN_ENTRY_LIMIT or totals[1] > SCAN_BYTE_LIMIT:
        raise _too_large()


def _git_nul_tokens(cmd: list[str], timeout: float):
    """NUL-separated tokens of a command's stdout, read as they arrive, so the
    whole output is never held. Raises TimeoutExpired once `timeout` seconds
    pass, and CalledProcessError on a non-zero exit; a consumer that stops
    early kills the command. The command runs in its own process group, which
    is what gets killed, so a git wrapper's children cannot hold stdout open
    past the timeout.

    A token still open (no NUL yet) is kept in a bytearray and only each new
    chunk is searched, so reading is linear in the output; joining the open
    token to every chunk made one long path cost its length squared. A token
    that grows past SCAN_BYTE_LIMIT raises ScanLimitExceeded before its NUL
    arrives, since git puts no limit on a tree entry's name."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    expired = threading.Event()

    def _kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _expire():
        expired.set()
        _kill()

    timer = threading.Timer(timeout, _expire)
    timer.start()
    try:
        pending = bytearray()
        while True:
            chunk = proc.stdout.read1(1 << 16)
            if not chunk:
                break
            start = 0
            end = chunk.find(b"\0")
            while end >= 0:
                if pending:
                    pending += chunk[start:end]
                    token = bytes(pending)
                    pending.clear()
                    yield token
                else:
                    yield chunk[start:end]
                start = end + 1
                end = chunk.find(b"\0", start)
            pending += chunk[start:]
            if len(pending) > SCAN_BYTE_LIMIT:
                raise _too_large()
        if pending:
            yield bytes(pending)
        rc = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            _kill()
            proc.wait()
        proc.stdout.close()
    if expired.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


# Violation.scope for a path that is not valid UTF-8 (#697)
NON_UTF8_SCOPE = "(path is not valid UTF-8)"


@dataclass
class Violation:
    """A single ACL violation: a file the identity cannot write."""

    identity: str
    filepath: str
    scope: str
    refname: str


def resolve_identity() -> str | None:
    """Resolve the pushing identity from environment variables.

    Priority:
    1. SCHIST_IDENTITY — explicit schist identity
    2. GL_USER — gitolite compatibility
    """
    return os.environ.get("SCHIST_IDENTITY") or os.environ.get("GL_USER") or None


def _sanitize_log_field(value: str) -> str:
    """Neutralize control characters before a value enters the audit log.

    The audit log is line-oriented and its entries are grep/parsed, so any
    field that can carry a newline is a log-forgery vector. `identity` on the
    pinning-rejection path is the raw client-sent SCHIST_IDENTITY (logged
    BEFORE the participant-name validation), and filenames come from
    `git log -z` which preserves embedded newlines (#518 review; #5 pre-
    existing). Replace CR/LF/other control bytes with a visible marker rather
    than dropping them, so the forgery attempt is evident in the log.
    """
    return "".join(_escape_char(c) for c in value)


def _escape_char(c: str) -> str:
    """One character, with control bytes and undecodable bytes made visible.

    A filename that is not valid UTF-8 reaches us as surrogate
    escapes (U+DC80..U+DCFF, one per raw byte). Those cannot be encoded to the
    audit log or a terminal, so show them as the `\\xNN` byte they stand for.
    """
    o = ord(c)
    if 0xDC80 <= o <= 0xDCFF:
        return "\\x%02x" % (o - 0xDC00)
    if o < 0x20 or o == 0x7F:
        return "\\x%02x" % o
    # C1 controls (U+0085 NEXT LINE among them) and the Unicode line and
    # paragraph separators end a line for JavaScript's `^` under the `m` flag,
    # which the MCP server's failure classifiers use. Left as they are, a
    # refused path could carry a forged "REJECTED: rate limit" line (#813).
    if 0x80 <= o <= 0x9F or o in (0x2028, 0x2029):
        return "\\u%04x" % o
    return c


# surrogateescape's stand-ins for bytes that are not UTF-8. A compiled search
# runs in C: the per-character loop it replaces took minutes of hub CPU on a
# push of many very long paths.
_UNDECODABLE_RE = re.compile("[\udc80-\udcff]")


def _has_undecodable_bytes(value: str) -> bool:
    return _UNDECODABLE_RE.search(value) is not None


def _push_source(environ: dict[str, str] | None = None) -> str:
    """Client address for audit-log attribution (#518).

    sshd sets SSH_CONNECTION="<client-ip> <client-port> <server-ip> <port>";
    SSH_CLIENT is the legacy spelling. A push with neither is filesystem-local.
    """
    env = os.environ if environ is None else environ
    for var in ("SSH_CONNECTION", "SSH_CLIENT"):
        value = env.get(var, "")
        if value.strip():
            return value.split()[0]
    return "local"


def pinning_rejection(
    acl: VaultACL, environ: dict[str, str] | None = None
) -> str | None:
    """Return a rejection message if pinned-identity enforcement fails, else None.

    When vault.yaml sets security.require_pinned_identity, an SSH push must
    carry SCHIST_IDENTITY_PINNED=1 — set only by the schist-shell forced
    command, which derives the identity from the authorized key instead of
    trusting a client-sent variable (issue #502). Local pushes (no SSH_*
    env) are exempt: filesystem access to the bare hub is admin authority by
    design, matching hub_admin.py's trust model.

    Note this check is only as strong as the hub's sshd config: AcceptEnv
    must not forward SCHIST_* variables, or a client could ship the pinned
    marker itself (`schist doctor --hub-path` checks for that).

    Transport assumption: schist hubs are SSH-or-local only (docs require an
    SSH box precisely because hosted git doesn't run pre-receive). If a hub
    is ever exposed over smart-HTTP or git-daemon, those transports set
    neither SSH_* var and would take the local-admin exemption — do not
    front a schist hub with another transport without revisiting this gate.
    """
    if not acl.security.require_pinned_identity:
        return None
    env = os.environ if environ is None else environ
    if "SSH_CONNECTION" not in env and "SSH_CLIENT" not in env:
        return None
    if env.get("SCHIST_IDENTITY_PINNED") == "1":
        return None
    return (
        "REJECTED: this hub requires SSH-key-pinned identities "
        "(security.require_pinned_identity is enabled in vault.yaml).\n"
        "Your key is not pinned to a participant — client-asserted "
        "SCHIST_IDENTITY is not trusted.\n"
        "Ask the hub admin to pin your key:\n"
        "  schist hub key add <participant> --key-file <your .pub> "
        "--hub-path <hub>\n"
        "(Pushing locally on the hub host from an SSH login session? The\n"
        "session's SSH_CONNECTION env triggers this guard; admin pushes can\n"
        "run `env -u SSH_CONNECTION -u SSH_CLIENT git push ...` — that only\n"
        "helps with shell-inherited env, not the transport env sshd sets\n"
        "server-side for remote pushes.)"
    )


def log_pinning_rejection(identity: str, log_path: Path | None = None) -> None:
    """Append an unpinned-identity rejection to the audit log."""
    if log_path is None:
        git_dir = os.environ.get("GIT_DIR", ".")
        log_path = Path(git_dir) / "hooks" / "rejected-pushes.log"

    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    entry = (
        f"[{timestamp}] PINNING_REJECTED identity={_sanitize_log_field(identity)} "
        f"src={_push_source()} reason=identity-not-key-pinned\n"
    )

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        logger.warning("Failed to write pinning rejection log: %s", e)


def derive_scope(filepath: str) -> str:
    """Derive the ACL scope from a file path.

    For scope_convention=subdirectory:
    - research/mario/note.md  → research/mario
    - research/note.md        → research
    - vault.yaml              → (root) — empty string means root-level file

    The returned scope is the directory portion of the path, normalized
    to prevent path traversal attacks (e.g. research/mario/../hbcd/).
    Root-level files return empty string, which requires "*" write access.
    """
    # normpath resolves ".." segments without touching the filesystem
    normalized = os.path.normpath(filepath)
    parent = str(Path(normalized).parent)
    if parent == ".":
        return ""
    return parent


def get_changed_files(oldrev: str, newrev: str, totals: list[int] | None = None) -> list[str]:
    """Get list of files touched by commits introduced between two revisions.

    Handles special cases:
    - New branch (oldrev is zero): inspect commits not already reachable
    - Deleted branch (newrev is zero): no files to check

    This intentionally enumerates per-commit file touches instead of using a
    net diff. A multi-commit push can add an out-of-scope file in an
    intermediate commit and remove it before the tip; the hook must still see
    that file because it entered git history.

    Two flags close what the plain listing missed (#703):
    - `--no-renames`: with rename detection a rename is listed by its
      destination only, so deleting a file in a scope the pusher cannot write
      went unchecked as long as the content reappeared in one they can.
    - `-c`: by default a merge commit lists nothing, so a path added or
      deleted only in the merge result was never checked. The combined diff
      lists exactly the paths that differ from every parent. Not `-m`: that
      diffs against each parent, so a routine pull-merge would list every path
      the hub side brought in from other participants and be rejected.

    `-c` alone is not enough, because the pusher chooses a merge's parents:
    with an old hub commit as one parent and its content as the result, any
    later change in any scope (vault.yaml included) is rolled back while the
    combined diff lists nothing. So an existing branch also adds the net diff
    from its current tip: every path whose content this push changes on the
    branch, however the merge was built. A routine pull-merge adds nothing
    there that is not the pusher's own, since the hub side is already in
    `oldrev`. A new branch has no current content to roll back; its new
    content is all in the per-commit listing.

    `totals` is the push's running [entries, path bytes] for the caps in
    _count_listed; main() passes one list for every ref line of a push.
    """
    if newrev == ZERO_SHA:
        # Branch deletion — nothing to check
        return []

    if oldrev == ZERO_SHA:
        # New branch — inspect commits introduced by this ref. Exclude commits
        # already reachable from existing refs so creating a branch at an
        # existing commit does not re-count old history.
        sources = [[*_SCAN_GIT, "log", "--root", "--name-only", "--no-renames", "-c", "--format=",
                    "-z", newrev, "--not", "--all"]]
    else:
        sources = [[*_SCAN_GIT, "log", "--root", "--name-only", "--no-renames", "-c", "--format=",
                    "-z", f"{oldrev}..{newrev}"],
                   [*_SCAN_GIT, "diff", "--name-only", "--no-renames", "-z", oldrev, newrev]]

    # Bytes, not text: a filename git stores need not be UTF-8, and a strict
    # decode here raised UnicodeDecodeError out of the hook (#697). Explicit
    # UTF-8 with surrogateescape, NOT os.fsdecode: that follows the locale, and
    # sshd forwards the client's LANG/LC_*, so under a latin-1 locale a bad
    # byte would decode to mojibake and slip past check_push. Distinct bad bytes
    # stay distinct, so check_push can reject the path by name.
    # Streamed and de-duplicated as read, with the same entry cap as the
    # frontmatter scan: buffering the listing let commits that switch between
    # two large trees hold gigabytes on the hub before any check ran.
    changed: dict[str, None] = {}
    if totals is None:
        totals = [0, 0]
    long_paths: list[bytes] = []
    for cmd in sources:
        for token in _git_nul_tokens(cmd, 30):
            # Each token is a path exactly as git stores it; one may begin
            # with a line break, so nothing is stripped.
            if not token:
                continue
            _count_listed(token, totals)
            if len(token) > MAX_PATH_BYTES:
                long_paths.append(token)
            changed[token.decode("utf-8", "surrogateescape")] = None
    _refuse_long_paths(newrev, long_paths, totals)
    return list(changed)


def _refuse_long_paths(newrev: str, paths: list[bytes],
                       totals: list[int] | None = None) -> None:
    """Refuse the push if any of `paths` (each over MAX_PATH_BYTES) is in the
    pushed tip, where no spoke could check it out. One the push deletes, or
    that only an intermediate commit held, passes, so a long path that landed
    before the hook refused them can still be pushed away."""
    if not paths:
        return
    wanted = set(paths)
    # The tip's own listing, read once, not a lookup per name. Looking names
    # up went wrong three ways: a line-based `cat-file --batch-check` drops a
    # trailing carriage return and so asked for a different name, a gitlink
    # read as missing because the hub need not have its commit, and a
    # `rev-parse` per name holding a line break cost one process each.
    # `cat-file -Z` needs git 2.42; ls-tree -z takes any name on git 2.30.
    # It also walks a tree's entries in stored order, as the diff listings
    # do, so an unsorted tree cannot hide an entry from this check. Counted
    # against the scope listing's push-wide totals, since the tip may be far
    # larger than the push and a push may carry several ref lines.
    if totals is None:
        totals = [0, 0]
    for token in _git_nul_tokens(["git", "ls-tree", "-r", "-z", "--name-only", "--full-tree",
                                  newrev], 30):
        _count_listed(token, totals)
        if token in wanted:
            raise ScanLimitExceeded(
                f"push contains a path longer than {MAX_PATH_BYTES} bytes; shorten it")


FRONTMATTER_SCOPE = "(note must open with a plain --- line)"
_BOM = "﻿".encode("utf-8")


def served_branch() -> str | None:
    """The branch this hub serves: the target of the bare repo's HEAD, or
    None when a bare repo's HEAD is not a branch (detached, or `symbolic-ref`
    fails). None refuses every ref update from an identity without `*`
    write; guessing `refs/heads/main` let a scoped writer create `main` on a
    hub serving another branch. A timeout or OSError raises, since the hub
    may answer on a retry. A non-bare repo's HEAD is whatever is checked out
    there, so only a bare repo's counts and a non-bare one serves
    refs/heads/main."""
    bare = subprocess.run(["git", "rev-parse", "--is-bare-repository"],
                          capture_output=True, text=True, encoding="utf-8", timeout=10)
    if bare.stdout.strip() != "true":
        return "refs/heads/main"
    r = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"],
                       capture_output=True, timeout=10)
    # Decoded like the pushed ref names in main(), not by the locale: a
    # non-ASCII branch name read two ways could match a different ref.
    ref = r.stdout.decode("utf-8", "surrogateescape").removesuffix("\n")
    return ref if r.returncode == 0 and ref.startswith("refs/heads/") else None


def ref_rejection(identity: str, acl: VaultACL, newrev: str, refname: str,
                  branch: str | None) -> str | None:
    """Why this ref update is refused, or None. Only `*` writers may touch a
    ref other than the served branch or delete one: the scope check covers
    the commits a push adds, not how other refs (a tag named `main`) could
    shadow the branch every spoke reads."""
    entry = acl.access.get(identity)
    if entry is not None and "*" in entry.write:
        return None
    if newrev == ZERO_SHA:
        return f"{refname}: only a '*' writer may delete a ref"
    if branch is None:
        return f"{refname}: the hub cannot read the branch it serves, so only a '*' writer may push"
    if refname != branch:
        return f"{refname}: '{identity}' may only push {branch}"
    return None


def _bad_opener(data: bytes) -> bool:
    """True when gray-matter (the MCP server's parser) would read a language
    name after the opening `---`: the rest of that line, trimmed, is not
    empty, and the fourth character is not another `-`. It strips a BOM
    first. Only YAML frontmatter is accepted, since its `javascript` engine
    evaluates the block. The MCP server mirrors this as hasNonYamlOpener;
    schema/frontmatter-opener-parity.json pins the two."""
    if data.startswith(_BOM):
        data = data[len(_BOM):]
    if not data.startswith(b"---") or data[3:4] == b"-":
        return False
    rest = data[3:].split(b"\n", 1)[0]
    return rest.strip() != b""


def _raw_entries(tokens):
    """(new mode, new blob id, status, path) per `--raw -z` entry. Each entry
    is a ":<old mode> <new mode> <old id> <new id> <status>" token followed
    by its path token; other tokens (commit ids from --format) are skipped.
    --no-renames keeps every entry to a single path."""
    tokens = iter(tokens)
    for token in tokens:
        head = token.lstrip(b"\n")
        if not head.startswith(b":"):
            continue
        fields = head[1:].split(b" ")
        path = next(tokens, None)
        # Output cut short (git killed mid-entry) refuses like a git failure,
        # not as a traceback.
        if len(fields) != 5 or path is None:
            raise subprocess.CalledProcessError(128, ["git", "--raw"], output=head)
        _old_mode, new_mode, _old_id, new_id, status = fields
        yield new_mode, new_id.decode("ascii", "replace"), status, path.decode("utf-8", "surrogateescape")


def _pushed_markdown_blobs(oldrev: str, newrev: str,
                           totals: list[int] | None = None) -> dict[str, list[str]]:
    """Blob id -> paths, for every Markdown version this push writes or
    leaves at the tip: each pushed commit's diff against each of its parents
    (`-m`, so a path changed only in a merge counts, and `--root` for a root
    commit's whole tree), plus, for an existing branch, the net diff from
    the old tip. The net diff catches a commit whose parent is an old hub
    commit still holding a bad note: the note is in no pushed commit's own
    diff, yet lands on the branch. Read with -z, so a path may hold any
    byte. Raises ScanLimitExceeded past the caps in _count_listed, counted
    into `totals` (the push's running total) when given."""
    rng = [newrev, "--not", "--all"] if oldrev == ZERO_SHA else [f"{oldrev}..{newrev}"]
    sources = [[*_SCAN_GIT, "log", "-m", "--root", "--no-renames", "--raw", "-z",
                "--no-abbrev", "--format=%H", *rng]]
    if oldrev != ZERO_SHA:
        sources.append([*_SCAN_GIT, "diff-tree", "-r", "-z", "--no-abbrev", "--no-renames",
                        oldrev, newrev])
    # Paths per blob as a dict (an ordered set): one blob can sit at every
    # path of a large tree, and a list membership test made that quadratic.
    blobs: dict[str, dict[str, None]] = {}
    if totals is None:
        totals = [0, 0]
    for cmd in sources:
        for new_mode, new_id, status, path in _raw_entries(_git_nul_tokens(cmd, 60)):
            _count_listed(path.encode("utf-8", "surrogateescape"), totals)
            if (status == b"D" or new_mode == b"160000" or not path.lower().endswith(".md")
                    or _has_undecodable_bytes(path)):
                continue
            blobs.setdefault(new_id, {})[path] = None
    return {blob_id: list(paths) for blob_id, paths in blobs.items()}


_HEAD_BYTES = 4096


def _read_opener(stream, size: int) -> bytes:
    """Enough of a `size`-byte object on `stream` for _bad_opener to answer as
    it would on all of it; the rest is read in chunks and dropped, so a large
    note is never held. That is its first _HEAD_BYTES bytes, plus one
    non-blank byte when the first line runs past them and is not blank there.
    Raises EOFError when the stream ends early."""
    head = stream.read(min(size, _HEAD_BYTES))
    if len(head) < min(size, _HEAD_BYTES):
        raise EOFError
    remaining = size - len(head)
    line_open = b"\n" not in head
    tail_nonblank = False
    while remaining:
        chunk = stream.read(min(remaining, 1 << 16))
        if not chunk:
            raise EOFError
        remaining -= len(chunk)
        if line_open:
            segment, newline, _ = chunk.partition(b"\n")
            if segment.strip():
                tail_nonblank, line_open = True, False
            elif newline:
                line_open = False
    return head + (b"x" if tail_nonblank else b"")


def _blob_heads(ids: list[str], timeout: float):
    """(id, type, opener) per object from one `cat-file --batch`, where
    opener is what _read_opener keeps, so memory stays small whatever the
    blob sizes. A missing object raises: a pushed commit names it, so the
    repository is broken, not the path absent."""
    proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    expired = threading.Event()

    def _expire():
        expired.set()
        proc.kill()

    def _feed():
        try:
            for blob_id in ids:
                proc.stdin.write(blob_id.encode("ascii") + b"\n")
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass                        # killed or exited; the reader reports it

    timer = threading.Timer(timeout, _expire)
    writer = threading.Thread(target=_feed, daemon=True)
    timer.start()
    writer.start()
    try:
        for blob_id in ids:
            line = proc.stdout.readline()
            if not line.endswith(b"\n"):
                break                   # killed or exited early
            line = line[:-1]
            if line.endswith((b" missing", b" ambiguous")):
                raise subprocess.CalledProcessError(128, ["git", "cat-file", "--batch"],
                                                    output=line)
            fields = line.split(b" ")
            if len(fields) != 3:
                break
            _sha, kind, size_s = fields
            try:
                opener = _read_opener(proc.stdout, int(size_s))
            except (EOFError, ValueError):
                break
            if proc.stdout.read(1) != b"\n":
                break
            yield blob_id, kind, opener
        else:
            proc.wait()
            if not expired.is_set():
                return
    finally:
        timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        writer.join(timeout=5)
        proc.stdout.close()
    if expired.is_set():
        raise subprocess.TimeoutExpired(["git", "cat-file", "--batch"], timeout)
    raise subprocess.CalledProcessError(proc.returncode or 128, ["git", "cat-file", "--batch"])


def find_bad_frontmatter(oldrev: str, newrev: str, changed_files: list[str],
                         totals: list[int] | None = None) -> list[str]:
    """Markdown paths whose content, at any commit this push adds or at the
    pushed tip, opens with `---` plus a language name. Every version a
    pushed commit writes is read, not only the tip, once per distinct blob:
    reading every path at every commit made a long push cost commits x
    paths reads and memory on the hub. A path that is not valid UTF-8 is
    skipped here; the scope check refuses it for every identity.
    `changed_files` is kept for callers; the versions come from the commits
    and the net diff. Raises ScanLimitExceeded for a push too large to
    check; `totals` is the push's running total for those caps."""
    if newrev == ZERO_SHA:
        return []
    blobs = _pushed_markdown_blobs(oldrev, newrev, totals)
    if not blobs:
        return []
    bad: list[str] = []
    for blob_id, kind, data in _blob_heads(list(blobs), 60):
        if kind == b"blob" and _bad_opener(data):
            bad.extend(blobs[blob_id])
    return list(dict.fromkeys(bad))


def check_push(
    identity: str,
    changed_files: list[str],
    acl: VaultACL,
    refname: str,
) -> list[Violation]:
    """Check all changed files against the identity's write ACL.

    Returns a list of Violation objects for any out-of-scope writes.
    """
    violations: list[Violation] = []

    for filepath in changed_files:
        if _has_undecodable_bytes(filepath):
            # Not a path the rest of the system can hold: a macOS spoke cannot
            # check it out and ingest is UTF-8. Rejected before the ACL, for
            # every identity including wildcard writers.
            violations.append(Violation(
                identity=identity,
                filepath=filepath,
                scope=NON_UTF8_SCOPE,
                refname=refname,
            ))
            continue
        scope = derive_scope(filepath)

        if scope == "":
            # Root-level file — only wildcard ("*") writers can modify
            entry = acl.access.get(identity)
            if entry is None or "*" not in entry.write:
                violations.append(Violation(
                    identity=identity,
                    filepath=filepath,
                    scope="(root)",
                    refname=refname,
                ))
        else:
            if not acl.can_write(identity, scope):
                violations.append(Violation(
                    identity=identity,
                    filepath=filepath,
                    scope=scope,
                    refname=refname,
                ))

    return violations


def format_rejection(violations: list[Violation], branch: str = "refs/heads/main") -> str:
    """Format a human-readable rejection message. `branch` is the served
    branch, which the frontmatter hint tells the pusher to reset to."""
    content_only = (NON_UTF8_SCOPE, FRONTMATTER_SCOPE)
    # Both spoke-side classifiers key on the `REJECTED:` prefix, not on the
    # words after it, so the header may say what was refused (#795).
    if all(v.scope in content_only for v in violations):
        header = "REJECTED: push contains files the hub refuses for every identity"
    else:
        header = "REJECTED: push contains out-of-scope writes"
    lines = [
        header,
        f"Identity: {_sanitize_log_field(violations[0].identity)}",
        "",
        "Violations:",
    ]
    for v in violations[:MAX_REPORTED_VIOLATIONS]:
        lines.append(f"  - {_sanitize_log_field(v.filepath)} (scope: {_sanitize_log_field(v.scope)})")
    if len(violations) > MAX_REPORTED_VIOLATIONS:
        lines.append(f"  ... and {len(violations) - MAX_REPORTED_VIOLATIONS} more")
    lines.append("")
    if any(v.scope == NON_UTF8_SCOPE for v in violations):
        lines.append(
            "A path that is not valid UTF-8 is refused for every identity, so no "
            "vault.yaml change helps: rename the file (bytes are shown as \\xNN), "
            "then `git reset --soft` the unpushed commit and recommit."
        )
    bad_refs = {v.refname for v in violations if v.scope == FRONTMATTER_SCOPE}
    if bad_refs:
        # Reset to the ref that carried the note, so the hint never squashes
        # one branch's commits onto another (#791).
        ref = next(iter(bad_refs))
        if len(bad_refs) > 1 or not ref.startswith('refs/heads/'):
            ref = branch
        lines.append(
            "Notes must open with a plain `---` line (YAML frontmatter), for every "
            "identity. Fix the first line, then "
            f"`git reset --soft origin/{_sanitize_log_field(ref.removeprefix('refs/heads/'))}` and "
            "recommit, so no unpushed commit keeps the old version."
        )
    if any(v.scope not in content_only for v in violations):
        lines.append("Check vault.yaml access rules for your identity.")
    return "\n".join(lines)


def _report_ref_reasons(identity: str, ref_reasons: list[str], log_path: Path | None) -> None:
    if ref_reasons:
        print("REJECTED: push updates refs this identity may not change", file=sys.stderr)
        for reason in ref_reasons:
            print(f"  - {_sanitize_log_field(reason)}", file=sys.stderr)
        log_ref_rejection(identity, ref_reasons, log_path=log_path)


def _rejection_log_path(log_path: Path | None) -> Path:
    if log_path is None:
        # Default: alongside the hook in GIT_DIR/hooks/
        git_dir = os.environ.get("GIT_DIR", ".")
        log_path = Path(git_dir) / "hooks" / "rejected-pushes.log"
    return log_path


def _append_log(log_path: Path, entries: list[str]) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.writelines(entries)
    except OSError as e:
        logger.warning("Failed to write rejection log: %s", e)


def log_rejection(
    violations: list[Violation],
    log_path: Path | None = None,
) -> None:
    """Append rejection details to the audit log, one entry per ref. Each
    entry records how many paths that ref had refused; at most
    MAX_REPORTED_VIOLATIONS paths are written for the whole push."""
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    identity = _sanitize_log_field(violations[0].identity)
    by_ref: dict[str, list[Violation]] = {}
    for v in violations:
        by_ref.setdefault(v.refname, []).append(v)
    budget = MAX_REPORTED_VIOLATIONS
    entries = []
    for ref, refused in by_ref.items():
        files = [_sanitize_log_field(v.filepath) for v in refused[:budget]]
        budget -= len(files)
        if len(refused) > len(files):
            files.append(f"... and {len(refused) - len(files)} more")
        entries.append(
            f"[{timestamp}] REJECTED identity={identity} src={_push_source()} "
            f"ref={_sanitize_log_field(ref)} count={len(refused)} files=[{', '.join(files)}]\n")
    _append_log(_rejection_log_path(log_path), entries)


def log_ref_rejection(
    identity: str,
    reasons: list[str],
    log_path: Path | None = None,
) -> None:
    """Append ref refusals (another branch, a tag, a deletion) to the audit log."""
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    _append_log(_rejection_log_path(log_path), [
        f"[{timestamp}] REF_REJECTED identity={_sanitize_log_field(identity)} "
        f"src={_push_source()} reason={_sanitize_log_field(r)}\n"
        for r in reasons
    ])


def log_rate_limit_rejection(
    identity: str,
    result: RateLimitResult,
    log_path: Path | None = None,
) -> None:
    """Append a rate-limit rejection to the audit log."""
    if log_path is None:
        git_dir = os.environ.get("GIT_DIR", ".")
        log_path = Path(git_dir) / "hooks" / "rejected-pushes.log"

    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    entry = (
        f"[{timestamp}] RATE_LIMIT_REJECTED identity={_sanitize_log_field(identity)} "
        f"src={_push_source()} "
        f"reason={result.reason} limit={result.limit} observed={result.observed} "
        f"retry_after={result.retry_after}\n"
    )

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as e:
        logger.warning("Failed to write rate-limit rejection log: %s", e)


def find_vault_yaml() -> Path | None:
    """Locate vault.yaml on the filesystem for non-bare repos.

    In a bare repo (typical for pre-receive), returns None so the caller
    falls through to git show HEAD:vault.yaml instead.
    """
    git_dir = os.environ.get("GIT_DIR", ".")

    # Check for a working tree (non-bare repo: GIT_DIR is <repo>/.git)
    work_tree = os.environ.get("GIT_WORK_TREE")
    if work_tree:
        candidate = Path(work_tree) / "vault.yaml"
        if candidate.is_file():
            return candidate

    # Heuristic: if GIT_DIR ends with .git, parent is the working tree
    git_path = Path(git_dir)
    if git_path.name == ".git":
        candidate = git_path.parent / "vault.yaml"
        if candidate.is_file():
            return candidate

    return None


def _git_bytes(*args: str) -> bytes:
    """stdout of a git command that must succeed; any failure, a timeout
    included, raises."""
    return subprocess.run(["git", *args], capture_output=True, check=True, timeout=10).stdout


_LOOSE_DIR_RE = re.compile(rb"[0-9a-f]{2}")


def _hub_has_objects(pre: list[str]) -> bool:
    """True when the repository's own object store holds any object: a loose
    object under objects/<xx>/, a pack under objects/pack/, or an alternates
    file naming another store. The quarantine directory a push is received
    into (objects/tmp_objdir-incoming-*, where GIT_OBJECT_DIRECTORY points
    during pre-receive) is not looked at, so a fresh hub's first push still
    counts as fresh. Read as bytes, so no path goes through the locale."""
    git_dir = _git_bytes(*pre, "rev-parse", "--git-dir").removesuffix(b"\n")
    objects = os.path.join(git_dir, b"objects")
    for name in os.listdir(objects):
        path = os.path.join(objects, name)
        if _LOOSE_DIR_RE.fullmatch(name) and os.path.isdir(path) and os.listdir(path):
            return True
    pack = os.path.join(objects, b"pack")
    if os.path.isdir(pack) and any(n.endswith(b".pack") for n in os.listdir(pack)):
        return True
    alternates = os.path.join(objects, b"info", b"alternates")
    return os.path.isfile(alternates) and os.path.getsize(alternates) > 0


def extract_vault_yaml_from_git(git_dir: str | None = None) -> str | None:
    """Extract vault.yaml content from HEAD in a bare repository.

    None only when HEAD has no vault.yaml: a hub with no refs and no objects
    of its own (before its first push), or a HEAD commit whose tree has no
    such entry. A hub with objects but no refs has lost its refs (`schist
    hub init` always seeds a commit holding vault.yaml), and raises.
    None lets the push through unchecked, so every other failure (a git
    error, a missing or corrupt object, a timeout on a loaded hub) raises
    and the push is refused. Reading a failed `git show` as "no vault.yaml"
    accepted a scoped writer's out-of-scope push when the blob could not
    be read.

    Decoded as UTF-8, not by the locale sshd forwards: under latin-1 a
    non-ASCII scope name read as a different string, which matched a
    differently named directory. Content that is not UTF-8 raises too.

    `git_dir` reads another repository, as `schist doctor --hub-path` does;
    the hook reads the one git runs it in."""
    pre = ["--git-dir", git_dir] if git_dir else []
    # HEAD naming a branch with no ref is "no vault.yaml" only on a hub that
    # has no refs at all. With other refs present (the served branch was
    # deleted or renamed, or HEAD names `master` while spokes push `main`)
    # a vault.yaml exists somewhere and the hook cannot tell which one
    # governs, so it refuses. `rev-parse -q --verify <ref>` resolves the ref
    # without reading its object, so a ref whose commit is missing resolves
    # here and fails below; it exits 1 only when the ref does not exist.
    head = subprocess.run(["git", *pre, "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, timeout=10)
    if head.returncode == 0:
        # The ref goes back to git as the bytes git printed: a str argument
        # is encoded by the locale, and under latin-9 `refs/heads/maïn` went
        # back as a different name that does not exist.
        raw_ref = head.stdout.removesuffix(b"\n")
        ref = raw_ref.decode("utf-8", "surrogateescape")
        tip = subprocess.run(["git", *pre, "rev-parse", "-q", "--verify", raw_ref],
                             capture_output=True, timeout=10)
        if tip.returncode == 1:
            if not _git_bytes(*pre, "for-each-ref", "--count=1", "--format=%(refname)").strip():
                if not _hub_has_objects(pre):
                    return None
                raise RuntimeError("the hub has objects but no refs; refs may have been lost; a "
                                   "hub admin should restore them, see git fsck --lost-found")
            raise RuntimeError(f"HEAD names {ref}, which does not exist, while the "
                               "repository has other refs; a hub admin points HEAD at the "
                               "served branch with `git symbolic-ref HEAD refs/heads/<branch>`")
        if tip.returncode != 0:
            raise subprocess.CalledProcessError(tip.returncode, tip.args, tip.stdout, tip.stderr)
    elif head.returncode != 1:          # 1 is a detached HEAD, which is a commit
        raise subprocess.CalledProcessError(head.returncode, head.args, head.stdout, head.stderr)
    if not _git_bytes(*pre, "ls-tree", "-z", "HEAD", "--", "vault.yaml"):
        return None
    return _git_bytes(*pre, "cat-file", "blob", "HEAD:vault.yaml").decode("utf-8")


def _git_failure(e: BaseException) -> str:
    """A short cause for a failed git call, without its command line."""
    if isinstance(e, subprocess.TimeoutExpired):
        return "git timed out"
    if isinstance(e, subprocess.CalledProcessError):
        if e.returncode < 0:
            return f"git was killed by signal {-e.returncode}"
        return f"git exited {e.returncode}"
    return type(e).__name__


def _killed(e: BaseException) -> bool:
    """A git that a signal ended (the OOM killer under a memory limit, an
    admin) said nothing about the repository, so a retry may succeed."""
    return isinstance(e, subprocess.CalledProcessError) and e.returncode < 0


def load_acl() -> VaultACL | None:
    """Load vault.yaml ACL, trying filesystem first, then git show."""
    path = find_vault_yaml()
    if path is not None:
        return parse_vault_yaml(path)

    # Bare repo fallback: extract from git
    content = extract_vault_yaml_from_git()
    if content is not None:
        import yaml

        from schist.acl import parse_vault_data

        data = yaml.safe_load(content)
        return parse_vault_data(data)

    return None


def main(
    stdin: list[str] | None = None,
    acl: VaultACL | None = None,
    identity: str | None = None,
    log_path: Path | None = None,
    db_path: Path | None = None,
) -> int:
    """Pre-receive hook entry point.

    Args:
        stdin: Lines from stdin (oldrev newrev refname). None reads sys.stdin.
        acl: Pre-loaded VaultACL. None loads from vault.yaml.
        identity: Override identity. None resolves from environment.
        log_path: Override rejection log path.
        db_path: Override rate-limit sqlite DB path.

    Returns:
        0 if push is allowed, 1 if rejected.
    """
    # Resolve identity
    if identity is None:
        identity = resolve_identity()
    if identity is None:
        print("REJECTED: cannot determine push identity", file=sys.stderr)
        print(
            "Set SCHIST_IDENTITY or GL_USER environment variable.",
            file=sys.stderr,
        )
        return 1

    # Load ACL
    if acl is None:
        try:
            acl = load_acl()
        except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as e:
            if isinstance(e, subprocess.CalledProcessError) and not _killed(e):
                # git ran and failed: a missing or corrupt object, which no
                # retry fixes, so a REJECTED that spokes do not treat as
                # transient.
                print(f"REJECTED: failed to load vault.yaml: {_git_failure(e)} reading it; a "
                      "hub admin should run `git fsck` on the hub.", file=sys.stderr)
                return 1
            # The hub's git timed out, could not run, or was killed by a
            # signal: load or a transient fault, and the pusher's only move
            # is to retry, so this is an ERROR (the hub), which spokes treat
            # as transient.
            print(f"ERROR: the hub could not read vault.yaml ({_git_failure(e)}); please "
                  "retry the push.", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"REJECTED: failed to load vault.yaml: {e}", file=sys.stderr)
            if isinstance(e, UnicodeDecodeError):
                print("vault.yaml is not valid UTF-8, so every push is refused until a hub "
                      "admin rewrites it as UTF-8 (docs/hub-spoke-setup.md, Troubleshooting, "
                      "\"REJECTED: failed to load vault.yaml\").", file=sys.stderr)
            return 1

    if acl is None:
        # No vault.yaml — allow push (vault.yaml is optional per spec)
        return 0

    # Enforce key-pinned identity before even checking the participant list:
    # an unpinned SSH push is rejected regardless of what identity it claims.
    pin_msg = pinning_rejection(acl)
    if pin_msg is not None:
        print(pin_msg, file=sys.stderr)
        log_pinning_rejection(identity, log_path=log_path)
        return 1

    # Verify identity is a known participant
    if acl.get_participant(identity) is None:
        print(
            f"REJECTED: unknown identity '{_sanitize_log_field(identity)}' — "
            "not listed in vault.yaml participants",
            file=sys.stderr,
        )
        return 1

    # Read push refs from stdin. Bytes decoded as UTF-8 with surrogateescape,
    # as in get_changed_files, so a ref name does not depend on the locale
    # sshd forwards.
    if stdin is None:
        stdin = sys.stdin.buffer.read().decode("utf-8", "surrogateescape").split("\n")

    all_violations: list[Violation] = []
    all_changed_files: list[str] = []
    ref_reasons: list[str] = []
    try:
        branch = served_branch()
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"ERROR: the hub could not read the branch it serves ({_git_failure(e)}); "
              "please retry the push.", file=sys.stderr)
        return 1

    updates: list[tuple[str, str, str]] = []
    for line in stdin:
        # Git writes "<old> SP <new> SP <ref>". Split on those spaces only: a
        # ref name may hold NO-BREAK SPACE, NEXT LINE or LINE SEPARATOR, which
        # the no-argument split() and strip() treat as whitespace, and would
        # read `refs/heads/main<U+00A0>x` as the served branch.
        parts = line.rstrip("\n").split(" ", 2)
        if len(parts) < 3:
            # Git never writes such a line; refuse rather than let an update
            # the hook could not read go unchecked.
            if line.strip("\n"):
                ref_reasons.append(f"{line}: not a ref update the hook can read")
            continue
        oldrev, newrev, refname = parts
        updates.append((oldrev, newrev, refname))

    # An identity without `*` write may update only the served branch, and
    # git sends each ref once, so a push of more than one ref line always
    # holds a refused ref. Refuse it before any scan: each further line
    # naming the same commits would list their history again (#790's scan
    # of a refused ref is kept for a push of that ref alone).
    entry = acl.access.get(identity)
    wildcard = entry is not None and "*" in entry.write
    if not wildcard and len(ref_reasons) + len(updates) > 1:
        for oldrev, newrev, refname in updates:
            reason = ref_rejection(identity, acl, newrev, refname, branch)
            if reason is not None:
                ref_reasons.append(reason)
        if not ref_reasons:
            ref_reasons.append(f"push updates {len(updates)} refs; '{identity}' may update "
                               "only the served branch, once per push")
        _report_ref_reasons(identity, ref_reasons, log_path)
        return 1

    # One running [entries, path bytes] per listing kind for the whole push.
    scope_totals = [0, 0]
    scan_totals = [0, 0]
    for oldrev, newrev, refname in updates:
        reason = ref_rejection(identity, acl, newrev, refname, branch)
        if reason is not None:
            ref_reasons.append(reason)
            # Still scan what the ref carries (a deletion carries nothing):
            # the pusher would otherwise move the commits to the served
            # branch and be refused again for the files (#790).
            if newrev == ZERO_SHA:
                continue

        try:
            changed_files = get_changed_files(oldrev, newrev, totals=scope_totals)
            bad_frontmatter = find_bad_frontmatter(oldrev, newrev, changed_files,
                                                   totals=scan_totals)
        except ScanLimitExceeded as e:
            _report_ref_reasons(identity, ref_reasons, log_path)
            print(f"REJECTED: {e}.", file=sys.stderr)
            return 1
        except subprocess.TimeoutExpired:
            # get_changed_files runs `git log` with timeout=30. On a slow hub
            # (large repo, NFS) that can expire, raising TimeoutExpired — NOT a
            # CalledProcessError subclass, so it would escape the handler below
            # and dump a raw traceback (internal paths, stack frames) onto the
            # pusher's terminal. Reject fail-safe with a human-readable message
            # instead (#243).
            _report_ref_reasons(identity, ref_reasons, log_path)
            # After a ref refusal the push is refused whatever a retry finds,
            # and spokes read an ERROR line as "retry", so none is printed.
            if not ref_reasons:
                print(
                    f"ERROR: timed out diffing {oldrev}..{newrev} — the hub may be "
                    f"under load; please retry the push.",
                    file=sys.stderr,
                )
            return 1
        except subprocess.CalledProcessError as e:
            _report_ref_reasons(identity, ref_reasons, log_path)
            if not ref_reasons:
                print(f"ERROR: failed to diff {oldrev}..{newrev}: {e}", file=sys.stderr)
            return 1

        all_changed_files.extend(changed_files)
        violations = check_push(identity, changed_files, acl, refname)
        all_violations.extend(violations)
        all_violations.extend(Violation(identity=identity, filepath=path,
                                        scope=FRONTMATTER_SCOPE, refname=refname)
                              for path in bad_frontmatter)

    _report_ref_reasons(identity, ref_reasons, log_path)

    # Reported even after a ref refusal: the push is refused either way, and a
    # pusher told only about the ref would fix it and be refused again for
    # these (#790).
    if all_violations:
        if ref_reasons:
            print("", file=sys.stderr)
        msg = format_rejection(all_violations, branch=branch or "refs/heads/main")
        print(msg, file=sys.stderr)
        log_rejection(all_violations, log_path=log_path)

    if ref_reasons or all_violations:
        return 1

    # ACL passed — enforce rate limits. Runs after ACL so that rejected
    # pushes never consume a rate-limit slot.
    #
    # Dedupe across refs: a multi-ref push (e.g. main + feature) that
    # touches the same file in both refs would otherwise count the file
    # twice and trigger false notes_per_sync rejections. dict.fromkeys
    # preserves order for deterministic logging.
    unique_changed_files = list(dict.fromkeys(all_changed_files))
    rl_result = check_rate_limit(
        identity,
        unique_changed_files,
        acl,
        db_path=db_path,
        log_path=log_path,
        source=_push_source(),
    )
    if not rl_result.allowed:
        print(rl_result.message, file=sys.stderr)
        log_rate_limit_rejection(identity, rl_result, log_path=log_path)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
