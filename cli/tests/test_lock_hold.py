import subprocess, sys, time
from schist import git_ops

def _helper(vault, *extra, stdin=subprocess.PIPE):
    return subprocess.Popen([sys.executable, "-m", "schist", "--vault", vault, "lock", "hold", *extra],
                            stdin=stdin, stdout=subprocess.PIPE, text=True)

def test_locked_then_release_on_eof(tmp_path):
    p = _helper(str(tmp_path))
    assert p.stdout.readline().strip() == "LOCKED"
    try:
        with git_ops.vault_write_lock(str(tmp_path), timeout=0.3):
            raise AssertionError("lock should be held by helper")
    except git_ops.VaultLockTimeout:
        pass
    p.stdin.close()
    assert p.wait(5) == 0
    with git_ops.vault_write_lock(str(tmp_path), timeout=1):
        pass

def test_devnull_stdin_releases_immediately(tmp_path):
    p = _helper(str(tmp_path), stdin=subprocess.DEVNULL)
    assert p.stdout.readline().strip() == "LOCKED"
    assert p.wait(5) == 0

def test_timeout_exit_75_names_holder(tmp_path):
    a = _helper(str(tmp_path)); assert a.stdout.readline().strip() == "LOCKED"
    b = _helper(str(tmp_path), "--timeout", "0.5")
    out = b.stdout.readline().strip()
    assert b.wait(5) == 75
    assert out.startswith("TIMEOUT holder=") and out.endswith(f":{a.pid}")
    a.stdin.close(); a.wait(5)

def test_max_hold_exit_76(tmp_path):
    p = _helper(str(tmp_path), "--max-hold", "0.5")
    assert p.stdout.readline().strip() == "LOCKED"
    assert p.stdout.readline().strip() == "EXPIRED"
    assert p.wait(5) == 76
    with git_ops.vault_write_lock(str(tmp_path), timeout=1):
        pass

def test_hidden_from_help():
    # argparse.SUPPRESS still lists the choice in the {...} usage string, but
    # no help line may describe `lock`.
    out = subprocess.run([sys.executable, "-m", "schist", "--help"], capture_output=True, text=True).stdout
    assert not any(line.strip().startswith("lock ") for line in out.splitlines())
