import fcntl, os, subprocess, sys, threading, time
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
    assert not (tmp_path / ".schist" / "cli-write.lock").exists()

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

def test_unwritable_lock_file_still_locks(tmp_path):
    d = tmp_path / ".schist"; d.mkdir()
    lock = d / "write.lock"; lock.write_text(""); lock.chmod(0o444)
    if os.access(lock, os.W_OK):
        pytest.skip("running as root")
    with git_ops.vault_write_lock(str(tmp_path)):
        fd = os.open(lock, os.O_RDONLY)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
