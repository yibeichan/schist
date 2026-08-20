"""Shared pytest scaffolding for the CLI suite."""

import os
from pathlib import Path

import pytest

# Runtime artifacts whose default location is derived from $GIT_DIR, falling
# back to the CWD when it is unset: the rate-limit DB (rate_limit.py) and the
# pre-receive audit log (pre_receive.py). A test that reaches either writer
# without passing an explicit path — and without pinning GIT_DIR — drops the
# file into whatever directory pytest was started from. That is how a 24 KB
# binary DB got committed to the repo root in #524. .gitignore now covers both
# names, but an ignored file is still a live leak that CI keeps re-creating,
# so fail the test that produces one instead of hiding it.
_ARTIFACTS = ("rate-limits.sqlite", "rejected-pushes.log")

_HINT = ("pass an explicit db_path=/log_path= or pin GIT_DIR to tmp_path "
         "(see TestMain._isolate_rate_limit_db)")


def _repo_root() -> Path:
    """Repo root, found by walking up to the directory holding `.git`.

    Not `parents[2]`: that hardcodes this file's depth, so moving the conftest
    up to `cli/` would silently retarget the guard at the parent of the repo —
    it would stop detecting repo-root leaks and start unlinking files outside
    the checkout.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / ".git").exists():
            return candidate
    return here.parents[2]


def _snapshot(cwd: Path) -> dict[Path, int]:
    """mtime per existing artifact path in the directories we watch.

    Absent paths are omitted rather than stored as None, so "unknown to the
    snapshot" and "did not exist" stay distinguishable at teardown.
    """
    root = _repo_root()
    # The audit log defaults to `$GIT_DIR/hooks/`, the DB to `$GIT_DIR` itself,
    # so watch each base directory and its `hooks/` child. This is a fixed set,
    # not a repo-wide sweep: it catches the GIT_DIR-unset fallback (the leak
    # that actually happened), not a test that points GIT_DIR at some other
    # directory inside the checkout.
    bases = {cwd, root, root / "cli"}
    snap: dict[Path, int] = {}
    for d in bases | {b / "hooks" for b in bases}:
        for name in _ARTIFACTS:
            p = d / name
            try:
                snap[p] = p.stat().st_mtime_ns
            except OSError:
                # Absent, or a path we cannot stat (dir, permissions, a
                # concurrent unlink). Either way: nothing to compare against.
                continue
    return snap


@pytest.fixture(autouse=True)
def _no_stray_runtime_artifacts():
    """Fail any test that leaks a GIT_DIR-derived artifact into the tree.

    Only the CWD as of setup is watched, not as of teardown: `monkeypatch.chdir`
    (the sole chdir idiom in this suite) restores the CWD before this fixture
    resumes, so a teardown-time CWD adds nothing for the realistic case — while
    for a raw `os.chdir` it would add a directory the setup snapshot never saw,
    where a pre-existing file looks indistinguishable from one this test just
    created and would be wrongly deleted.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        # Workers share one filesystem, so one worker's leak surfaces in
        # another's teardown: the innocent test gets blamed AND the file is
        # unlinked under the leaking test's feet. Serial runs (CI) do the
        # detecting; don't turn a parallel run into two bogus failures.
        yield
        return
    cwd = Path.cwd().resolve()
    before = _snapshot(cwd)
    yield
    problems: list[str] = []
    for path, now in _snapshot(cwd).items():
        was = before.get(path)
        if was is None:
            problems.append(f"created {path}")
            try:
                path.unlink()
            except OSError:
                pass
        elif now != was:
            problems.append(f"wrote to {path}")
    if problems:
        pytest.fail(f"test {'; '.join(problems)} — {_HINT}")


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch, tmp_path_factory):
    """Pin git's global/system config away from the developer's own.

    Nothing in this suite isolated it, so any ambient `core.hooksPath` — which
    a pre-commit-framework user has set as a matter of course — leaked into
    every check that shells out to git. With one set, 8 tests failed, and the
    failures were not the suite's fault so much as evidence that the checks
    were reading config discovered outside the repo under test.

    Also keeps a test that fabricates a repo from being silently answered by
    an ancestor repository's config: this source tree is itself a git repo, so
    `git -C <tmp>` walking upward is a real path when tmp is not isolated.
    """
    empty = tmp_path_factory.mktemp("gitconfig") / "empty"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
