# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

import errno, fcntl, os, subprocess, sys, threading, time
import pytest
from schist import git_ops

def _hold_in_child(vault, seconds):
    code = (
        "import sys,time; from schist import git_ops\n"
        "with git_ops.vault_write_lock(sys.argv[1]):\n"
        "    print('held', flush=True); time.sleep(float(sys.argv[2]))\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code, vault, str(seconds)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p

def test_lock_file_is_write_lock(tmp_path):
    with git_ops.vault_write_lock(str(tmp_path)):
        assert (tmp_path / ".schist" / "write.lock").exists()


def _flock_is_held(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


def test_also_holds_the_legacy_cli_lock_for_older_clis(tmp_path):
    # Upgrade window: CLIs from before #724 serialize on cli-write.lock only.
    legacy = tmp_path / ".schist" / "cli-write.lock"
    with git_ops.vault_write_lock(str(tmp_path)):
        assert _flock_is_held(legacy)
        assert _flock_is_held(tmp_path / ".schist" / "write.lock")
    assert not _flock_is_held(legacy)


def test_waits_for_an_older_cli_on_the_legacy_lock(tmp_path):
    (tmp_path / ".schist").mkdir()
    code = (
        "import fcntl,os,sys,time\n"
        "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(5)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / ".schist" / "cli-write.lock")],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        with pytest.raises(git_ops.VaultLockTimeout) as ei:
            with git_ops.vault_write_lock(str(tmp_path), timeout=0.5):
                pass
        assert "older schist CLI" in ei.value.holder
        # The new lock is not left held after the timeout.
        assert not _flock_is_held(tmp_path / ".schist" / "write.lock")
    finally:
        p.kill(); p.wait()

def test_holder_note_written(tmp_path):
    import socket
    with git_ops.vault_write_lock(str(tmp_path)):
        assert git_ops.read_holder(str(tmp_path)) == f"{socket.gethostname()}:{os.getpid()}"

def test_reentrant_same_process(tmp_path):
    with git_ops.vault_write_lock(str(tmp_path)):
        with git_ops.vault_write_lock(str(tmp_path), timeout=0.5):
            pass  # must not deadlock or time out

def test_timeout_names_holder(tmp_path):
    p = _hold_in_child(str(tmp_path), 5)
    try:
        t0 = time.monotonic()
        with pytest.raises(git_ops.VaultLockTimeout) as ei:
            with git_ops.vault_write_lock(str(tmp_path), timeout=0.5):
                pass
        assert time.monotonic() - t0 < 3
        assert ei.value.holder.endswith(f":{p.pid}")
    finally:
        p.kill(); p.wait()

def test_waits_then_acquires(tmp_path):
    p = _hold_in_child(str(tmp_path), 0.5)
    with git_ops.vault_write_lock(str(tmp_path), timeout=5):
        pass
    p.wait()

def _readonly_flock_works(directory):
    """False where flock needs a writable fd: the Linux NFS client emulates
    flock with a POSIX write lock, so LOCK_EX on an O_RDONLY fd is EBADF."""
    probe = directory / "readonly-flock-probe"
    probe.write_text("")
    fd = os.open(probe, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno == errno.EBADF:
            return False
        raise
    finally:
        os.close(fd)
        probe.unlink()


def test_unwritable_lock_file_still_locks(tmp_path):
    d = tmp_path / ".schist"; d.mkdir()
    lock = d / "write.lock"; lock.write_text(""); lock.chmod(0o444)
    if os.access(lock, os.W_OK):
        pytest.skip("running as root")
    if not _readonly_flock_works(d):
        pytest.skip("flock needs a writable fd here (NFS); see the next test")
    with git_ops.vault_write_lock(str(tmp_path)):
        fd = os.open(lock, os.O_RDONLY)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)


@pytest.mark.parametrize("name", ["write.lock", "cli-write.lock"])
def test_unwritable_lock_file_where_flock_needs_write_access(tmp_path, monkeypatch, capsys, name):
    # #748: on NFS a read-only fd cannot take the lock, so a writer that can
    # only open a lock file read-only gets a message naming the file and the
    # permission it needs, not an OSError traceback. On a local filesystem
    # the NFS behaviour is simulated; on NFS it is the real one.
    d = tmp_path / ".schist"; d.mkdir()
    lock = d / name; lock.write_text(""); lock.chmod(0o444)
    if os.access(lock, os.W_OK):
        pytest.skip("running as root")
    if _readonly_flock_works(d):
        real_flock = fcntl.flock

        def nfs_flock(fd, op):
            if op & (fcntl.LOCK_EX | fcntl.LOCK_SH) and \
                    fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))
            return real_flock(fd, op)
        monkeypatch.setattr(fcntl, "flock", nfs_flock)
    with pytest.raises(SystemExit) as ei:
        with git_ops.vault_write_lock(str(tmp_path)):
            pytest.fail("entered the lock through a read-only fd that cannot lock")
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert str(lock) in err
    assert "write access" in err
    assert "Traceback" not in err
    # Nothing is left held: once the file is writable, the lock works.
    monkeypatch.undo()
    lock.chmod(0o644)
    with git_ops.vault_write_lock(str(tmp_path), timeout=1):
        pass


# #745 — re-entry is per thread. A second thread of the same process must wait
# for the first, not re-enter the lock without it.

def _hold_in_thread(vault):
    held, release = threading.Event(), threading.Event()
    errors = []

    def run():
        try:
            with git_ops.vault_write_lock(vault):
                held.set()
                release.wait(10)
        except BaseException as e:  # surfaced by the caller
            errors.append(e)
            held.set()

    t = threading.Thread(target=run)
    t.start()
    assert held.wait(10)
    return t, release, errors


def test_second_thread_waits_and_times_out(tmp_path):
    import socket
    t, release, errors = _hold_in_thread(str(tmp_path))
    try:
        with pytest.raises(git_ops.VaultLockTimeout) as ei:
            with git_ops.vault_write_lock(str(tmp_path), timeout=0.3):
                pass
        assert ei.value.holder.startswith(f"{socket.gethostname()}:{os.getpid()}")
    finally:
        release.set(); t.join(10)
    assert not errors
    # Once the first thread lets go, this one gets it.
    with git_ops.vault_write_lock(str(tmp_path), timeout=1):
        pass


def test_waiting_thread_never_opens_the_lock_file(tmp_path, monkeypatch):
    # On NFS, closing any fd on write.lock drops the POSIX lock the holding
    # thread has, so a thread waiting on the in-process lock must neither open
    # the file nor read the holder note from it.
    t, release, errors = _hold_in_thread(str(tmp_path))
    opened = []
    real_open = git_ops._open_lock_file
    monkeypatch.setattr(git_ops, "_open_lock_file",
                        lambda p: (opened.append(p), real_open(p))[1])
    monkeypatch.setattr(git_ops, "read_holder",
                        lambda v: pytest.fail("read_holder called while this process holds the lock"))
    try:
        with pytest.raises(git_ops.VaultLockTimeout):
            with git_ops.vault_write_lock(str(tmp_path), timeout=0.3):
                pass
        assert opened == []
        assert _flock_is_held(tmp_path / ".schist" / "write.lock")
    finally:
        release.set(); t.join(10)
    assert not errors


def test_threads_take_turns(tmp_path):
    inside, overlaps, errors = [], [], []

    def run():
        try:
            for _ in range(5):
                with git_ops.vault_write_lock(str(tmp_path)):
                    inside.append(1)
                    if len(inside) > 1:
                        overlaps.append(1)
                    time.sleep(0.01)
                    with git_ops.vault_write_lock(str(tmp_path), timeout=0.5):
                        pass  # re-entry in the same thread still works
                    inside.pop()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert not overlaps
    assert not _flock_is_held(tmp_path / ".schist" / "write.lock")


@pytest.mark.parametrize("timeout", [1e10, float("inf")])
def test_timeout_beyond_lock_acquire_range(tmp_path, timeout):
    # threading.Lock.acquire raises OverflowError above TIMEOUT_MAX; the lock
    # must still accept any timeout the flock wait does.
    with git_ops.vault_write_lock(str(tmp_path), timeout=timeout):
        pass
