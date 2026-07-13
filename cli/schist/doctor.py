"""schist doctor — health check for schist setup."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .index_contract import INDEX_SCHEMA_VERSION
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


def check_uv() -> CheckResult:
    """Check for `uv` — schist's recommended Python package manager.

    Reports PASS when uv is available with version info, WARN when missing
    (pip still works as a fallback for the documented install paths). Not
    a FAIL because end-users installing the published `schist` wheel from
    PyPI never need uv; the recommendation is for source/development use.
    """
    uv = shutil.which("uv")
    if not uv:
        return CheckResult(
            "WARN", "uv", "not found",
            "Recommended: install uv from https://docs.astral.sh/uv/getting-started/installation/ "
            "(pip continues to work as a fallback).",
        )
    try:
        raw = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=5)
        ver_str = raw.stdout.strip()
        return CheckResult("PASS", "uv", ver_str)
    except Exception as e:
        return CheckResult("WARN", "uv", f"error: {e}",
                           "Reinstall uv from https://docs.astral.sh/uv/getting-started/installation/.")


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


# Match the marker line and tolerate an optional trailing `# comment` so users
# who annotate the line ("# schist-hook-version: 2  # bumped 2026-05-19") still
# parse as v2 rather than falling through to "legacy".
_HOOK_VERSION_RE = re.compile(
    r"^# schist-hook-version:\s*(\S+)\s*(?:#.*)?$", re.MULTILINE
)


class HookReadError(RuntimeError):
    """Raised when a hook file exists but cannot be read (e.g. permissions)."""


def _installed_hook_version(hook_path: Path) -> Optional[str]:
    """Read the `# schist-hook-version:` marker from an installed hook.

    Returns the version token as a string (`"2"`, `"pinned"`, etc.), None if
    the hook is missing or has no marker (legacy unversioned hook). Raises
    HookReadError when the file exists but is unreadable — that case must NOT
    collapse into "legacy" because the recommended fix (`hooks reinstall`)
    would also fail.
    """
    try:
        text = hook_path.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise HookReadError(f"cannot read {hook_path}: {e}") from e
    m = _HOOK_VERSION_RE.search(text)
    return m.group(1) if m else None


def check_hooks_freshness(vault_path: Optional[str]) -> CheckResult:
    """Compare installed pre/post-commit hooks against the canonical templates.

    Warns when a vault is running a stale hook template — e.g. a spoke that
    was init'd before the pre-commit regex was tightened (issue #103). Users
    who intentionally customized their hooks can set the version line to
    `# schist-hook-version: pinned` to silence the warning.
    """
    if not vault_path:
        return CheckResult("SKIP", "Hooks freshness", "skipped (no vault)")
    if not (Path(vault_path) / ".git").exists():
        return CheckResult("SKIP", "Hooks freshness", "skipped (not a git repo)")

    # Local import to keep this module's import graph cheap when sync.py is not
    # the entry point. sync.py imports many things including subprocess/shutil.
    from . import sync as sync_mod

    current = str(sync_mod.HOOK_VERSION)
    hooks_dir = Path(vault_path) / ".git" / "hooks"
    stale = []
    pinned = []
    unreadable = []
    for name in ("pre-commit", "post-commit"):
        try:
            installed = _installed_hook_version(hooks_dir / name)
        except HookReadError as e:
            unreadable.append(f"{name}: {e}")
            continue
        if installed is None:
            stale.append(f"{name}: legacy (no version marker)")
        elif installed == "pinned":
            pinned.append(name)
        elif installed != current:
            stale.append(f"{name}: v{installed} (current: v{current})")

    if unreadable:
        return CheckResult(
            "FAIL", "Hooks freshness",
            "; ".join(unreadable),
            fix="Check filesystem permissions on .git/hooks/ — schist cannot read the installed hooks.",
        )
    if stale:
        return CheckResult(
            "WARN", "Hooks freshness",
            "; ".join(stale),
            fix=f"Run `schist --vault {vault_path} hooks reinstall` to update.",
        )
    if pinned:
        return CheckResult(
            "PASS", "Hooks freshness",
            f"current (v{current}); pinned: {', '.join(pinned)}",
        )
    return CheckResult("PASS", "Hooks freshness", f"current (v{current})")


def check_hooks_path(vault_path: Optional[str]) -> CheckResult:
    """Warn if `core.hooksPath` is set — schist's hooks at .git/hooks/
    are silently bypassed when this config is non-default.

    See issue #40. Common cause: user runs a pre-commit framework or
    shared team hooks under `~/.git-hooks/` and never installed schist's
    post-commit ingester / pre-commit secret guard there.
    """
    if not vault_path:
        return CheckResult("SKIP", "Hooks path", "skipped (no vault)")
    if not (Path(vault_path) / ".git").exists():
        return CheckResult("SKIP", "Hooks path", "skipped (not a git repo)")
    try:
        result = subprocess.run(
            ["git", "-C", vault_path, "config", "--get", "core.hooksPath"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return CheckResult("SKIP", "Hooks path", f"git config check failed: {e}")

    # `git config --get` exits 1 when the key is unset.
    if result.returncode == 0 and result.stdout.strip():
        configured = result.stdout.strip()
        return CheckResult(
            "WARN", "Hooks path",
            f"core.hooksPath is set to '{configured}' — schist hooks at .git/hooks/ are bypassed",
            fix=(
                f"Either unset (`git -C {vault_path} config --unset core.hooksPath`) "
                f"or symlink schist's hooks into {configured}/."
            ),
        )
    return CheckResult("PASS", "Hooks path", "uses default .git/hooks/")


# Lines that plainly ignore the .schist/ runtime dir. Deliberately simple —
# exact `.schist/` / `.schist`, optionally root-anchored — NOT full gitignore
# semantics. An exotic pattern that happens to cover .schist/ still warns:
# false-warn is preferred over false-pass here.
_SCHIST_IGNORE_LINES = {".schist", ".schist/", "/.schist", "/.schist/"}


def check_root_gitignore(vault_path: Optional[str]) -> CheckResult:
    """Warn when the vault root's tracked .gitignore does not cover .schist/.

    #309 added `.schist/` to the root .gitignore of the hub SEED commit, so
    only vaults initialized after it get one. Already-seeded hubs (and their
    clones) never gain it automatically (#362): spoke working copies are
    retrofitted via `schist hooks reinstall` (.git/info/exclude, #354), but
    that is per-clone — only the tracked .gitignore protects every working
    copy of the vault, including the hub operator's own. Without it,
    `git status` shows the SQLite index as untracked and a broad `git add`
    can commit it.

    Applies to both hub and spoke vault roots. WARN, never FAIL: a retrofitted
    spoke's own working copy is already covered by .git/info/exclude.
    """
    label = "Root .gitignore"
    if not vault_path:
        return CheckResult("SKIP", label, "skipped (no vault)")
    if not (Path(vault_path) / ".git").exists():
        return CheckResult("SKIP", label, "skipped (not a git repo)")

    fix = (
        "Add a `.schist/` line to the vault root .gitignore and commit it. "
        "The root file is hub-owned on hub/spoke deployments: per the vault "
        "ACL, commit and push it with the hub operator's machine identity "
        "(e.g. SCHIST_IDENTITY=pi; see docs/hub-spoke-pi-orcd-dragonfly.md)."
    )
    gitignore = Path(vault_path) / ".gitignore"
    try:
        text = gitignore.read_text()
    except FileNotFoundError:
        return CheckResult(
            "WARN", label,
            "no .gitignore at the vault root — .schist/ (runtime SQLite "
            "state) is not ignored; expected a line `.schist/`",
            fix,
        )
    except (OSError, UnicodeDecodeError) as e:
        # Unreadable is as suspect as missing — prefer false-warn.
        return CheckResult("WARN", label, f"cannot read {gitignore}: {e}", fix)

    if any(line.strip() in _SCHIST_IGNORE_LINES for line in text.splitlines()):
        return CheckResult("PASS", label, ".gitignore ignores .schist/")

    return CheckResult(
        "WARN", label,
        ".gitignore has no line ignoring .schist/ (runtime SQLite state); "
        "expected a line `.schist/`",
        fix,
    )


def check_ingest_available(vault_path: Optional[str]) -> CheckResult:
    if not vault_path:
        return CheckResult("SKIP", "Ingest", "skipped (no vault)")

    # Mirror the discovery logic from hooks/post-commit
    env_script = os.environ.get("SCHIST_INGEST_SCRIPT")
    if env_script and Path(env_script).exists():
        return CheckResult("PASS", "Ingest", f"SCHIST_INGEST_SCRIPT={env_script}")

    vault_script = Path(vault_path) / ".schist" / "ingest.py"
    if vault_script.exists():
        # Hand-provisioned bare-script copies must be refreshed alongside
        # slice B (#130 D3): ingest.py now imports a sibling
        # index_contract.py (like the sibling schema.sql it always needed)
        # and stamps INDEX_SCHEMA_VERSION instead of a literal 1. A stale
        # copy re-stamps the OLD version after every commit while readers
        # expect the new one, so every read triggers a full rebuild — a
        # silent ping-pong with no error anywhere.
        issues = []
        sibling_contract = vault_script.parent / "index_contract.py"
        if not sibling_contract.exists():
            issues.append("missing sibling index_contract.py")
        else:
            # #357: a sibling that EXISTS but declares an old schemaVersion is
            # the exact rebuild-loop trigger the missing-sibling check guards
            # against — the hook copy stamps the old version after every
            # commit while this installed CLI's readers expect the new one.
            # The sibling's baked-in mirror is authoritative in bare-script
            # deployments (no repo checkout, so load_index_contract falls
            # back to it); read the declared version out of that literal.
            try:
                m = re.search(
                    r"[\"']schemaVersion[\"']\s*:\s*(\d+)",
                    sibling_contract.read_text(encoding="utf-8"),
                )
            except (OSError, UnicodeDecodeError):
                m = None
            if m is None:
                # A sibling whose declared version can't be read is exactly
                # as suspect as a mismatched one — treating no-match as fine
                # let a hand-edited copy pass while the runtime stamped
                # something else entirely (#380).
                issues.append(
                    "cannot determine the schemaVersion its baked-in mirror "
                    "declares (hand-edited or truncated copy?)"
                )
            elif int(m.group(1)) != INDEX_SCHEMA_VERSION:
                issues.append(
                    f"sibling index_contract.py declares schema v{m.group(1)} "
                    f"but this installed schist expects v{INDEX_SCHEMA_VERSION}"
                )
        try:
            script_text = vault_script.read_text()
        except (OSError, UnicodeDecodeError):
            script_text = ""
        if re.search(r"PRAGMA user_version = 1\b", script_text):
            issues.append("stamps a hardcoded user_version=1 (pre-index-contract copy)")
        # #369: the #354 revision of ingest.py imports env_utils with no inline
        # fallback, so a copy from that window breaks the post-commit hook with
        # ModuleNotFoundError on every commit unless env_utils.py was copied
        # alongside. Current copies define the env_flag fallback inline and
        # need no sibling — only flag the fallback-less window.
        if (
            "from env_utils import" in script_text
            and "def env_flag" not in script_text
            and not (vault_script.parent / "env_utils.py").exists()
        ):
            issues.append(
                "imports env_utils with no inline fallback and no sibling "
                "env_utils.py (#369) — the post-commit hook fails on every commit"
            )
        if issues:
            return CheckResult(
                "WARN", "Ingest",
                f"{vault_script} is a stale hand-provisioned copy: {'; '.join(issues)}",
                "Refresh the copy from cli/schist/ (ingest.py + index_contract.py + "
                "env_utils.py + schema.sql), or delete it so the hook falls back to "
                "the installed schist-ingest. After the next index-schema bump a "
                "stale copy causes a silent rebuild loop: the post-commit hook "
                "restamps the old version, and every read rebuilds the whole index "
                "again.",
            )
        return CheckResult("PASS", "Ingest", str(vault_script))

    if shutil.which("schist-ingest"):
        return CheckResult("PASS", "Ingest", "schist-ingest on PATH")

    return CheckResult("FAIL", "Ingest", "schist-ingest not found",
                       "Set SCHIST_INGEST_SCRIPT or run `uv pip install --system -e ./cli` (or `pip install -e ./cli`).")


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


def _resolve_push_identity_env() -> Optional[str]:
    """Mirror hub pre-receive identity env precedence.

    Spoke pushes are authorized by the hub's pre-receive hook, which reads the
    machine/spoke identity from SCHIST_IDENTITY first, then GL_USER. Empty
    strings fall through the same way Python's `or` does in pre_receive.py.
    """
    return os.environ.get("SCHIST_IDENTITY") or os.environ.get("GL_USER") or None


def check_spoke_identity_env(vault_path: Optional[str]) -> CheckResult:
    """Fail fast when a spoke cannot identify itself to the hub.

    MCP's local ACL check can fall back to the agent owner for UX, but the hub
    cannot: a spoke push with neither SCHIST_IDENTITY nor GL_USER set is
    rejected by pre_receive.py. Doctor surfaces that launcher/config problem
    before agents accumulate local commits that only fail on background push.
    """
    label = "Spoke identity"
    if not vault_path:
        return CheckResult("SKIP", label, "skipped (no vault)")
    if not is_spoke(vault_path):
        return CheckResult("SKIP", label, "not a spoke vault")

    identity = _resolve_push_identity_env()
    if identity:
        return CheckResult("PASS", label, f"push identity resolves to '{identity}'")

    return CheckResult(
        "FAIL",
        label,
        "spoke push identity is not configured; hub pushes will be rejected",
        (
            "Set SCHIST_IDENTITY in the MCP/launcher environment, or GL_USER "
            "for gitolite-compatible deployments. This must be the spoke's "
            "machine identity from vault.yaml, not just the MCP owner."
        ),
    )


def check_spoke_acl_drift(vault_path: Optional[str]) -> CheckResult:
    """Flag schist.yaml directories not present in this spoke's hub write grant.

    Runs only on spokes (skips standalone vaults and contexts without spoke.yaml).
    Reads schist.yaml directories, the spoke's identity from .schist/spoke.yaml,
    and the per-identity write grant from vault.yaml's access map. Reports any
    schema dir that doesn't match any of the identity's write scopes (using the
    same parent->child rule as the hub's pre-receive).
    """
    label = "Spoke ACL"

    if not vault_path:
        return CheckResult("SKIP", label, "no vault path supplied")

    vault = Path(vault_path)
    if not is_spoke(vault_path):
        return CheckResult("SKIP", label, "not a spoke")

    vault_yaml = vault / "vault.yaml"
    if not vault_yaml.exists():
        return CheckResult("SKIP", label, "no vault.yaml")

    # Identity from spoke.yaml
    try:
        cfg = load_spoke_config(vault_path)
        identity = cfg.identity
    except Exception as e:  # noqa: BLE001 — surface as SKIP so doctor never crashes
        return CheckResult("SKIP", label, f"could not read spoke.yaml: {e}")

    # Schema dirs from schist.yaml — inline yaml.safe_load
    try:
        schist_data = yaml.safe_load((vault / "schist.yaml").read_text()) or {}
    except Exception as e:  # noqa: BLE001
        return CheckResult("SKIP", label, f"could not read schist.yaml: {e}")

    dirs_field = schist_data.get("directories") or {}
    # `directories:` can be either a dict (canonical default.yaml form) or a list (some test fixtures).
    if isinstance(dirs_field, dict):
        schema_dirs = [v.rstrip("/") for v in dirs_field.values()]
    elif isinstance(dirs_field, list):
        schema_dirs = [str(v).rstrip("/") for v in dirs_field]
    else:
        return CheckResult("SKIP", label, "schist.yaml 'directories' field is malformed")

    if not schema_dirs:
        return CheckResult("SKIP", label, "schist.yaml has no directories declared")

    # Parse vault.yaml and resolve the identity's write grant
    try:
        from schist.acl import _scope_matches, parse_vault_yaml
        acl = parse_vault_yaml(vault_yaml)
    except Exception as e:  # noqa: BLE001
        return CheckResult("SKIP", label, f"could not parse vault.yaml: {e}")

    entry = acl.access.get(identity)
    if entry is None:
        return CheckResult(
            "WARN", label,
            f"identity '{identity}' has no access entry in vault.yaml — ask the hub admin to add one",
        )

    # Find schema dirs the identity is NOT granted write on.
    drift = [d for d in schema_dirs if not _scope_matches(entry.write, d)]
    if not drift:
        return CheckResult("PASS", label, f"identity '{identity}' is granted all schema directories")

    return CheckResult(
        "WARN", label,
        f"identity '{identity}' has no hub write grant for: {', '.join(drift)}. "
        f"Ask the hub admin to extend your write scope in vault.yaml.",
    )


def _hub_expected_dirs(hub: Path) -> list[str]:
    """Directories a hub's participants are expected to be granted.

    Prefer HEAD:schist.yaml if the hub has one; else fall back to the packaged
    default.yaml directory list. Either way, exclude infra dirs (logs/, projects/),
    which the seed deliberately does not grant (see sync.py:_build_seed_vault) —
    so they are never flagged as "missing" drift.
    """
    import subprocess

    INFRA = {"logs", "projects"}

    def _dirs_from(text: str) -> list[str]:
        d = yaml.safe_load(text) or {}
        dirs = d.get("directories") or {}
        vals = dirs.values() if isinstance(dirs, dict) else dirs
        return [str(v).rstrip("/") for v in vals]

    r = subprocess.run(
        ["git", "--git-dir", str(hub), "show", "HEAD:schist.yaml"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode == 0:
        dirs = _dirs_from(r.stdout)
    else:
        # Fallback: packaged default.yaml (sibling of this module).
        default_path = Path(__file__).resolve().parent / "default.yaml"
        dirs = _dirs_from(default_path.read_text())

    # Infra dirs are never expected participant grants, regardless of source.
    return [d for d in dirs if d not in INFRA]


def check_hub_acl_drift(hub_path: Optional[str]) -> CheckResult:
    """Flag ACL drift on a bare hub: schema dirs not granted (a), and dirs that
    some participants have but others lack (b).
    """
    label = "Hub ACL drift"
    if not hub_path:
        return CheckResult("SKIP", label, "no --hub-path supplied")

    hub = Path(hub_path)
    if not (hub / "objects").is_dir():
        return CheckResult("SKIP", label, f"not a git repository: {hub_path}")

    try:
        import subprocess
        from schist.acl import _scope_matches, parse_vault_data
        text = subprocess.run(
            ["git", "--git-dir", str(hub), "show", "HEAD:vault.yaml"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
        acl = parse_vault_data(yaml.safe_load(text))
        expected_dirs = _hub_expected_dirs(hub)
    except Exception as e:  # noqa: BLE001 — surface as SKIP so doctor never crashes
        return CheckResult("SKIP", label, f"could not read/parse hub vault.yaml: {e}")

    names = sorted(acl.access.keys())

    # Signal (a): expected schema dir not granted to one-or-more participants.
    a_problems: list[str] = []
    for d in expected_dirs:
        missing = [n for n in names if not _scope_matches(acl.access[n].write, d)]
        if missing:
            a_problems.append(f"'{d}' not granted to: {', '.join(missing)}")

    # Signal (b): a concrete dir some participants have in write but others lack.
    name_set = set(names)
    holders: dict[str, set] = {}
    for n in names:
        for s in acl.access[n].write:
            if s != "*":
                holders.setdefault(s, set()).add(n)
    b_problems: list[str] = []
    for s, who in sorted(holders.items()):
        lacking = name_set - who
        if lacking:
            b_problems.append(f"'{s}' held by {', '.join(sorted(who))} but not {', '.join(sorted(lacking))}")

    if not a_problems and not b_problems:
        return CheckResult("PASS", label, f"{len(names)} participants, no ACL drift")

    msg_parts = a_problems + b_problems
    return CheckResult(
        "WARN", label, "; ".join(msg_parts),
        fix="Grant missing scopes from the hub host, e.g. `schist hub grant <participant> --write <dir> --hub-path <hub>`.",
    )


def _auto_detect_mcp_path() -> Optional[str]:
    """Locate `mcp-server/dist/index.js` relative to this checkout.

    Mirrors the fallback logic in sync.py:_print_mcp_config (lines 719-722).
    Returns the absolute path if found; None if not (e.g. distribution-
    installed schist with no source checkout).
    """
    pkg_dir = Path(__file__).resolve().parents[2]
    candidate = pkg_dir / "mcp-server" / "dist" / "index.js"
    if candidate.exists():
        return str(candidate.resolve())
    return None


def check_mcp_config(vault_path: Optional[str]) -> CheckResult:
    """Check if schist is configured in Claude Code or Cursor settings.

    Beyond locating the entry, validates (issue #43):
      1. args[0] exists on disk (REQUIRED)
      2. env.SCHIST_VAULT_PATH matches vault_path if provided
      3. args[0] matches the auto-detected current mcp-server path
    """
    candidates = [
        # Claude Code (active product) stores user-scope MCP servers here.
        # Same `mcpServers` shape as Claude Desktop, different path.
        Path.home() / ".claude.json",
        # Claude Desktop / settings.json paths (legacy and project-scoped).
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    if vault_path:
        candidates.append(Path(vault_path) / ".claude" / "settings.json")
        candidates.append(Path(vault_path) / ".claude" / "settings.local.json")

    located = None  # tuple of (config_path, entry_name, entry_dict)
    for c in candidates:
        if not c.exists():
            continue
        try:
            data = json.loads(c.read_text())
        except Exception:
            continue
        servers = data.get("mcpServers", {})
        if "schist" in servers:
            located = (c, "schist", servers["schist"])
            break
        for name, cfg in servers.items():
            args = cfg.get("args", [])
            if any("schist" in str(a) or "dist/index.js" in str(a) for a in args):
                located = (c, name, cfg)
                break
        if located:
            break

    if not located:
        # Also check Cursor as a final fallback.
        cursor = Path.home() / ".cursor" / "mcp.json"
        if cursor.exists():
            try:
                data = json.loads(cursor.read_text())
                servers = data.get("mcpServers", {})
                if "schist" in servers:
                    located = (cursor, "schist", servers["schist"])
            except Exception:
                pass

    if not located:
        return CheckResult(
            "WARN", "MCP", "no schist entry found",
            "Run `schist init --print-mcp-config --identity <name>` and "
            "execute the printed `claude mcp add` command.",
        )

    config_path, entry_name, entry = located
    args = entry.get("args", [])
    args0 = str(args[0]) if args else ""

    warnings: list[str] = []

    # Sub-check 1 (required): args[0] is_file
    if not args0:
        warnings.append("MCP entry has no args[0]")
    elif not Path(args0).is_file():
        warnings.append(f"MCP entry points at {args0} which does not exist")

    # Sub-check 2: env SCHIST_VAULT_PATH matches current vault
    if vault_path and args0:
        env = entry.get("env", {}) or {}
        entry_vault = env.get("SCHIST_VAULT_PATH", "")
        if entry_vault and Path(entry_vault).resolve() != Path(vault_path).resolve():
            warnings.append(
                f"MCP env SCHIST_VAULT_PATH={entry_vault} ≠ current vault {vault_path}"
            )

    # Sub-check 3: args[0] matches auto-detected mcp-server/dist/index.js
    if args0 and Path(args0).is_file():
        detected = _auto_detect_mcp_path()
        if detected and Path(detected).resolve() != Path(args0).resolve():
            warnings.append(
                f"MCP entry's dist/index.js path differs from auto-detected current path"
            )

    if warnings:
        return CheckResult(
            "WARN", "MCP", "; ".join(warnings),
            "Re-run `schist init --print-mcp-config --identity <name>` and "
            "update the entry in your Claude Code config.",
        )

    return CheckResult(
        "PASS", "MCP", f"{entry_name} in {config_path} (args[0]={args0})",
    )


# Match `REQUIRED_DOCS_COLUMNS = new Set([ "id", "title", ... ])` in the
# compiled JS. Tolerant of newlines and trailing commas. The inner-string
# match is non-greedy so a later `Set(...)` literal in the file doesn't
# extend the capture. The optional `(?::[^=]*)?` also tolerates the source
# form's type annotation (`: ReadonlySet<string>`), so
# cli/tests/test_index_contract.py can pin this regex against the REAL
# sqlite-reader.ts text — a refactor of the literal there would otherwise
# silently downgrade this check to SKIP on the next build.
_REQUIRED_DOCS_RE = re.compile(
    r"REQUIRED_DOCS_COLUMNS(?::[^=]*)?\s*=\s*new\s+Set\(\s*\[(.*?)\]\s*\)",
    re.DOTALL,
)
_DOC_COL_STRING_RE = re.compile(r"""['"]([a-zA-Z_][a-zA-Z0-9_]*)['"]""")

# Tool definitions in the compiled tool-registry.js are object literals whose
# first property is `name: "..."`. Requiring the preceding `{` scopes the match
# to those literals so a `name: "x"` that appears inside a description string or
# a comment (tsc keeps comments) can't be harvested as a phantom live tool —
# which would silently mask a stale skill reference (false negative).
_MCP_TOOL_NAME_RE = re.compile(r'\{\s*name:\s*"([a-z][a-z0-9_]*)"')
# Keys of the REMOVED_TOOLS tombstone map, to tell "retired" apart from "typo".
# Bound the block at the closing brace on its own line (`\n}`): a `}` inside a
# tombstone message string sits within quotes mid-line, never line-leading, so
# this won't truncate early and drop later keys. A regex can't balance braces;
# this matches the shape `tsc` emits and degrades to an empty set otherwise.
_MCP_REMOVED_BLOCK_RE = re.compile(
    r"REMOVED_TOOLS\s*=\s*\{(.*?)\n\s*\}", re.DOTALL
)
# Match only line-leading keys (`  toolname:`). Object keys sit at the start of
# their line; a `word:` inside a tombstone message string (e.g. "unlock step:")
# falls on a continuation line that starts with a quote, so MULTILINE-anchoring
# to ^ keeps prose colons from being misread as removed-tool names.
_MCP_REMOVED_KEY_RE = re.compile(r"^\s*([a-z][a-z0-9_]*)\s*:", re.MULTILINE)
# How skills reference a schist tool: the `mcp__schist__<tool>` prefix form,
# used in both `allowed-tools:` frontmatter and inline prose.
_SKILL_TOOL_REF_RE = re.compile(r"mcp__schist__([a-z][a-z0-9_]*)")


def _mcp_dist_dir_from_config(vault_path: Optional[str]) -> Optional[Path]:
    """Return the dist/ directory of the configured MCP entry, or None.

    Mirrors `check_mcp_config`'s lookup order so doctor reports against the
    actual deployed dist (not just the source checkout's local build).
    """
    candidates = [
        Path.home() / ".claude.json",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    if vault_path:
        candidates.append(Path(vault_path) / ".claude" / "settings.json")
        candidates.append(Path(vault_path) / ".claude" / "settings.local.json")
    candidates.append(Path.home() / ".cursor" / "mcp.json")

    for c in candidates:
        if not c.exists():
            continue
        try:
            data = json.loads(c.read_text())
        except Exception:
            continue
        servers = data.get("mcpServers", {})
        entry = servers.get("schist")
        if not entry:
            for cfg in servers.values():
                args = cfg.get("args", [])
                if any("schist" in str(a) or "dist/index.js" in str(a) for a in args):
                    entry = cfg
                    break
        if not entry:
            continue
        args = entry.get("args", [])
        if not args:
            continue
        args0 = Path(str(args[0]))
        if args0.is_file():
            return args0.parent
    return None


def _extract_mcp_required_columns(dist_dir: Path) -> Optional[set[str]]:
    """Read REQUIRED_DOCS_COLUMNS from the MCP server's compiled
    sqlite-reader.js. Returns None when the constant can't be found —
    older MCP builds (pre-#145) don't declare it.
    """
    reader = dist_dir / "sqlite-reader.js"
    try:
        text = reader.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    m = _REQUIRED_DOCS_RE.search(text)
    if not m:
        return None
    return set(_DOC_COL_STRING_RE.findall(m.group(1)))


def _extract_mcp_tool_names(dist_dir: Path) -> Optional[set[str]]:
    """Read the live tool names from the MCP server's compiled
    tool-registry.js. Returns None when the file can't be read or no tool
    names are found (older builds, or a moved registry).
    """
    registry = dist_dir / "tool-registry.js"
    try:
        text = registry.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    names = set(_MCP_TOOL_NAME_RE.findall(text))
    return names or None


def _extract_mcp_removed_tools(dist_dir: Path) -> set[str]:
    """Read the keys of the REMOVED_TOOLS tombstone map from the compiled
    tool-registry.js. Returns an empty set when absent (pre-tombstone builds).
    """
    registry = dist_dir / "tool-registry.js"
    try:
        text = registry.read_text()
    except (OSError, UnicodeDecodeError):
        return set()
    block = _MCP_REMOVED_BLOCK_RE.search(text)
    if not block:
        return set()
    return set(_MCP_REMOVED_KEY_RE.findall(block.group(1)))


def _scan_skill_tool_refs(skills_dir: Path) -> dict[str, set[str]]:
    """Map each referenced schist tool name to the set of skill files that
    reference it (via the `mcp__schist__<tool>` form), scanning all markdown
    under skills_dir. Files are recorded as paths relative to skills_dir
    (e.g. `learn/SKILL.md`) — every skill's file is named SKILL.md, so the
    bare basename would collapse distinct skills and hide which one to fix.
    """
    refs: dict[str, set[str]] = {}
    # os.walk(followlinks=True), not Path.rglob: shared skills are commonly
    # symlinked into the vault, and rglob does not descend into symlinked
    # directories — it would skip exactly the skills this check exists to scan.
    for root, _dirs, files in os.walk(skills_dir, followlinks=True):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            md = Path(root) / fname
            try:
                text = md.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            rel = str(md.relative_to(skills_dir))
            for tool in _SKILL_TOOL_REF_RE.findall(text):
                refs.setdefault(tool, set()).add(rel)
    return refs


def _canonical_docs_columns() -> Optional[set[str]]:
    """Return the `docs` table column set defined by the bundled schema.sql.

    Materializes the schema in an in-memory SQLite and reads PRAGMA
    table_info — same source of truth ingest.py uses. Returns None on
    failure (missing schema.sql, malformed SQL — both are 'someone broke
    the install', distinct from skew).
    """
    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        return None
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(schema_path.read_text())
            cols = {row[1] for row in conn.execute("PRAGMA table_info(docs)").fetchall()}
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return cols or None


def check_mcp_schema_alignment(vault_path: Optional[str]) -> CheckResult:
    """Detect MCP-server-vs-ingest schema skew before it surfaces as the
    misleading 'schist-ingest is older than this MCP server' error from
    ensureSchemaCurrent.

    Specifically catches the case where MCP's REQUIRED_DOCS_COLUMNS lists
    columns the current `schema.sql` does not define — i.e. the MCP dist
    was compiled against an older schema that has since dropped columns.
    The reverse case (canonical schema has columns MCP doesn't list) is
    intentional — MCP only declares the columns it reads, not the full
    table — so we don't flag it.

    SKIPs when no MCP entry is configured or the MCP dist predates the
    drift-detection feature (pre-#145).
    """
    dist_dir = _mcp_dist_dir_from_config(vault_path)
    if dist_dir is None:
        return CheckResult("SKIP", "MCP schema alignment", "skipped (no MCP config)")

    mcp_required = _extract_mcp_required_columns(dist_dir)
    if mcp_required is None:
        return CheckResult(
            "SKIP", "MCP schema alignment",
            f"skipped (REQUIRED_DOCS_COLUMNS not declared in {dist_dir / 'sqlite-reader.js'})",
        )

    canonical = _canonical_docs_columns()
    if canonical is None:
        return CheckResult(
            "FAIL", "MCP schema alignment",
            "could not read canonical schema.sql",
            "Reinstall schist: `uv tool install --reinstall --force <path-to-schist/cli>`.",
        )

    retired = mcp_required - canonical
    if not retired:
        return CheckResult("PASS", "MCP schema alignment",
                           f"in sync ({len(mcp_required)} required docs columns)")

    return CheckResult(
        "WARN", "MCP schema alignment",
        f"MCP expects retired columns: {', '.join(sorted(retired))}",
        fix=(
            "Rebuild the MCP server dist to align with the installed schist-ingest: "
            f"`cd <schist>/mcp-server && npm run build`, then restart Claude Code / "
            "Claude Desktop so they reload the new dist. "
            "(The skew direction makes the runtime error from `ensureSchemaCurrent` "
            "misleading — it always blames `schist-ingest`.)"
        ),
    )


# Match the `schemaVersion: <int>` entry of the INDEX_CONTRACT_FALLBACK
# object literal in the compiled dist/sqlite-reader.js. Non-greedy `\{(.*?)\}`
# is safe: the object nests arrays (brackets), never braces. The optional
# `(?::[^=]*)?` tolerates the source form's `: IndexContract` annotation so
# cli/tests/test_index_contract.py can pin this regex against the REAL
# sqlite-reader.ts text — a refactor of the literal there would otherwise
# silently downgrade the version leg of check_index_schema_version to
# "not declared".
_INDEX_CONTRACT_FALLBACK_RE = re.compile(
    r"INDEX_CONTRACT_FALLBACK(?::[^=]*)?\s*=\s*\{(.*?)\}", re.DOTALL,
)
_SCHEMA_VERSION_KEY_RE = re.compile(r"schemaVersion:\s*(\d+)")


def _extract_mcp_index_schema_version(dist_dir: Path) -> Optional[int]:
    """Read the index schema version the MCP server's compiled
    sqlite-reader.js was built with. Returns None when the constant can't be
    found — pre-slice-B builds (#130 D3) don't declare it.
    """
    reader = dist_dir / "sqlite-reader.js"
    try:
        text = reader.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    block = _INDEX_CONTRACT_FALLBACK_RE.search(text)
    if not block:
        return None
    m = _SCHEMA_VERSION_KEY_RE.search(block.group(1))
    return int(m.group(1)) if m else None


def check_index_schema_version(vault_path: Optional[str],
                               db_path: Optional[str] = None) -> CheckResult:
    """Detect index-schema-VERSION skew — the axis the column-based 'MCP
    schema alignment' check cannot see (a version bump with no column the
    MCP reader SELECTs changes user_version only).

    Compares three values and names the direction-specific remedy:
      - the vault index's `PRAGMA user_version` (what the last completed
        ingest stamped),
      - the installed CLI's INDEX_SCHEMA_VERSION (what this schist-ingest
        stamps and get_db expects),
      - the schemaVersion baked into the configured MCP dist (what
        ensureSchemaCurrent expects).
    """
    label = "Index schema version"
    if not vault_path:
        return CheckResult("SKIP", label, "skipped (no vault)")
    if db_path is None:
        db_path = str(Path(vault_path) / ".schist" / "schist.db")
    if not Path(db_path).exists():
        return CheckResult("SKIP", label,
                           "skipped (no index DB yet — built on first read)")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            stamped = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return CheckResult("SKIP", label,
                           "skipped (index DB unreadable — see the SQLite check)")

    warnings: list[str] = []
    fixes: list[str] = []

    if stamped != 0 and stamped != INDEX_SCHEMA_VERSION:
        if stamped < INDEX_SCHEMA_VERSION:
            warnings.append(
                f"index is stamped v{stamped} but the installed schist-ingest "
                f"expects v{INDEX_SCHEMA_VERSION}"
            )
            fixes.append(
                "Rebuild the index: `schist-ingest --vault <vault> --db "
                "<vault>/.schist/schist.db` (readers also rebuild it "
                "automatically on next access)."
            )
        else:
            warnings.append(
                f"index is stamped v{stamped}, NEWER than the installed "
                f"schist-ingest (v{INDEX_SCHEMA_VERSION}) — something newer "
                "built it, and this CLI would downgrade it on rebuild"
            )
            fixes.append(
                "Upgrade schist: `uv tool install --reinstall --force "
                "<path-to-schist/cli>`."
            )

    dist_dir = _mcp_dist_dir_from_config(vault_path)
    mcp_version = _extract_mcp_index_schema_version(dist_dir) if dist_dir else None
    if mcp_version is not None and mcp_version != INDEX_SCHEMA_VERSION:
        if mcp_version > INDEX_SCHEMA_VERSION:
            warnings.append(
                f"MCP dist expects index schema v{mcp_version} but the installed "
                f"schist-ingest stamps v{INDEX_SCHEMA_VERSION} — MCP reads will "
                "rebuild once, recheck, and fail with the 'schist-ingest is older' error"
            )
            fixes.append(
                "Upgrade schist: `uv tool install --reinstall --force "
                "<path-to-schist/cli>`."
            )
        else:
            warnings.append(
                f"MCP dist expects index schema v{mcp_version} but the installed "
                f"schist-ingest stamps v{INDEX_SCHEMA_VERSION} — MCP reads will "
                "trigger a full index rebuild on every tool call"
            )
            fixes.append(
                "Rebuild the MCP server dist: `cd <schist>/mcp-server && npm run "
                "build`, then restart Claude Code / Claude Desktop."
            )

    if warnings:
        return CheckResult("WARN", label, "; ".join(warnings), " ".join(fixes))

    parts = [f"index v{stamped}" if stamped != 0 else "index unstamped (v0: pre-marker or ingest in flight)",
             f"schist-ingest v{INDEX_SCHEMA_VERSION}"]
    if mcp_version is not None:
        parts.append(f"MCP dist v{mcp_version}")
    return CheckResult("PASS", label, ", ".join(parts))


def check_skill_tool_references(vault_path: Optional[str]) -> CheckResult:
    """Catch skills that call schist MCP tools the installed server no longer
    exposes — the failure mode that surfaced as the `request_capabilities`
    removal (#72/#76) leaving dangling `mcp__schist__request_capabilities`
    calls in shared skills, which agents hit as a confusing runtime error.

    Cross-checks every `mcp__schist__<tool>` reference under
    `<vault>/shared/skills/` against the live tool set parsed from the
    installed MCP server's tool-registry.js. Known-removed tools (present in
    the REMOVED_TOOLS tombstone map) are reported distinctly from unknown
    names (likely typos or a rename the skill missed).

    SKIPs when no MCP entry is configured, no shared/skills directory exists,
    or the registry can't be parsed (older dist).
    """
    label = "Skill tool references"
    if not vault_path:
        return CheckResult("SKIP", label, "skipped (no vault path)")
    skills_dir = Path(vault_path) / "shared" / "skills"
    if not skills_dir.is_dir():
        return CheckResult("SKIP", label, "skipped (no shared/skills directory)")

    dist_dir = _mcp_dist_dir_from_config(vault_path)
    if dist_dir is None:
        return CheckResult("SKIP", label, "skipped (no MCP config)")

    live = _extract_mcp_tool_names(dist_dir)
    if live is None:
        return CheckResult(
            "SKIP", label,
            f"skipped (no tool names found in {dist_dir / 'tool-registry.js'})",
        )

    removed = _extract_mcp_removed_tools(dist_dir)
    refs = _scan_skill_tool_refs(skills_dir)
    stale = {tool: files for tool, files in refs.items() if tool not in live}
    if not stale:
        return CheckResult(
            "PASS", label,
            f"all skill tool references resolve ({len(refs)} distinct tools referenced)",
        )

    retired = {t: f for t, f in stale.items() if t in removed}
    unknown = {t: f for t, f in stale.items() if t not in removed}
    parts = []
    for t, f in sorted(retired.items()):
        parts.append(f"{t} (removed; in {', '.join(sorted(f))})")
    for t, f in sorted(unknown.items()):
        parts.append(f"{t} (unknown; in {', '.join(sorted(f))})")
    return CheckResult(
        "WARN", label,
        "skills reference tools the server does not expose: " + "; ".join(parts),
        fix=(
            "Update the offending skills under <vault>/shared/skills/ to stop "
            "calling these tools. Removed tools return a TOOL_REMOVED message "
            "explaining the replacement; unknown names are likely typos or a "
            "missed rename. Restart Claude Code / Claude Desktop after editing "
            "so they reload the skills."
        ),
    )


def run_doctor(vault_path: Optional[str], db_path: Optional[str],
               as_json: bool = False, hub_path: Optional[str] = None) -> list[CheckResult]:
    checks = [
        check_python(),
        check_node(),
        check_uv(),
        check_git(),
        check_vault_exists(vault_path),
        check_vault_is_git(vault_path),
        check_schist_yaml(vault_path),
        check_sqlite(vault_path, db_path),
        check_post_commit_hook(vault_path),
        check_hooks_freshness(vault_path),
        check_hooks_path(vault_path),
        check_root_gitignore(vault_path),
        check_ingest_available(vault_path),
        check_spoke(vault_path),
        check_spoke_identity_env(vault_path),
        check_spoke_acl_drift(vault_path),
        check_mcp_config(vault_path),
        check_mcp_schema_alignment(vault_path),
        check_index_schema_version(vault_path, db_path),
        check_skill_tool_references(vault_path),
    ]
    if hub_path:
        checks.append(check_hub_acl_drift(hub_path))

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
    hub_path = getattr(args, "hub_path", None)

    results = run_doctor(vault_path, db_path, as_json, hub_path=hub_path)
    if any(r.status == "FAIL" for r in results):
        sys.exit(1)
