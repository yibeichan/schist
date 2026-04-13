"""Tests for the sliding-window rate limiter."""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from schist.acl import RateLimits, VaultACL, parse_vault_data
from schist.rate_limit import (
    SCHEMA_VERSION,
    WINDOW_SECONDS,
    RateLimitResult,
    _count_note_files,
    _init_db,
    check_rate_limit,
)


# ---------------------------------------------------------------------------
# Shared vault fixtures
# ---------------------------------------------------------------------------


def _make_acl(
    scope_convention: str = "subdirectory",
    rate_limits: dict | None = None,
) -> VaultACL:
    data = {
        "name": "rl-test",
        "vault_version": 1,
        "scope_convention": scope_convention,
        "participants": [
            {"name": "agent-a", "type": "agent"},
            {"name": "agent-b", "type": "agent"},
            {"name": "no-limits", "type": "agent"},
        ],
        "access": {
            "agent-a": {"read": ["*"], "write": ["*"]},
            "agent-b": {"read": ["*"], "write": ["*"]},
            "no-limits": {"read": ["*"], "write": ["*"]},
        },
    }
    if rate_limits is not None:
        data["rate_limits"] = rate_limits
    return parse_vault_data(data)


@pytest.fixture()
def acl_sub() -> VaultACL:
    return _make_acl(
        "subdirectory",
        {
            "agent-a": {"git_syncs_per_hour": 3, "notes_per_sync": 5},
            "agent-b": {"git_syncs_per_hour": 3, "notes_per_sync": 5},
        },
    )


@pytest.fixture()
def acl_flat() -> VaultACL:
    return _make_acl(
        "flat",
        {"agent-a": {"git_syncs_per_hour": 3, "notes_per_sync": 5}},
    )


