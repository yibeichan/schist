"""Tests for the schist hub admin CLI (hub_admin.py)."""

from __future__ import annotations

import copy
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from schist import hub_admin
from schist.hub_admin import HubAdminError


def _has_git():
    return shutil.which("git") is not None


needs_git = pytest.mark.skipif(not _has_git(), reason="git not available")


def _make_hub(tmp_path):
    """Build a real bare hub via init_hub and return its path."""
    from schist.sync import init_hub
    hub = tmp_path / "hub.git"
    init_hub(SimpleNamespace(name="test-vault", participant=["alpha", "beta"]), str(hub))
    return hub


def _hub_vault_text(hub):
    return subprocess.run(
        ["git", "--git-dir", str(hub), "show", "HEAD:vault.yaml"],
        capture_output=True, text=True, check=True,
    ).stdout


def _hub_head(hub):
    return subprocess.run(
        ["git", "--git-dir", str(hub), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _seed_data():
    """A minimal valid vault.yaml dict matching _build_seed_vault output."""
    return {
        "vault_version": 1,
        "name": "test-vault",
        "scope_convention": "flat",
        "participants": [
            {"name": "alpha", "type": "spoke", "default_scope": "global"},
            {"name": "beta", "type": "spoke", "default_scope": "global"},
        ],
        "access": {
            "alpha": {"read": ["*"], "write": ["research", "notes"]},
            "beta": {"read": ["*"], "write": ["research", "notes"]},
        },
    }


class TestGrantWrite:
    def test_adds_scope(self):
        data = _seed_data()
        changed = hub_admin.grant_write(data, "alpha", "ops")
        assert changed is True
        assert "ops" in data["access"]["alpha"]["write"]

    def test_idempotent_returns_false(self):
        data = _seed_data()
        changed = hub_admin.grant_write(data, "alpha", "research")
        assert changed is False
        assert data["access"]["alpha"]["write"].count("research") == 1

    def test_refuses_wildcard(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="refusing to grant"):
            hub_admin.grant_write(data, "alpha", "*")

    def test_unknown_participant(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.grant_write(data, "ghost", "ops")

    def test_invalid_scope_syntax(self):
        data = _seed_data()
        with pytest.raises(Exception):  # ACLError from _validate_scope
            hub_admin.grant_write(data, "alpha", "ops/")


class TestRevokeWrite:
    def test_removes_scope(self):
        data = _seed_data()
        changed = hub_admin.revoke_write(data, "alpha", "notes")
        assert changed is True
        assert "notes" not in data["access"]["alpha"]["write"]

    def test_absent_scope_returns_false(self):
        data = _seed_data()
        changed = hub_admin.revoke_write(data, "alpha", "ops")
        assert changed is False

    def test_allows_emptying_write_list(self):
        data = _seed_data()
        hub_admin.revoke_write(data, "alpha", "research")
        hub_admin.revoke_write(data, "alpha", "notes")
        assert data["access"]["alpha"]["write"] == []

    def test_unknown_participant(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.revoke_write(data, "ghost", "notes")


class TestParticipantAdd:
    def test_adds_participant_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_add(data, "gamma", write=["ops"])
        assert changed is True
        assert hub_admin._participant_index(data, "gamma") is not None
        assert data["access"]["gamma"] == {"read": ["*"], "write": ["ops"]}

    def test_default_write_is_empty(self):
        data = _seed_data()
        hub_admin.participant_add(data, "gamma")
        assert data["access"]["gamma"]["write"] == []
        assert data["access"]["gamma"]["read"] == ["*"]

    def test_rejects_existing_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="already exists"):
            hub_admin.participant_add(data, "alpha")

    def test_rejects_invalid_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="invalid participant name"):
            hub_admin.participant_add(data, "Bad_Name")

    def test_refuses_wildcard_write(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="refusing"):
            hub_admin.participant_add(data, "gamma", write=["*"])


class TestParticipantRename:
    def test_rekeys_entry_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_rename(data, "alpha", "alpha-laptop")
        assert changed is True
        assert hub_admin._participant_index(data, "alpha") is None
        assert hub_admin._participant_index(data, "alpha-laptop") is not None
        assert "alpha" not in data["access"]
        assert data["access"]["alpha-laptop"]["write"] == ["research", "notes"]

    def test_unknown_old(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.participant_rename(data, "ghost", "new")

    def test_new_already_exists(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="already exists"):
            hub_admin.participant_rename(data, "alpha", "beta")

    def test_rejects_invalid_new_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="invalid participant name"):
            hub_admin.participant_rename(data, "alpha", "Bad_Name")


class TestParticipantRemove:
    def test_drops_entry_and_access(self):
        data = _seed_data()
        changed = hub_admin.participant_remove(data, "beta")
        assert changed is True
        assert hub_admin._participant_index(data, "beta") is None
        assert "beta" not in data["access"]

    def test_unknown_name(self):
        data = _seed_data()
        with pytest.raises(HubAdminError, match="unknown participant"):
            hub_admin.participant_remove(data, "ghost")


@needs_git
class TestApplyMutation:
    def test_commits_change_and_roundtrips(self, tmp_path):
        from schist.acl import parse_vault_data
        import yaml
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)

        # "projects" is not in the default seed grant, so this is a real change.
        committed = hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "projects"), "hub: grant alpha write:projects"
        )
        assert committed is True
        assert _hub_head(hub) != before  # advanced

        acl = parse_vault_data(yaml.safe_load(_hub_vault_text(hub)))
        assert acl.can_write("alpha", "projects") is True

    def test_noop_does_not_commit(self, tmp_path):
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)
        committed = hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "research"), "hub: noop"
        )
        assert committed is False
        assert _hub_head(hub) == before  # unchanged

    def test_cas_aborts_on_stale_old_sha(self, tmp_path):
        hub = _make_hub(tmp_path)
        stale = _hub_head(hub)
        # Advance the ref so `stale` is no longer HEAD.
        # Use "projects" (not in default seed grant) to ensure a real commit.
        hub_admin.apply_mutation(
            hub, lambda d: hub_admin.grant_write(d, "alpha", "projects"), "advance"
        )
        # `stale` is a real (valid) commit, so commit-tree -p succeeds; the
        # update-ref compare-and-swap is what must fail because the ref moved.
        _, text = hub_admin.read_hub_vault(hub)
        with pytest.raises(HubAdminError, match="hub changed"):
            hub_admin.commit_vault_yaml(hub, text, "msg", expected_old_sha=stale)

    def test_invalid_result_is_rejected(self, tmp_path):
        hub = _make_hub(tmp_path)
        before = _hub_head(hub)

        def break_it(d):
            d["participants"] = []  # acl.py rejects empty participants
            return True

        with pytest.raises(HubAdminError, match="invalid vault.yaml"):
            hub_admin.apply_mutation(hub, break_it, "hub: break")
        assert _hub_head(hub) == before  # nothing committed
