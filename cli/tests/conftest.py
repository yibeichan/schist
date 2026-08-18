"""Shared pytest scaffolding for the CLI suite."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_stray_rate_limit_db():
    """Fail any test that leaks a rate-limit DB into the working directory.

    ``check_rate_limit`` defaults its sqlite path to
    ``$GIT_DIR/rate-limits.sqlite`` and falls back to ``.`` when GIT_DIR is
    unset, so a test that reaches it without passing ``db_path`` (and without
    pinning GIT_DIR) writes into whatever directory pytest was started from.
    That is how a 24 KB binary DB got committed to the repo root in #524 —
    .gitignore now covers the path, but an ignored file is still a live leak
    that CI would keep re-creating. Guard every test, not just the two classes
    we know about today.
    """
    stray = Path.cwd() / "rate-limits.sqlite"
    before = stray.stat().st_mtime_ns if stray.exists() else None
    yield
    if not stray.exists():
        return
    after = stray.stat().st_mtime_ns
    if before is None:
        stray.unlink()
        pytest.fail(
            f"test created {stray} — pass db_path= to check_rate_limit or "
            "pin GIT_DIR to tmp_path (see TestMain._isolate_rate_limit_db)"
        )
    if after != before:
        pytest.fail(
            f"test wrote to {stray} — pass db_path= to check_rate_limit or "
            "pin GIT_DIR to tmp_path (see TestMain._isolate_rate_limit_db)"
        )
