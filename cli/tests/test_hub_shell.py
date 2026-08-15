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

    def test_upload_archive(self):
        argv, _ = build_exec("laptop", "git-upload-archive '/srv/git/vault.git'")
        assert argv[3].startswith("git-upload-archive ")

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
        assert ALLOWED_VERBS == {
            "git-receive-pack", "git-upload-pack", "git-upload-archive",
        }


class TestMain:
    def test_usage_without_identity(self, capsys):
        assert main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_usage_with_extra_args(self, capsys):
        assert main(["pi", "extra"]) == 2

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
