# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for stale_temps: reaping atomic-write temps orphaned in .schist/tmp (#448)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from schist import stale_temps
from schist.stale_temps import (
    ANY_AGE,
    DEAD_PID_MIN_AGE,
    _pid_alive,
    find_stale_temps,
    reap_stale_temps,
)

NOW = 2_000_000_000.0
HEX = "0123456789ab"


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _stage(tmp_path: Path) -> Path:
    d = tmp_path / ".schist" / "tmp"
    d.mkdir(parents=True)
    return d


def _make(d: Path, name: str, age: float) -> Path:
    p = d / name
    p.write_text("x")
    os.utime(p, (NOW - age, NOW - age))
    return p


def _alive(_pid: int) -> bool:
    return True


def _dead(_pid: int) -> bool:
    return False


def _names(found) -> list[str]:
    return [s.path.name for s in found]


class TestNameFormats:
    def test_cli_writer_names_are_matched(self, tmp_path):
        # The real CLI writer's name format: pins the pattern to mkstemp.
        d = _stage(tmp_path)
        fd, real = tempfile.mkstemp(dir=d, suffix=".tmp")
        os.close(fd)
        os.utime(real, (NOW - ANY_AGE - 10, NOW - ANY_AGE - 10))
        assert _names(find_stale_temps(tmp_path, now=NOW)) == [Path(real).name]

    def test_mcp_writer_name_is_matched_with_pid(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, f".note.md.4242.{HEX}.tmp", DEAD_PID_MIN_AGE + 5)
        found = find_stale_temps(tmp_path, now=NOW, pid_alive=_dead)
        assert [(s.path.name, s.pid) for s in found] == [(f".note.md.4242.{HEX}.tmp", 4242)]

    @pytest.mark.parametrize("name", [
        "notes.tmp",
        "tmpabcdefgh.tmp\n",           # trailing newline must not match
        "tmpabcdefg.tmp",               # 7 chars
        "tmpABCDEFGH.tmp",              # uppercase
        "tmp\xe9bcdefgh.tmp",           # non-ASCII
        "tmpabcdefgh.tmp.bak",
        f".note.md.4242.{HEX.upper()}.tmp",
        f".note.md.4242.{HEX[:-1]}.tmp",
        f"note.md.4242.{HEX}.tmp",      # no leading dot
        f".n\xe9te.md.4242.{HEX}.tmp",  # non-ASCII basename
        f".note.md.\N{ARABIC-INDIC DIGIT FOUR}242.{HEX}.tmp",  # non-ASCII pid digit
        f".note.md.x.{HEX}.tmp",
        f"..{HEX}.tmp",
    ])
    def test_foreign_names_are_ignored(self, tmp_path, name):
        d = _stage(tmp_path)
        _make(d, name, ANY_AGE * 10)
        assert find_stale_temps(tmp_path, now=NOW, pid_alive=_dead) == []


class TestStaleRule:
    def test_dead_pid_and_old_enough(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, f".a.md.99.{HEX}.tmp", DEAD_PID_MIN_AGE + 1)
        assert len(find_stale_temps(tmp_path, now=NOW, pid_alive=_dead)) == 1

    def test_dead_pid_but_young_is_kept(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, f".a.md.99.{HEX}.tmp", DEAD_PID_MIN_AGE - 1)
        assert find_stale_temps(tmp_path, now=NOW, pid_alive=_dead) == []

    def test_live_pid_and_old_but_under_any_age_is_kept(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, f".a.md.99.{HEX}.tmp", ANY_AGE - 1)
        assert find_stale_temps(tmp_path, now=NOW, pid_alive=_alive) == []

    def test_age_alone_suffices_over_any_age(self, tmp_path):
        # Another node may own the pid: a "live" pid here proves nothing.
        d = _stage(tmp_path)
        _make(d, f".a.md.99.{HEX}.tmp", ANY_AGE + 1)
        assert len(find_stale_temps(tmp_path, now=NOW, pid_alive=_alive)) == 1

    def test_cli_format_has_no_pid_and_waits_for_any_age(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, "tmpabcdefgh.tmp", ANY_AGE - 1)
        _make(d, "tmpabcdefgi.tmp", ANY_AGE + 1)
        assert _names(find_stale_temps(tmp_path, now=NOW, pid_alive=_dead)) == ["tmpabcdefgi.tmp"]

    def test_future_mtime_is_kept(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, "tmpabcdefgh.tmp", -ANY_AGE * 5)
        assert find_stale_temps(tmp_path, now=NOW) == []

    def test_pid_alive_probe(self):
        assert _pid_alive(os.getpid()) is True
        assert _pid_alive(_dead_pid()) is False
        assert _pid_alive(0) is True        # never signal a process group
        assert _pid_alive(-5) is True
        assert _pid_alive(10**12) is True   # unrepresentable pid: unknown, keep