@pytest.fixture()
def acl_multi() -> VaultACL:
    return _make_acl(
        "multi-vault",
        {"agent-a": {"git_syncs_per_hour": 3, "notes_per_sync": 5}},
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "rate-limits.sqlite"


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "rejected-pushes.log"


# ---------------------------------------------------------------------------
# _count_note_files — unit, no sqlite
# ---------------------------------------------------------------------------


class TestCountNoteFiles:
    def test_subdirectory_mixed(self):
        files = [
            "notes/a.md", "papers/b.md", "concepts/c.md",
            "vault.yaml", "README.md",
        ]
        assert _count_note_files(files, "subdirectory") == 3

    def test_subdirectory_only_schema(self):
        assert _count_note_files(["vault.yaml"], "subdirectory") == 0

    def test_subdirectory_nested(self):
        assert _count_note_files(["notes/2026/04/note.md"], "subdirectory") == 1

    def test_flat_md_only(self):
        files = ["a.md", "b.md", "vault.yaml"]
        assert _count_note_files(files, "flat") == 2

    def test_multi_vault_fallback(self):
        files = ["vault-a/notes/x.md", "vault-b/notes/y.md"]
        assert _count_note_files(files, "multi-vault") == 2

    def test_empty_push(self):
        assert _count_note_files([], "subdirectory") == 0

    def test_ignores_other_dirs(self):
        files = ["tools/x.md", "docs/y.md"]
        assert _count_note_files(files, "subdirectory") == 0


# ---------------------------------------------------------------------------
# _init_db — unit
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_creates_tables_and_index(self, db_path: Path):
        conn = _init_db(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert "sync_events" in tables
        assert "meta" in tables
        assert "idx_identity_ts" in indexes

    def test_does_not_set_wal_journal_mode(self, db_path: Path):
        # WAL is intentionally not set — see the _init_db comment for why.
        # Document the choice so a future "we should enable WAL" PR hits
        # this test and re-reads the reasoning first.
        conn = _init_db(db_path)
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        assert mode.lower() != "wal"

    def test_sets_busy_timeout_5000(self, db_path: Path):
        conn = _init_db(db_path)
        (timeout,) = conn.execute("PRAGMA busy_timeout").fetchone()
        conn.close()
        assert timeout == 5000

    def test_writes_schema_version_1(self, db_path: Path):
        conn = _init_db(db_path)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        conn.close()
        assert row == (SCHEMA_VERSION,)

    def test_idempotent_on_reopen(self, db_path: Path):
        conn1 = _init_db(db_path)
        conn1.execute(
            "INSERT INTO sync_events (identity, ts, note_count) VALUES (?, ?, ?)",
            ("x", 123, 1),
        )
        conn1.close()
        conn2 = _init_db(db_path)
        rows = conn2.execute("SELECT identity, ts FROM sync_events").fetchall()
        conn2.close()
        assert rows == [("x", 123)]

    def test_permission_error_raises(self, tmp_path: Path):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            with pytest.raises((OSError, sqlite3.OperationalError)):
                _init_db(readonly / "subdir" / "db.sqlite")
        finally:
            readonly.chmod(0o700)

    def test_corrupt_file_raises(self, db_path: Path):
        db_path.write_bytes(b"this is not a sqlite database" * 20)
        with pytest.raises(sqlite3.DatabaseError):
            _init_db(db_path)


# ---------------------------------------------------------------------------
# check_rate_limit — happy path & boundaries
# ---------------------------------------------------------------------------


class TestCheckRateLimitHappyPath:
    def test_first_event_allowed(self, acl_sub, db_path, log_path):
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True
        assert result.reason == "ok"

    def test_under_limit_allowed(self, acl_sub, db_path, log_path):
        for i in range(2):
            result = check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
            assert result.allowed is True

    def test_at_limit_minus_one_allowed(self, acl_sub, db_path, log_path):
        # Limit is 3, fill 2, third push should still be allowed.
        for i in range(2):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1002, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True

    def test_at_limit_rejected(self, acl_sub, db_path, log_path):
        # Fill exactly 3 slots, 4th push is rejected.
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1003, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is False
        assert result.reason == "git_syncs_per_hour"
        assert result.observed == 3
        assert result.limit == 3

    def test_at_limit_plus_one_rejected(self, acl_sub, db_path, log_path):
        # Hit limit then try twice more — both rejected, count never exceeds 3.
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        r1 = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1003, db_path=db_path, log_path=log_path,
        )
        r2 = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1004, db_path=db_path, log_path=log_path,
        )
        assert r1.allowed is False
        assert r2.allowed is False
        # Verify the count didn't grow beyond the limit — still 3 rows.
        conn = sqlite3.connect(str(db_path))
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM sync_events WHERE identity = ?", ("agent-a",)
        ).fetchone()
        conn.close()
        assert n == 3

    def test_stale_events_swept_and_allowed(self, acl_sub, db_path, log_path):
        # Fill to limit at t=1000, then push at t=1000+3601 (past window).
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        # All three events are > 3600s old; new push should sweep them.
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000 + 3601 + 5, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True

    def test_ts_equals_now_minus_3600_still_in_window(self, acl_sub, db_path, log_path):
        # DELETE uses strict < (now - 3600), so ts == now - 3600 survives.
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000 + WINDOW_SECONDS, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is False

    def test_ts_equals_now_minus_3601_swept(self, acl_sub, db_path, log_path):
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000 + WINDOW_SECONDS + 1, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# check_rate_limit — notes_per_sync
# ---------------------------------------------------------------------------


