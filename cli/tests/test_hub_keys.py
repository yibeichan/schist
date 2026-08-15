"""Tests for authorized_keys identity pinning (hub_keys.py, #502)."""

from __future__ import annotations

import base64
import shutil
from types import SimpleNamespace

import pytest

from schist.hub_admin import HubAdminError
from schist.hub_keys import (
    KeyEntry,
    cmd_key_add,
    cmd_key_list,
    cmd_key_remove,
    key_add,
    key_remove,
    parse_authorized_keys,
    parse_public_key,
    pinned_options,
)

# Syntactically valid base64 blobs (content is irrelevant to the parser).
BLOB_A = base64.b64encode(b"key-material-a").decode()
BLOB_B = base64.b64encode(b"key-material-b").decode()

KEY_A = f"ssh-ed25519 {BLOB_A} alice@laptop"
KEY_B = f"ssh-rsa {BLOB_B} bob@hpc"


class TestParseAuthorizedKeys:
    def test_bare_key(self):
        entries = parse_authorized_keys(KEY_A + "\n")
        assert len(entries) == 1
        e = entries[0]
        assert (e.options, e.keytype, e.blob, e.comment) == (
            None, "ssh-ed25519", BLOB_A, "alice@laptop")

    def test_comments_and_blanks_skipped(self):
        text = f"# a comment\n\n{KEY_A}\n   \n# another\n{KEY_B}\n"
        entries = parse_authorized_keys(text)
        assert [e.line_no for e in entries] == [3, 6]

    def test_options_with_quoted_comma(self):
        line = f'restrict,command="schist-shell pi",no-pty {KEY_A}'
        e = parse_authorized_keys(line)[0]
        assert e.options == 'restrict,command="schist-shell pi",no-pty'
        assert e.keytype == "ssh-ed25519"
        assert e.pinned_identity == "pi"

    def test_command_with_space_inside_quotes(self):
        line = f'command="schist-shell pi",restrict {KEY_A}'
        e = parse_authorized_keys(line)[0]
        assert e.blob == BLOB_A
        assert e.pinned_identity == "pi"

    def test_absolute_path_wrapper_still_pinned(self):
        line = f'restrict,command="/usr/local/bin/schist-shell hpc" {KEY_B}'
        assert parse_authorized_keys(line)[0].pinned_identity == "hpc"

    def test_non_schist_command_is_unpinned(self):
        line = f'command="/usr/bin/false" {KEY_A}'
        assert parse_authorized_keys(line)[0].pinned_identity is None

    def test_lookalike_command_is_unpinned(self):
        # A command merely mentioning schist-shell in an argument must not
        # count as a pin.
        line = f'command="echo schist-shellish pi" {KEY_A}'
        assert parse_authorized_keys(line)[0].pinned_identity is None

    def test_garbage_line_skipped(self):
        assert parse_authorized_keys("not a key at all\n") == []

    def test_sk_keytype(self):
        line = f"sk-ssh-ed25519@openssh.com {BLOB_A} yubikey"
        assert parse_authorized_keys(line)[0].keytype == "sk-ssh-ed25519@openssh.com"


class TestParsePublicKey:
    def test_bare_line(self):
        assert parse_public_key(KEY_A) == ("ssh-ed25519", BLOB_A, "alice@laptop")

    def test_strips_existing_options(self):
        line = f'command="old",no-pty {KEY_A}'
        assert parse_public_key(line) == ("ssh-ed25519", BLOB_A, "alice@laptop")

    def test_rejects_garbage(self):
        with pytest.raises(HubAdminError, match="could not parse"):
            parse_public_key("hello world")

    def test_rejects_multiple_keys(self):
        with pytest.raises(HubAdminError, match="could not parse"):
            parse_public_key(f"{KEY_A}\n{KEY_B}")

    def test_rejects_bad_base64(self):
        with pytest.raises(HubAdminError, match="base64"):
            parse_public_key("ssh-ed25519 !!!notbase64!!! c")


class TestKeyAdd:
    def test_add_to_empty(self):
        text, action = key_add("", "alpha", KEY_A)
        assert action == "added"
        e = parse_authorized_keys(text)[0]
        assert e.pinned_identity == "alpha"
        assert e.options == pinned_options("alpha")
        assert e.comment == "alice@laptop"

    def test_repin_same_blob_replaces_line(self):
        text, _ = key_add("", "alpha", KEY_A)
        text, action = key_add(text, "beta", KEY_A)
        assert action == "repinned"
        entries = parse_authorized_keys(text)
        assert len(entries) == 1
        assert entries[0].pinned_identity == "beta"

    def test_repin_replaces_handwritten_options(self):
        text = f'no-pty,command="something-else" {KEY_A}\n'
        text, action = key_add(text, "alpha", KEY_A)
        assert action == "repinned"
        assert parse_authorized_keys(text)[0].options == pinned_options("alpha")

    def test_add_preserves_unrelated_lines(self):
        original = f"# comment\n{KEY_B}\n"
        text, _ = key_add(original, "alpha", KEY_A)
        assert text.startswith("# comment\n")
        assert len(parse_authorized_keys(text)) == 2

    def test_invalid_identity_rejected(self):
        with pytest.raises(HubAdminError, match="invalid participant name"):
            key_add("", "Not-Valid", KEY_A)