class TestNeverTouchesOthers:
    def test_symlink_is_not_followed_or_removed(self, tmp_path):
        d = _stage(tmp_path)
        target = tmp_path / "precious.md"
        target.write_text("keep")
        os.utime(target, (NOW - ANY_AGE * 5, NOW - ANY_AGE * 5))
        link = d / "tmpabcdefgh.tmp"
        link.symlink_to(target)
        assert find_stale_temps(tmp_path, now=NOW) == []
        assert reap_stale_temps(tmp_path, now=NOW) == []
        assert link.is_symlink() and target.read_text() == "keep"

    def test_directory_with_a_matching_name_is_kept(self, tmp_path):
        d = _stage(tmp_path)
        sub = d / "tmpabcdefgh.tmp"
        sub.mkdir()
        (sub / "inner").write_text("x")
        os.utime(sub, (NOW - ANY_AGE * 5, NOW - ANY_AGE * 5))
        assert reap_stale_temps(tmp_path, now=NOW) == []
        assert (sub / "inner").exists()

    def test_other_files_in_the_directory_are_kept(self, tmp_path):
        d = _stage(tmp_path)
        other = _make(d, "scratch.txt", ANY_AGE * 5)
        mine = _make(d, "tmpabcdefgh.tmp", ANY_AGE * 5)
        assert reap_stale_temps(tmp_path, now=NOW) == [mine]
        assert other.exists() and not mine.exists()

    def test_nothing_outside_the_staging_dir_is_touched(self, tmp_path):
        _stage(tmp_path)
        (tmp_path / "notes").mkdir()
        note = _make(tmp_path / "notes", "tmpabcdefgh.tmp", ANY_AGE * 5)
        assert reap_stale_temps(tmp_path, now=NOW) == []
        assert note.exists()


class TestNeverRaises:
    def test_missing_directory(self, tmp_path):
        assert find_stale_temps(tmp_path, now=NOW) == []
        assert reap_stale_temps(tmp_path, now=NOW) == []

    def test_staging_path_is_a_file(self, tmp_path):
        (tmp_path / ".schist").mkdir()
        (tmp_path / ".schist" / "tmp").write_text("x")
        assert reap_stale_temps(tmp_path, now=NOW) == []

    def test_one_failing_unlink_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        d = _stage(tmp_path)
        a = _make(d, "tmpaaaaaaaa.tmp", ANY_AGE * 5)
        b = _make(d, "tmpbbbbbbbb.tmp", ANY_AGE * 5)
        real = os.unlink

        def flaky(p, *args, **kw):
            if str(p).endswith("tmpaaaaaaaa.tmp"):
                raise PermissionError(13, "denied")
            return real(p, *args, **kw)

        monkeypatch.setattr(stale_temps.os, "unlink", flaky)
        assert reap_stale_temps(tmp_path, now=NOW) == [b]
        assert a.exists() and not b.exists()

    def test_one_failing_lstat_does_not_stop_the_rest(self, tmp_path, monkeypatch):
        d = _stage(tmp_path)
        _make(d, "tmpaaaaaaaa.tmp", ANY_AGE * 5)
        b = _make(d, "tmpbbbbbbbb.tmp", ANY_AGE * 5)
        real = os.lstat

        def flaky(p, *args, **kw):
            if str(p).endswith("tmpaaaaaaaa.tmp"):
                raise OSError(5, "io error")
            return real(p, *args, **kw)

        monkeypatch.setattr(stale_temps.os, "lstat", flaky)
        assert _names(find_stale_temps(tmp_path, now=NOW)) == [b.name]

    def test_a_raising_pid_probe_does_not_stop_the_rest(self, tmp_path):
        d = _stage(tmp_path)
        _make(d, f".a.md.1.{HEX}.tmp", DEAD_PID_MIN_AGE + 1)
        _make(d, "tmpbbbbbbbb.tmp", ANY_AGE * 5)

        def boom(_pid):
            raise RuntimeError("probe failed")

        assert _names(find_stale_temps(tmp_path, now=NOW, pid_alive=boom)) == ["tmpbbbbbbbb.tmp"]
