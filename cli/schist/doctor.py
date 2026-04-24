"""schist doctor — health check for schist setup."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .spoke_config import is_spoke, load_spoke_config

MIN_PYTHON = (3, 12)
MIN_NODE = (20, 0, 0)
MIN_GIT = (2, 30)


@dataclass
class CheckResult:
    status: str  # PASS, FAIL, WARN, SKIP
    label: str
    message: str
    fix: Optional[str] = None


def _parse_semver(s: str) -> tuple[int, ...]:
    parts = s.lstrip("v").split(".")
    return tuple(int(p) for p in parts if p.isdigit())


def check_python() -> CheckResult:
    ver = sys.version_info[:3]
    ok = ver >= MIN_PYTHON
    return CheckResult(
        status="PASS" if ok else "FAIL",
        label="Python",
        message=f"{ver[0]}.{ver[1]}.{ver[2]}",
        fix=None if ok else "Install Python 3.12+ from python.org or your package manager.",
    )


def check_node() -> CheckResult:
    node = shutil.which("node")
    if not node:
        return CheckResult("FAIL", "Node.js", "not found",
                           "Install Node.js 20+ from nodejs.org or via nvm.")
    try:
        raw = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
        ver_str = raw.stdout.strip()
        ver = _parse_semver(ver_str)
        ok = ver >= MIN_NODE
        return CheckResult(
            status="PASS" if ok else "FAIL",
            label="Node.js",
            message=ver_str,
            fix=None if ok else "Install Node.js 20+ from nodejs.org or via nvm.",
        )
    except Exception as e:
        return CheckResult("FAIL", "Node.js", f"error: {e}",
                           "Install Node.js 20+ from nodejs.org or via nvm.")


def check_git() -> CheckResult:
    git = shutil.which("git")
    if not git:
        return CheckResult("FAIL", "Git", "not found",
                           "Install Git 2.30+.")
    try:
        raw = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=5)
        ver_str = raw.stdout.strip().split(" ")[2]
        ver = _parse_semver(ver_str)
        ok = ver >= MIN_GIT
        return CheckResult(
            status="PASS" if ok else "FAIL",
            label="Git",
            message=ver_str,
            fix=None if ok else "Install Git 2.30+.",
        )
    except Exception as e:
        return CheckResult("FAIL", "Git", f"error: {e}", "Install Git 2.30+.")


def check_vault_exists(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Vault", "no path given",
                           "Set --vault or SCHIST_VAULT_PATH.")
    p = Path(vault_path)
    if p.is_dir():
        return CheckResult("PASS", "Vault", str(p))
    return CheckResult("FAIL", "Vault", f"{p} does not exist",
                       f"Create it with `schist init {p}` or set SCHIST_VAULT_PATH.")


def check_vault_is_git(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Git repo", "skipped (no vault)")
    git_dir = Path(vault_path) / ".git"
    if git_dir.exists():
        return CheckResult("PASS", "Git repo", "initialized")
    return CheckResult("FAIL", "Git repo", "not a git repo",
                       f"Run `cd {vault_path} && git init`.")


def check_schist_yaml(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "schist.yaml", "skipped (no vault)")
    yml = Path(vault_path) / "schist.yaml"
    if not yml.exists():
        return CheckResult("FAIL", "schist.yaml", "not found",
                           f"Run `schist init {vault_path}` to scaffold.")
    try:
        with open(yml) as f:
            yaml.safe_load(f)
        return CheckResult("PASS", "schist.yaml", "valid")
    except Exception as e:
        return CheckResult("FAIL", "schist.yaml", f"invalid: {e}",
                           f"Fix {yml} or re-run `schist init`.")


def check_sqlite(vault_path: Optional[str], db_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "SQLite", "skipped (no vault)")
    db = db_path or str(Path(vault_path) / ".schist" / "schist.db")
    if not Path(db).exists():
        return CheckResult("FAIL", "SQLite", f"{db} not found",
                           f"Run `schist-ingest --vault {vault_path} --db {db}` to rebuild.")
    try:
        conn = sqlite3.connect(db)
        try:
            docs = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
            concepts = conn.execute("SELECT count(*) FROM concepts").fetchone()[0]
            edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        finally:
            conn.close()
        return CheckResult("PASS", "SQLite",
                           f"{docs} docs, {concepts} concepts, {edges} edges")
    except Exception as e:
        return CheckResult("FAIL", "SQLite", f"query failed: {e}",
                           f"Run `schist-ingest --vault {vault_path} --db {db}` to rebuild.")


def check_post_commit_hook(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Post-commit hook", "skipped (no vault)")
    hook = Path(vault_path) / ".git" / "hooks" / "post-commit"
    if hook.exists():
        return CheckResult("PASS", "Post-commit hook", "installed")
    return CheckResult("FAIL", "Post-commit hook", "not installed",
                       f"Run `schist init {vault_path}` to install hooks.")


def check_ingest_available(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Ingest", "skipped (no vault)")

    # Mirror the discovery logic from hooks/post-commit
    env_script = os.environ.get("SCHIST_INGEST_SCRIPT")
    if env_script and Path(env_script).exists():
        return CheckResult("PASS", "Ingest", f"SCHIST_INGEST_SCRIPT={env_script}")

    vault_script = Path(vault_path) / ".schist" / "ingest.py"
    if vault_script.exists():
        return CheckResult("PASS", "Ingest", str(vault_script))

    if shutil.which("schist-ingest"):
        return CheckResult("PASS", "Ingest", "schist-ingest on PATH")

    return CheckResult("FAIL", "Ingest", "schist-ingest not found",
                       "Set SCHIST_INGEST_SCRIPT or run `pip install -e ./cli`.")


def check_spoke(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Spoke", "skipped (no vault)")
    if not is_spoke(vault_path):
        return CheckResult("SKIP", "Spoke", "not a spoke vault")
    try:
        cfg = load_spoke_config(vault_path)
        hub = cfg.hub
        try:
            subprocess.run(
                ["git", "ls-remote", hub],
                capture_output=True, text=True, timeout=10,
            )
            return CheckResult("PASS", "Spoke",
                               f"identity={cfg.identity}, scope={cfg.scope}, hub reachable")
        except subprocess.TimeoutExpired:
            return CheckResult("WARN", "Spoke",
                               f"identity={cfg.identity}, scope={cfg.scope}, hub timeout",
                               f"Check SSH connectivity to {hub}.")
        except Exception as e:
            return CheckResult("WARN", "Spoke",
                               f"identity={cfg.identity}, hub error: {e}",
                               f"Check that hub {hub} is reachable.")
    except Exception as e:
        return CheckResult("FAIL", "Spoke", f"invalid spoke.yaml: {e}",
                           "Re-run `schist init --spoke`.")


def check_mcp_config(vault_path: Optional[str]) -> CheckResult:
    """Check if schist is configured in Claude Code or Cursor settings."""
    candidates = [
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    if vault_path:
        candidates.append(Path(vault_path) / ".claude" / "settings.json")
        candidates.append(Path(vault_path) / ".claude" / "settings.local.json")

    for c in candidates:
        if not c.exists():
            continue
        try:
            data = json.loads(c.read_text())
            servers = data.get("mcpServers", {})
            if "schist" in servers:
                return CheckResult("PASS", "MCP", f"schist in {c}")
            for name, cfg in servers.items():
                args = cfg.get("args", [])
                if any("schist" in str(a) or "dist/index.js" in str(a) for a in args):
                    return CheckResult("PASS", "MCP", f"{name} in {c}")
        except Exception:
            pass

    # Also check Cursor
    cursor = Path.home() / ".cursor" / "mcp.json"
    if cursor.exists():
        try:
            data = json.loads(cursor.read_text())
            servers = data.get("mcpServers", {})
            if "schist" in servers:
                return CheckResult("PASS", "MCP", f"schist in {cursor}")
        except Exception:
            pass

    return CheckResult("WARN", "MCP", "no schist entry found",
                       "Run `schist init --print-mcp-config` for a ready-to-paste config.")


def run_doctor(vault_path: Optional[str], db_path: Optional[str],
               as_json: bool = False) -> list[CheckResult]:
    checks = [
        check_python(),
        check_node(),
        check_git(),
        check_vault_exists(vault_path),
        check_vault_is_git(vault_path),
        check_schist_yaml(vault_path),
        check_sqlite(vault_path, db_path),
        check_post_commit_hook(vault_path),
        check_ingest_available(vault_path),
        check_spoke(vault_path),
        check_mcp_config(vault_path),
    ]

    if as_json:
        data = []
        for r in checks:
            entry = {"status": r.status, "label": r.label, "message": r.message}
            if r.fix:
                entry["fix"] = r.fix
            data.append(entry)
        print(json.dumps(data, indent=2))
    else:
        for r in checks:
            line = f"[{r.status}] {r.label}: {r.message}"
            print(line)
            if r.fix:
                print(f"       Fix: {r.fix}")

    return checks


def doctor(args) -> None:
    vault_path = getattr(args, "vault", None)
    db_path = getattr(args, "db", None)
    if vault_path and not db_path:
        db_path = str(Path(vault_path) / ".schist" / "schist.db")
    as_json = getattr(args, "as_json", False)

    results = run_doctor(vault_path, db_path, as_json)
    if any(r.status == "FAIL" for r in results):
        sys.exit(1)