class TestKeyRemove:
    def test_remove_pinned(self):
        text, _ = key_add("", "alpha", KEY_A)
        text, _ = key_add(text, "beta", KEY_B)
        text, removed = key_remove(text, "alpha")
        assert removed == 1
        entries = parse_authorized_keys(text)
        assert [e.pinned_identity for e in entries] == ["beta"]

    def test_remove_leaves_unpinned_keys(self):
        text = KEY_A + "\n"
        new_text, removed = key_remove(text, "alpha")
        assert removed == 0
        assert new_text == text

    def test_remove_multiple_keys_same_identity(self):
        text, _ = key_add("", "alpha", KEY_A)
        text, _ = key_add(text, "alpha", KEY_B)
        text, removed = key_remove(text, "alpha")
        assert removed == 2
        assert parse_authorized_keys(text) == []


class TestFingerprint:
    def test_matches_sha256_shape(self):
        e = KeyEntry(None, "ssh-ed25519", BLOB_A, "", 1)
        assert e.fingerprint.startswith("SHA256:")
        assert not e.fingerprint.endswith("=")

    def test_invalid_blob(self):
        e = KeyEntry(None, "ssh-ed25519", "!!!", "", 1)
        assert e.fingerprint == "(invalid base64)"


needs_git = pytest.mark.skipif(shutil.which("git") is None,
                               reason="git not available")


@needs_git
class TestCmdLayer:
    """cmd_* wrappers against a real bare hub (participants: alpha, beta)."""

    @pytest.fixture()
    def hub(self, tmp_path):
        from schist.sync import init_hub
        hub = tmp_path / "hub.git"
        init_hub(SimpleNamespace(name="test-vault", participant=["alpha", "beta"]),
                 str(hub))
        return hub

    @pytest.fixture()
    def ak(self, tmp_path):
        return tmp_path / "authorized_keys"

    def test_add_list_remove_roundtrip(self, hub, ak, capsys):
        cmd_key_add(SimpleNamespace(participant="alpha", key=KEY_A, key_file=None,
                                    hub_path=str(hub), authorized_keys=str(ak)))
        assert "added" in capsys.readouterr().out
        assert (ak.stat().st_mode & 0o777) == 0o600

        cmd_key_list(SimpleNamespace(hub_path=str(hub), authorized_keys=str(ak)))
        out = capsys.readouterr().out
        assert "alpha" in out and "SHA256:" in out

        cmd_key_remove(SimpleNamespace(participant="alpha", authorized_keys=str(ak)))
        assert "Removed 1" in capsys.readouterr().out
        assert parse_authorized_keys(ak.read_text()) == []

    def test_add_unknown_participant_rejected(self, hub, ak):
        with pytest.raises(HubAdminError, match="unknown participant"):
            cmd_key_add(SimpleNamespace(participant="mallory", key=KEY_A,
                                        key_file=None, hub_path=str(hub),
                                        authorized_keys=str(ak)))
        assert not ak.exists()

    def test_add_from_key_file(self, hub, ak, tmp_path, capsys):
        pub = tmp_path / "id_ed25519.pub"
        pub.write_text(KEY_A + "\n")
        cmd_key_add(SimpleNamespace(participant="beta", key=None,
                                    key_file=str(pub), hub_path=str(hub),
                                    authorized_keys=str(ak)))
        assert parse_authorized_keys(ak.read_text())[0].pinned_identity == "beta"

    def test_list_flags_pin_not_in_vault(self, hub, ak, capsys):
        text, _ = key_add("", "ghost", KEY_A)
        ak.write_text(text)
        cmd_key_list(SimpleNamespace(hub_path=str(hub), authorized_keys=str(ak)))
        assert "NOT IN vault.yaml" in capsys.readouterr().out

    def test_remove_missing_is_noop(self, ak, capsys):
        ak.write_text(KEY_A + "\n")
        cmd_key_remove(SimpleNamespace(participant="alpha", authorized_keys=str(ak)))
        assert "no change" in capsys.readouterr().out
