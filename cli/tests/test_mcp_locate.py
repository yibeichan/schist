# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for locating the MCP server's entry point (schist.mcp_locate)."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from schist import mcp_locate


def _npm_root(path: Path):
    """A stand-in for `npm root -g` that prints `path`."""
    def run(cmd, *args, **kwargs):
        assert cmd == ["npm", "root", "-g"]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{path}\n", stderr="")
    return run


def _global_install(root: Path) -> Path:
    entry = root / "@schist" / "mcp-server" / "dist" / "index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    return entry


class TestFindMcpServerEntry:
    def test_a_source_checkout_wins(self, tmp_path, monkeypatch):
        checkout = tmp_path / "checkout" / "mcp-server" / "dist" / "index.js"
        checkout.parent.mkdir(parents=True)
        checkout.write_text("", encoding="utf-8")
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: checkout)
        _global_install(tmp_path / "npm")
        with patch("subprocess.run", _npm_root(tmp_path / "npm")):
            assert mcp_locate.find_mcp_server_entry() == str(checkout.resolve())

    def test_npm_is_not_run_when_a_checkout_has_the_build(self, tmp_path, monkeypatch):
        checkout = tmp_path / "index.js"
        checkout.write_text("", encoding="utf-8")
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: checkout)

        def run(*a, **k):
            raise AssertionError("npm should not run")

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() == str(checkout.resolve())

    def test_a_redacted_npm_root_is_found_from_node(self, tmp_path, monkeypatch):
        """npm prints *** for a path segment that looks like a UUID, so its
        root names nothing; node's own path gives the same root."""
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
        monkeypatch.delenv("npm_config_prefix", raising=False)
        prefix = tmp_path / "0f8fad5b-d9cb-469f-a165-70867728950e"
        entry = _global_install(prefix / "lib" / "node_modules")

        redacted = str(tmp_path / "***" / "lib" / "node_modules")

        def run(cmd, *a, **k):
            out = {("npm", "root", "-g"): redacted,
                   ("node", "-p", "process.execPath"): str(prefix / "bin" / "node")}[tuple(cmd)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() == str(entry.resolve())

    def test_node_is_not_trusted_when_its_prefix_is_not_npms(self, tmp_path, monkeypatch):
        """npm's redacted root and node's prefix disagree (npm installs
        elsewhere): node's prefix may hold another copy, so it is not used."""
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
        monkeypatch.delenv("npm_config_prefix", raising=False)
        node_prefix = tmp_path / "node-home"
        _global_install(node_prefix / "lib" / "node_modules")

        def run(cmd, *a, **k):
            out = {("npm", "root", "-g"): str(tmp_path / "npm" / "***" / "lib" / "node_modules"),
                   ("node", "-p", "process.execPath"): str(node_prefix / "bin" / "node")}[tuple(cmd)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() is None

    def test_a_configured_prefix_wins_over_a_matching_node_prefix(self, tmp_path, monkeypatch):
        """Two UUID-named prefixes side by side redact to the same root; with
        NPM_CONFIG_PREFIX set, that prefix is where npm installed."""
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        npm_prefix = tmp_path / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        node_prefix = tmp_path / "0f8fad5b-d9cb-469f-a165-70867728950e"
        wanted = _global_install(npm_prefix / "lib" / "node_modules")
        _global_install(node_prefix / "lib" / "node_modules")
        monkeypatch.setenv("NPM_CONFIG_PREFIX", str(npm_prefix))
        monkeypatch.delenv("npm_config_prefix", raising=False)

        def run(cmd, *a, **k):
            out = {("npm", "root", "-g"): str(tmp_path / "***" / "lib" / "node_modules"),
                   ("node", "-p", "process.execPath"): str(node_prefix / "bin" / "node")}[tuple(cmd)]
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() == str(wanted.resolve())

    def test_node_is_not_asked_when_npm_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        calls = []

        def run(cmd, *a, **k):
            calls.append(cmd[0])
            raise subprocess.CalledProcessError(1, cmd)

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() is None
        assert calls == ["npm"]

    def test_a_package_install_finds_the_npm_global_install(self, tmp_path, monkeypatch):
        """`pip install schist` has no mcp-server/ beside it, so the README's
        `init --print-mcp-config` failed for anyone who installed both
        packages from the registries."""
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        entry = _global_install(tmp_path / "npm")
        with patch("subprocess.run", _npm_root(tmp_path / "npm")):
            assert mcp_locate.find_mcp_server_entry() == str(entry.resolve())

    @pytest.mark.parametrize("failure", [
        FileNotFoundError("npm"),
        subprocess.TimeoutExpired(["npm"], 15),
        subprocess.CalledProcessError(1, ["npm"]),
    ], ids=["no-npm", "timeout", "npm-fails"])
    def test_no_npm_means_not_found(self, tmp_path, monkeypatch, failure):
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")

        def run(*a, **k):
            raise failure

        with patch("subprocess.run", run):
            assert mcp_locate.find_mcp_server_entry() is None

    def test_an_npm_root_without_the_package_is_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
        (tmp_path / "npm").mkdir()
        with patch("subprocess.run", _npm_root(tmp_path / "npm")):
            assert mcp_locate.find_mcp_server_entry() is None


def test_print_mcp_config_uses_the_npm_global_install(tmp_path, monkeypatch, capsys):
    from schist.sync import _print_mcp_config
    monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
    entry = _global_install(tmp_path / "npm")
    args = SimpleNamespace(vault=str(tmp_path / "vault"), identity="local",
                           mcp_server_path=None, mcp_format="claude")
    with patch("subprocess.run", _npm_root(tmp_path / "npm")):
        _print_mcp_config(args)
    assert f"-- node {entry.resolve()}" in capsys.readouterr().out


def test_doctor_uses_the_same_locator(tmp_path, monkeypatch):
    from schist.doctor import _auto_detect_mcp_path
    monkeypatch.setattr(mcp_locate, "_checkout_entry", lambda: tmp_path / "absent.js")
    entry = _global_install(tmp_path / "npm")
    with patch("subprocess.run", _npm_root(tmp_path / "npm")):
        assert _auto_detect_mcp_path() == str(entry.resolve())
