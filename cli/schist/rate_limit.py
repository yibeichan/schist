"""Sliding-window rate limiting for the pre-receive hook.

Enforces ``git_syncs_per_hour`` and ``notes_per_sync`` from vault.yaml.
State is persisted in ``$GIT_DIR/rate-limits.sqlite``.

Design: see ``eleven-main-design-rate-limiting-20260412.md``. Key invariants:

- DELETE + SELECT + INSERT run inside a single ``BEGIN IMMEDIATE`` transaction
  so concurrent pushes from the same identity cannot race past the limit.
- ``PRAGMA busy_timeout=5000`` on every connection so the atomic transaction
  waits out contention instead of failing immediately. WAL journal mode is
  intentionally NOT set — see ``_init_db`` for why.
- Any DB or import error fails OPEN with a stderr warning — ACL is the
  primary defense and a broken rate-limit DB must not brick the hub.
- ``notes_per_sync`` only counts files under note-bearing directories, so
  edits to ``vault.yaml`` or ``README.md`` never consume the note budget.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from schist.acl import DEFAULT_RATE_LIMITS, RateLimits, VaultACL

logger = logging.getLogger("schist.rate_limit")

SCHEMA_VERSION = "1"
WINDOW_SECONDS = 3600
BUSY_TIMEOUT_MS = 5000

# Path prefixes considered "note-bearing" for the subdirectory convention.
NOTE_DIRS = ("notes/", "papers/", "concepts/")


@dataclass
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    reason: str  # "ok", "git_syncs_per_hour", "notes_per_sync", "db_unavailable"
    limit: int = 0
    observed: int = 0
    retry_after: int = 0  # seconds until next slot (0 when allowed or N/A)
    message: str = ""  # pre-formatted stderr message for rejections


def _count_note_files(changed_files: list[str], scope_convention: str) -> int:
    """Count note-bearing files in a push.

    For ``subdirectory`` convention, only files under ``notes/``, ``papers/``,
    or ``concepts/`` are counted — this is uncheatable because non-note files
    never contribute to the count. For ``flat`` and ``multi-vault``, we fall
    back to a ``.md`` suffix filter because there is no canonical directory
    structure to key off of.
    """
    if scope_convention == "subdirectory":
        return sum(
            1 for f in changed_files
            if any(f.startswith(prefix) for prefix in NOTE_DIRS)
        )
    return sum(1 for f in changed_files if f.endswith(".md"))


def _init_db(db_path: Path) -> "sqlite3.Connection":  # noqa: F821
    """Open (and create if needed) the rate-limit sqlite DB.

    Raises any ``sqlite3`` or OS error to the caller, which is responsible
    for translating it into a fail-open result.
    """
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    # busy_timeout governs BEGIN IMMEDIATE waits below.
    #
    # We intentionally do NOT set PRAGMA journal_mode = WAL. Changing
    # journal_mode is not governed by busy_timeout — SQLite returns
    # SQLITE_BUSY immediately if any other connection has the DB open,
    # which under concurrent first-time pushes races the cold start
    # and surfaces as spurious fail-open errors. At our throughput
    # (≤10 pushes/hour/identity, serialized writes) the default
    # rollback journal is sufficient; the read-contention benefit WAL
    # would offer is not material here.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # Do all DDL + seeding inside a single IMMEDIATE transaction. Running
    # CREATE TABLE / INSERT in autocommit mode under contention can surface
    # SQLITE_BUSY immediately because SQLite refuses to wait on a lock
    # upgrade from shared→write (deadlock avoidance). BEGIN IMMEDIATE takes
    # the write lock upfront so busy_timeout governs the wait correctly.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_events ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " identity TEXT NOT NULL,"
            " ts INTEGER NOT NULL,"
            " note_count INTEGER NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_identity_ts ON sync_events (identity, ts)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            " key TEXT PRIMARY KEY,"
            " value TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return conn


def _get_limits(acl: VaultACL, identity: str) -> RateLimits:
    """Resolve rate limits for an identity, falling back to defaults."""
    configured = acl.rate_limits.get(identity)
    if configured is not None:
        return configured
    return RateLimits(
        git_syncs_per_hour=DEFAULT_RATE_LIMITS["git_syncs_per_hour"],
        mcp_writes_per_hour=DEFAULT_RATE_LIMITS["mcp_writes_per_hour"],
        notes_per_sync=DEFAULT_RATE_LIMITS["notes_per_sync"],
    )


def _fail_open(err: Exception, log_path: Path | None) -> RateLimitResult:
    """Print stderr warning + append to rejection log, return allow result.

    The log tag ``RATE_LIMIT_BYPASSED`` is intentionally alarm-triggering so
    operational dashboards can alert on repeated occurrences — a corrupt or
    unwritable rate-limit DB lets every push through uncounted and must be
    visible to humans, not just buried in "operational" noise.
    """
    print(
        f"WARNING: RATE_LIMIT_BYPASSED — rate limit DB unavailable ({err}); "
        "rate limiting DISABLED for this push",
        file=sys.stderr,
    )
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
            with open(log_path, "a") as f:
                f.write(
                    f"[{timestamp}] RATE_LIMIT_BYPASSED err={err!r}\n"
                )
        except OSError as log_err:
            logger.warning("Failed to write RATE_LIMIT_BYPASSED log: %s", log_err)
    return RateLimitResult(allowed=True, reason="db_unavailable")


def _format_rejection(
    identity: str,
    reason: str,
    limit: int,
    observed: int,
    retry_after: int,
    now: int,
) -> str:
    """Format a human-readable rejection stderr message."""
    lines = [
        f"REJECTED: rate limit exceeded ({reason}: {observed}/{limit})",
        f"Identity: {identity}",
    ]
    if retry_after > 0:
        next_ts = datetime.datetime.fromtimestamp(
            now + retry_after, tz=datetime.timezone.utc
        )
        lines.append(
            f"Retry after: {retry_after} seconds "
            f"(next slot available at {next_ts.isoformat()})"
        )
    return "\n".join(lines)


def check_rate_limit(
    identity: str,
    changed_files: list[str],
    acl: VaultACL,
    *,
    now: int | None = None,
    db_path: Path | None = None,
    log_path: Path | None = None,
) -> RateLimitResult:
    """Check git-side rate limits for a push.

    Enforces ``notes_per_sync`` (stateless) and ``git_syncs_per_hour``
    (persistent sliding window). Fails OPEN on any DB or import error.
    """
    if now is None:
        now = int(time.time())
    if db_path is None:
        git_dir = os.environ.get("GIT_DIR", ".")
        db_path = Path(git_dir) / "rate-limits.sqlite"

    limits = _get_limits(acl, identity)

    # 1. Stateless notes_per_sync check.
    note_count = _count_note_files(changed_files, acl.scope_convention)
    if note_count > limits.notes_per_sync:
        return RateLimitResult(
            allowed=False,
            reason="notes_per_sync",
            limit=limits.notes_per_sync,
            observed=note_count,
            retry_after=0,
            message=_format_rejection(
                identity, "notes_per_sync",
                limits.notes_per_sync, note_count, 0, now,
            ),
        )

    # 2. Stateful git_syncs_per_hour check (atomic DELETE/SELECT/INSERT).
    try:
        import sqlite3
    except ImportError as e:
        return _fail_open(e, log_path)

    try:
        conn = _init_db(db_path)
    except Exception as e:  # noqa: BLE001 — fail-open is an explicit design choice
        return _fail_open(e, log_path)

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM sync_events WHERE ts < ?",
                (now - WINDOW_SECONDS,),
            )
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM sync_events WHERE identity = ?",
                (identity,),
            ).fetchone()

            if count >= limits.git_syncs_per_hour:
                (oldest_ts,) = conn.execute(
                    "SELECT MIN(ts) FROM sync_events WHERE identity = ?",
                    (identity,),
                ).fetchone()
                conn.execute("ROLLBACK")
                # The DELETE filter is strict `<`, so an event at exactly
                # `now - WINDOW_SECONDS` survives this pass and is swept on
                # the next call. That makes the true "next free slot" one
                # second after `oldest_ts + WINDOW_SECONDS`, not at it. The
                # formula below gives 0 at the boundary; `max(1, ...)` is
                # therefore semantically correct, not a bug mask.
                retry_after = max(1, (oldest_ts + WINDOW_SECONDS) - now)
                return RateLimitResult(
                    allowed=False,
                    reason="git_syncs_per_hour",
                    limit=limits.git_syncs_per_hour,
                    observed=count,
                    retry_after=retry_after,
                    message=_format_rejection(
                        identity, "git_syncs_per_hour",
                        limits.git_syncs_per_hour, count, retry_after, now,
                    ),
                )

            conn.execute(
                "INSERT INTO sync_events (identity, ts, note_count) VALUES (?, ?, ?)",
                (identity, now, note_count),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error as e:
        return _fail_open(e, log_path)
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    return RateLimitResult(
        allowed=True,
        reason="ok",
        limit=limits.git_syncs_per_hour,
        observed=count + 1,
    )