class TestNotesPerSync:
    def test_notes_at_limit_allowed(self, acl_sub, db_path, log_path):
        files = [f"notes/{i}.md" for i in range(5)]  # limit is 5
        result = check_rate_limit(
            "agent-a", files, acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True

    def test_notes_over_limit_rejected(self, acl_sub, db_path, log_path):
        files = [f"notes/{i}.md" for i in range(6)]
        result = check_rate_limit(
            "agent-a", files, acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is False
        assert result.reason == "notes_per_sync"
        assert result.observed == 6
        assert result.limit == 5

    def test_notes_zero_allowed(self, acl_sub, db_path, log_path):
        # vault.yaml-only push; no notes counted.
        result = check_rate_limit(
            "agent-a", ["vault.yaml"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True

    def test_notes_subdirectory_path_prefix_only(self, acl_sub, db_path, log_path):
        # 5 real notes + 100 junk files at root — still counted as 5.
        files = [f"notes/{i}.md" for i in range(5)] + [
            f"junk{i}.md" for i in range(100)
        ]
        result = check_rate_limit(
            "agent-a", files, acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True


# ---------------------------------------------------------------------------
# check_rate_limit — defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_identity_without_rate_limits_uses_defaults(self, db_path, log_path):
        acl = _make_acl("subdirectory")  # no rate_limits block at all
        # Default git_syncs_per_hour is 10 — 10 pushes allowed, 11th rejected.
        for i in range(10):
            r = check_rate_limit(
                "no-limits", ["notes/a.md"], acl,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
            assert r.allowed is True
        r = check_rate_limit(
            "no-limits", ["notes/a.md"], acl,
            now=1010, db_path=db_path, log_path=log_path,
        )
        assert r.allowed is False
        assert r.limit == 10


# ---------------------------------------------------------------------------
# check_rate_limit — retry-after hint
# ---------------------------------------------------------------------------


class TestRetryAfter:
    def test_rejection_includes_retry_after(self, acl_sub, db_path, log_path):
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1003, db_path=db_path, log_path=log_path,
        )
        assert result.retry_after > 0
        assert "Retry after" in result.message

    def test_rejection_retry_after_matches_oldest_event(self, acl_sub, db_path, log_path):
        # Oldest event at ts=1000, now=1500 → oldest slot frees at 1000+3600=4600
        # retry_after should be 4600 - 1500 = 3100.
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1500, db_path=db_path, log_path=log_path,
        )
        assert result.retry_after == 1000 + WINDOW_SECONDS - 1500

    def test_rejection_includes_iso_timestamp(self, acl_sub, db_path, log_path):
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1003, db_path=db_path, log_path=log_path,
        )
        # ISO 8601 "T" separator appears in fromtimestamp().isoformat() output.
        assert "T" in result.message
        assert "Z" in result.message or "+00:00" in result.message


# ---------------------------------------------------------------------------
# check_rate_limit — fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_fail_open_on_corrupt_db(self, acl_sub, db_path, log_path, capsys):
        db_path.write_bytes(b"not a sqlite db" * 100)
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True
        assert result.reason == "db_unavailable"
        stderr = capsys.readouterr().err
        assert "WARNING" in stderr
        assert "rate limit DB unavailable" in stderr

    def test_fail_open_on_permission_error(self, acl_sub, tmp_path, log_path, capsys):
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            result = check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000,
                db_path=readonly / "sub" / "db.sqlite",
                log_path=log_path,
            )
        finally:
            readonly.chmod(0o700)
        assert result.allowed is True
        assert result.reason == "db_unavailable"
        stderr = capsys.readouterr().err
        assert "WARNING" in stderr

    def test_fail_open_on_sqlite3_importerror(
        self, acl_sub, db_path, log_path, capsys,
    ):
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sqlite3":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            # Drop cached sqlite3 so the import inside check_rate_limit re-runs.
            saved = sys.modules.pop("sqlite3", None)
            try:
                result = check_rate_limit(
                    "agent-a", ["notes/a.md"], acl_sub,
                    now=1000, db_path=db_path, log_path=log_path,
                )
            finally:
                if saved is not None:
                    sys.modules["sqlite3"] = saved
        assert result.allowed is True
        assert result.reason == "db_unavailable"
        stderr = capsys.readouterr().err
        assert "WARNING" in stderr

    def test_fail_open_on_busy_timeout_exhausted(
        self, acl_sub, db_path, log_path, capsys, monkeypatch,
    ):
        # Use a very short busy_timeout so the test is fast.
        import schist.rate_limit as rl_mod
        monkeypatch.setattr(rl_mod, "BUSY_TIMEOUT_MS", 100)

        # Prime the DB, then hold an exclusive lock from another connection.
        _init_db(db_path).close()
        blocker = sqlite3.connect(str(db_path))
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            result = check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=1000, db_path=db_path, log_path=log_path,
            )
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        assert result.allowed is True
        assert result.reason == "db_unavailable"
        stderr = capsys.readouterr().err
        assert "WARNING" in stderr

    def test_fail_open_prints_stderr_warning(self, acl_sub, db_path, log_path, capsys):
        db_path.write_bytes(b"garbage" * 200)
        check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        stderr = capsys.readouterr().err
        assert "WARNING: rate limit DB unavailable" in stderr
        assert "DISABLED for this push" in stderr

    def test_fail_open_logs_to_rejected_pushes_log(
        self, acl_sub, db_path, log_path, capsys,
    ):
        db_path.write_bytes(b"garbage" * 200)
        check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert log_path.exists()
        content = log_path.read_text()
        assert "rate_limit_db_failure" in content


# ---------------------------------------------------------------------------
# check_rate_limit — clock edge cases
# ---------------------------------------------------------------------------


class TestClockEdgeCases:
    def test_clock_rollback_harmless(self, acl_sub, db_path, log_path):
        # Store events at t=5000, then inject now=2000 (clock rolled back).
        # DELETE filter `ts < 2000 - 3600 = -1600` sweeps nothing. Count is 3,
        # so a 4th push is rejected — no false allows.
        for i in range(3):
            check_rate_limit(
                "agent-a", ["notes/a.md"], acl_sub,
                now=5000 + i, db_path=db_path, log_path=log_path,
            )
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=2000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is False

    def test_integer_timestamp_boundary(self, acl_sub, db_path, log_path):
        # Pass a float now — the function coerces to int internally.
        result = check_rate_limit(
            "agent-a", ["notes/a.md"], acl_sub,
            now=1000, db_path=db_path, log_path=log_path,
        )
        assert result.allowed is True
        conn = sqlite3.connect(str(db_path))
        (ts,) = conn.execute("SELECT ts FROM sync_events").fetchone()
        conn.close()
        assert ts == 1000
        assert isinstance(ts, int)


# ---------------------------------------------------------------------------
# Concurrency — real threads
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_pushes_same_identity_serialize(
        self, acl_sub, db_path, log_path,
    ):
        """Two threads race to consume the same identity's budget.

        With BEGIN IMMEDIATE + busy_timeout, the total admitted must never
        exceed the configured limit even under parallel pressure.
        """
        limit = 3
        attempts_per_thread = 25
        barrier = threading.Barrier(2)
        results = {"a": [], "b": []}

        def worker(label: str, start_now: int) -> None:
            barrier.wait()
            for i in range(attempts_per_thread):
                r = check_rate_limit(
                    "agent-a", ["notes/x.md"], acl_sub,
                    now=start_now + i, db_path=db_path, log_path=log_path,
                )
                results[label].append(r.allowed)

        # Both threads operate inside the same hour window, so stale sweep
        # never fires and the limiter state is shared.
        t1 = threading.Thread(target=worker, args=("a", 1000))
        t2 = threading.Thread(target=worker, args=("b", 1500))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        total_allowed = sum(results["a"]) + sum(results["b"])
        assert total_allowed <= limit, (
            f"admitted {total_allowed} > limit {limit}"
        )

    def test_concurrent_pushes_different_identities_do_not_interfere(
        self, acl_sub, db_path, log_path,
    ):
        """agent-a and agent-b each have their own 3-slot budget."""
        results = {"a": [], "b": []}
        barrier = threading.Barrier(2)

        def worker(identity: str, label: str) -> None:
            barrier.wait()
            for i in range(3):
                r = check_rate_limit(
                    identity, ["notes/x.md"], acl_sub,
                    now=1000 + i, db_path=db_path, log_path=log_path,
                )
                results[label].append(r.allowed)

        t1 = threading.Thread(target=worker, args=("agent-a", "a"))
        t2 = threading.Thread(target=worker, args=("agent-b", "b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert all(results["a"])
        assert all(results["b"])
