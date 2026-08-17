"""Tests for schist-shell, the identity-pinning SSH forced command (#502)."""

from __future__ import annotations

import pytest

from schist.hub_shell import ALLOWED_VERBS, HubShellError, build_exec, main


class TestBuildExec:
    def test_receive_pack(self):
        argv, env = build_exec("pi", "git-receive-pack '/srv/git/vault.git'")
        assert argv == ["git", "shell", "-c", "git-receive-pack '/srv/git/vault.git'"]
        assert env == {"SCHIST_IDENTITY": "pi", "SCHIST_IDENTITY_PINNED": "1"}

    def test_upload_pack(self):
        argv, _ = build_exec("laptop", "git-upload-pack '/srv/git/vault.git'")
        assert argv[3].startswith("git-upload-pack ")

    def test_upload_archive_rejected(self):
        # schist sync never uses `git archive --remote`; keep the surface at
        # push/fetch only.
        with pytest.raises(HubShellError, match="not permitted"):
            build_exec("laptop", "git-upload-archive '/srv/git/vault.git'")

    def test_two_word_form_normalized(self):
        argv, _ = build_exec("pi", "git receive-pack '/srv/git/vault.git'")
        assert argv[3] == "git-receive-pack '/srv/git/vault.git'"

    def test_path_with_spaces_requoted(self):
        argv, _ = build_exec("pi", "git-upload-pack '/srv/git/my vault.git'")
        assert argv[3] == "git-upload-pack '/srv/git/my vault.git'"

    def test_identity_overrides_client_assertion(self):
        # The env dict is what schist-shell writes AFTER sshd applied any
        # client SendEnv — the pinned name always wins.
        _, env = build_exec("cluster-a", "git-receive-pack 'x.git'")
        assert env["SCHIST_IDENTITY"] == "cluster-a"

    @pytest.mark.parametrize("cmd", [
        "bash",
        "scp -t /tmp",
        "git-receive-pack",  # missing repo argument
        "git-receive-pack a.git b.git",  # extra argument
        "rm -rf /; git-upload-pack x.git",
        "git shell -c whoami",
        "GIT_DIR=/x git-receive-pack y.git",
    ])
    def test_rejects_non_transport_commands(self, cmd):
        with pytest.raises(HubShellError, match="not permitted|empty"):
            build_exec("pi", cmd)

    def test_shell_metachars_neutralized_by_shlex(self):
        # shlex.split keeps `;` inside quotes as literal argument text; the
        # rebuild re-quotes it so `git shell -c` sees a single path token.
        argv, _ = build_exec("pi", "git-upload-pack 'x.git; rm -rf /'")
        assert argv[3] == "git-upload-pack 'x.git; rm -rf /'"

    def test_rejects_interactive_session(self):
        with pytest.raises(HubShellError, match="interactive shell disabled"):
            build_exec("pi", None)
        with pytest.raises(HubShellError, match="interactive shell disabled"):
            build_exec("pi", "")

    @pytest.mark.parametrize("identity", ["", "Pi", "pi;x", "-pi", "a b", "1abc"])
    def test_rejects_invalid_identity(self, identity):
        with pytest.raises(HubShellError, match="invalid pinned identity"):
            build_exec(identity, "git-receive-pack 'x.git'")

    def test_unparseable_quoting_rejected(self):
        with pytest.raises(HubShellError, match="unparseable"):
            build_exec("pi", "git-receive-pack 'unterminated")

    def test_allowed_verbs_are_transport_only(self):
        assert ALLOWED_VERBS == {"git-receive-pack", "git-upload-pack"}

    def test_repo_binding_allows_matching_repo(self, tmp_path):
        repo = tmp_path / "vault.git"
        repo.mkdir()
        argv, _ = build_exec(
            "pi", f"git-receive-pack '{repo}'", allowed_repo=str(repo))
        assert argv[3].startswith("git-receive-pack ")

    def test_repo_binding_normalizes_spelling(self, tmp_path, monkeypatch):
        # Client says '~/vault.git' (relative to the SSH home); the baked pin
        # is absolute. Same repo, different spelling — must be allowed.
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = tmp_path / "vault.git"
        repo.mkdir()
        argv, _ = build_exec(
            "pi", "git-receive-pack 'vault.git'", allowed_repo=str(repo))
        assert argv[3].startswith("git-receive-pack ")

    def test_repo_binding_rejects_other_repo(self, tmp_path):
        a = tmp_path / "vault-a.git"; a.mkdir()
        b = tmp_path / "vault-b.git"; b.mkdir()
        with pytest.raises(HubShellError, match="confined to repository"):
            build_exec("pi", f"git-receive-pack '{b}'", allowed_repo=str(a))

    def test_scrub_environ_strips_injection_vectors(self):
        from schist.hub_shell import scrub_environ
        env = {"LD_PRELOAD": "/evil.so", "DYLD_INSERT_LIBRARIES": "/e.dylib",
               "BASH_ENV": "/evil.sh", "ENV": "/evil.sh",
               "GIT_EXEC_PATH": "/evil-git", "GIT_CONFIG_GLOBAL": "/evil.cfg",
               "GIT_SSH_COMMAND": "evil", "GIT_ASKPASS": "/evil-askpass",
               "PATH": "/usr/bin", "GIT_PROTOCOL": "version=2",
               "SCHIST_IDENTITY": "pi"}
        removed = scrub_environ(env)
        assert sorted(removed) == ["BASH_ENV", "DYLD_INSERT_LIBRARIES", "ENV",
                                   "GIT_ASKPASS", "GIT_CONFIG_GLOBAL",
                                   "GIT_EXEC_PATH", "GIT_SSH_COMMAND",
                                   "LD_PRELOAD"]
        # GIT_PROTOCOL (protocol v2) and PATH (doctor-policed, legit hub-side
        # uses) deliberately survive — see the #516 comment in hub_shell.py.
        assert set(env) == {"PATH", "GIT_PROTOCOL", "SCHIST_IDENTITY"}


