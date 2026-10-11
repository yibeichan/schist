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


def test_lock_hold_skips_the_rest_of_the_cli(tmp_path):
    # The MCP server spawns `lock hold` once per write, so its start-up time is
    # paid on every write: it must not import the command modules.
    code = (
        "import sys\n"
        "sys.argv = ['schist', '--vault', sys.argv[1], 'lock', 'hold', '--timeout', '1']\n"
        "from schist import __main__ as m\n"
        "try:\n"
        "    m.main()\n"
        "except SystemExit as e:\n"
        "    code = e.code\n"
        "heavy = sorted(n for n in ('schist.commands', 'schist.sync', 'schist.doctor', 'yaml') if n in sys.modules)\n"
        "print(code, heavy)\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    assert r.stdout.strip().splitlines()[-1] == "0 []", r.stdout + r.stderr


def test_lock_usage_errors_still_come_from_the_full_parser(tmp_path):
    run = lambda *a: subprocess.run([sys.executable, "-m", "schist", "--vault", str(tmp_path), *a],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True)
    bad = run("lock", "hold", "--timeout", "soon")
    assert bad.returncode == 2 and "invalid float value" in bad.stderr
    unknown = run("lock", "hold", "--bogus")
    assert unknown.returncode == 2 and "unrecognized arguments" in unknown.stderr
    bare = run("lock")
    assert bare.returncode == 2 and "Usage: schist lock hold" in bare.stderr
    helped = run("lock", "hold", "--help")
    assert helped.returncode == 0 and "--max-hold" in helped.stdout
    eq = run("lock", "hold", "--timeout=1", "--max-hold=5")
    assert eq.returncode == 0 and eq.stdout.strip() == "LOCKED"


def test_lock_hold_without_a_vault_reports_it(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k != "SCHIST_VAULT_PATH"}
    r = subprocess.run([sys.executable, "-m", "schist", "lock", "hold"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "--vault required" in r.stderr
