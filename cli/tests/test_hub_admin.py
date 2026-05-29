"""Tests for the schist hub admin CLI (hub_admin.py)."""

from __future__ import annotations

import copy

import pytest

from schist import hub_admin
from schist.hub_admin import HubAdminError


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