class TestMain:
    def test_usage_without_identity(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_usage_with_three_args(self, capsys):
        assert main(["pi", "repo.git", "extra"]) == 2

    def test_rejection_is_clean_error(self, capsys, monkeypatch):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "bash")
        assert main(["pi"]) == 1
        err = capsys.readouterr().err
        assert "not permitted" in err
        assert "Traceback" not in err

    def test_no_original_command_rejected(self, capsys, monkeypatch):
        monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
        assert main(["pi"]) == 1
        assert "interactive shell disabled" in capsys.readouterr().err

    def test_two_arg_repo_bound_success_execs(self, monkeypatch, tmp_path):
        # #515: main()'s 2-arg (repo-confined) path was only covered via
        # build_exec, never through the console-script entry point.
        repo = tmp_path / "vault.git"
        repo.mkdir()
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"git-receive-pack '{repo}'")
        recorded = {}

        def fake_execvp(file, argv):
            recorded["argv"] = argv
            raise SystemExit(0)

        monkeypatch.setattr("os.execvp", fake_execvp)
        with pytest.raises(SystemExit):
            main(["pi", str(repo)])
        assert recorded["argv"][3].startswith("git-receive-pack ")

    def test_two_arg_repo_mismatch_rejected(self, monkeypatch, tmp_path, capsys):
        a = tmp_path / "a.git"; a.mkdir()
        b = tmp_path / "b.git"; b.mkdir()
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"git-receive-pack '{b}'")
        assert main(["pi", str(a)]) == 1
        assert "confined to repository" in capsys.readouterr().err

    def test_success_sets_env_and_execs(self, monkeypatch):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "git-receive-pack 'v.git'")
        monkeypatch.setenv("SCHIST_IDENTITY", "attacker-asserted")
        recorded = {}

        def fake_execvp(file, argv):
            import os
            recorded["argv"] = argv
            recorded["identity"] = os.environ["SCHIST_IDENTITY"]
            recorded["pinned"] = os.environ["SCHIST_IDENTITY_PINNED"]
            raise SystemExit(0)  # execvp never returns; simulate process handoff

        monkeypatch.setattr("os.execvp", fake_execvp)
        with pytest.raises(SystemExit):
            main(["pi"])
        assert recorded["argv"] == ["git", "shell", "-c", "git-receive-pack 'v.git'"]
        assert recorded["identity"] == "pi"  # client assertion overwritten
        assert recorded["pinned"] == "1"
