"""Tests for vault.yaml ACL parser and scope resolution."""

import copy
import warnings

import pytest

from schist.acl import ACLError, VaultACL, parse_vault_data, parse_vault_yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_V1 = {
    "name": "test-vault",
    "vault_version": 1,
    "domains": ["ai", "security"],
    "scope_convention": "subdirectory",
    "participants": [
        {"name": "alice", "type": "agent", "default_scope": "global"},
        {
            "name": "cluster-bob",
            "type": "spoke",
            "default_scope": "research",
            "transport": "git-only",
            "metadata": {"project": "bob"},
        },
    ],
    "access": {
        "alice": {"read": ["*"], "write": ["*"]},
        "cluster-bob": {"read": ["*"], "write": ["research/bob"]},
    },
}

V0_MINIMAL = {
    "name": "old-vault",
    "domains": ["ai"],
    "scope_convention": "subdirectory",
    "participants": [
        {"name": "eleven", "default_scope": "global"},
    ],
    "access": {
        "eleven": {"read": ["*"], "write": ["*"]},
    },
}


def _v1(**overrides):
    d = copy.deepcopy(VALID_V1)
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Valid v1 parsing
# ---------------------------------------------------------------------------


class TestValidV1:
    def test_parse_returns_vault_acl(self):
        acl = parse_vault_data(VALID_V1)
        assert isinstance(acl, VaultACL)
        assert acl.name == "test-vault"
        assert acl.vault_version == 1
        assert acl.scope_convention == "subdirectory"

    def test_participants_parsed(self):
        acl = parse_vault_data(VALID_V1)
        assert len(acl.participants) == 2
        alice = acl.get_participant("alice")
        assert alice is not None
        assert alice.type == "agent"
        assert alice.transport == "ssh-and-git"
        assert alice.metadata == {}

        bob = acl.get_participant("cluster-bob")
        assert bob is not None
        assert bob.type == "spoke"
        assert bob.transport == "git-only"
        assert bob.metadata == {"project": "bob"}

    def test_access_parsed(self):
        acl = parse_vault_data(VALID_V1)
        assert acl.access["alice"].read == ["*"]
        assert acl.access["alice"].write == ["*"]
        assert acl.access["cluster-bob"].write == ["research/bob"]

    def test_domains_parsed(self):
        acl = parse_vault_data(VALID_V1)
        assert acl.domains == ["ai", "security"]

    def test_rate_limits_defaults(self):
        acl = parse_vault_data(VALID_V1)
        assert acl.rate_limits == {}

    def test_rate_limits_parsed(self):
        data = _v1(rate_limits={
            "cluster-bob": {"git_syncs_per_hour": 5, "notes_per_sync": 10},
        })
        acl = parse_vault_data(data)
        rl = acl.rate_limits["cluster-bob"]
        assert rl.git_syncs_per_hour == 5
        assert rl.mcp_writes_per_hour == 100  # default
        assert rl.notes_per_sync == 10

    def test_parse_live_vault_yaml(self):
        """Parse the real vault.yaml from the schist-vault repo."""
        import os
        vault_path = os.path.expanduser("~/Projects/GitHub/schist-vault/vault.yaml")
        if not os.path.exists(vault_path):
            pytest.skip("Live vault.yaml not available")
        # v0 vault — should parse with warning
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            acl = parse_vault_yaml(vault_path)
        assert acl.vault_version == 0
        assert len(acl.participants) >= 1


# ---------------------------------------------------------------------------
# Validation checklist (14 items)
# ---------------------------------------------------------------------------


class TestValidation:
    """Each test corresponds to a validation checklist item from the spec."""

    # 1. vault_version exists and is a known integer
    def test_reject_unknown_vault_version(self):
        with pytest.raises(ACLError, match="Unsupported vault_version"):
            parse_vault_data(_v1(vault_version=99))

    def test_reject_string_vault_version(self):
        with pytest.raises(ACLError, match="Unsupported vault_version"):
            parse_vault_data(_v1(vault_version="1"))

    # 2. participants list is non-empty
    def test_reject_empty_participants(self):
        with pytest.raises(ACLError, match="non-empty"):
            parse_vault_data(_v1(participants=[]))

    def test_reject_missing_participants(self):
        data = _v1()
        del data["participants"]
        with pytest.raises(ACLError, match="non-empty"):
            parse_vault_data(data)

    # 3. Every participant name is unique
    def test_reject_duplicate_names(self):
        data = _v1(participants=[
            {"name": "alice"},
            {"name": "alice"},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="duplicate"):
            parse_vault_data(data)

    # 4. Every participant name matches pattern
    def test_reject_invalid_name_uppercase(self):
        data = _v1(participants=[
            {"name": "Alice"},
        ], access={
            "Alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="must match"):
            parse_vault_data(data)

    def test_reject_invalid_name_starts_with_number(self):
        data = _v1(participants=[
            {"name": "1abc"},
        ], access={
            "1abc": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="must match"):
            parse_vault_data(data)

    # 5. Every participant has a corresponding access entry
    def test_reject_participant_without_access(self):
        data = _v1()
        data["participants"].append({"name": "orphan"})
        with pytest.raises(ACLError, match="orphan.*no access"):
            parse_vault_data(data)

    # 6. Every access key has a corresponding participant
    def test_reject_access_without_participant(self):
        data = _v1()
        data["access"]["ghost"] = {"read": ["*"], "write": ["*"]}
        with pytest.raises(ACLError, match="ghost.*no corresponding participant"):
            parse_vault_data(data)

    # 7. access.<name>.read is a non-empty list
    def test_reject_empty_read(self):
        data = _v1()
        data["access"]["alice"]["read"] = []
        with pytest.raises(ACLError, match="read must be a non-empty list"):
            parse_vault_data(data)

    def test_reject_missing_read(self):
        data = _v1()
        del data["access"]["alice"]["read"]
        with pytest.raises(ACLError, match="read must be a non-empty list"):
            parse_vault_data(data)

    # 8. access.<name>.write is a non-empty list
    def test_reject_empty_write(self):
        data = _v1()
        data["access"]["alice"]["write"] = []
        with pytest.raises(ACLError, match="write must be a non-empty list"):
            parse_vault_data(data)

    # 9. Scope values — syntax validation (no filesystem check)
    def test_reject_scope_leading_slash(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["/research"]
        with pytest.raises(ACLError, match="Invalid scope syntax"):
            parse_vault_data(data)

    def test_reject_scope_trailing_slash(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["research/"]
        with pytest.raises(ACLError, match="Invalid scope syntax"):
            parse_vault_data(data)

    # 10. No partial wildcards
    def test_reject_partial_wildcard(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["research/*"]
        with pytest.raises(ACLError, match="Partial wildcards"):
            parse_vault_data(data)

    def test_reject_partial_wildcard_prefix(self):
        data = _v1()
        data["access"]["alice"]["read"] = ["*research"]
        with pytest.raises(ACLError, match="Partial wildcards"):
            parse_vault_data(data)

    # 11. scope_convention is known
    def test_reject_unknown_scope_convention(self):
        with pytest.raises(ACLError, match="scope_convention"):
            parse_vault_data(_v1(scope_convention="flat"))

    # 12. type values are agent or spoke
    def test_reject_invalid_type(self):
        data = _v1(participants=[
            {"name": "alice", "type": "hub"},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="type must be one of"):
            parse_vault_data(data)

    # 13. transport values are ssh-and-git or git-only
    def test_reject_invalid_transport(self):
        data = _v1(participants=[
            {"name": "alice", "transport": "http"},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="transport must be one of"):
            parse_vault_data(data)


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------


class TestScopeResolution:
    @pytest.fixture()
    def acl(self):
        data = {
            "name": "scope-test",
            "vault_version": 1,
            "scope_convention": "subdirectory",
            "participants": [
                {"name": "admin"},
                {"name": "researcher"},
                {"name": "narrow"},
                {"name": "reader"},
            ],
            "access": {
                "admin": {"read": ["*"], "write": ["*"]},
                "researcher": {"read": ["*"], "write": ["research"]},
                "narrow": {"read": ["research/mario"], "write": ["research/mario"]},
                "reader": {"read": ["research", "health"], "write": ["health"]},
            },
        }
        return parse_vault_data(data)

    # Wildcard
    def test_wildcard_matches_all_write(self, acl):
        assert acl.can_write("admin", "anything")
        assert acl.can_write("admin", "research/mario")

    def test_wildcard_matches_all_read(self, acl):
        assert acl.can_read("admin", "anything")
        assert acl.can_read("admin", "research/mario/deep")

    # Parent grants child (write)
    def test_parent_grants_child_write(self, acl):
        assert acl.can_write("researcher", "research")
        assert acl.can_write("researcher", "research/mario")
        assert acl.can_write("researcher", "research/mario/deep")

    def test_write_no_match_denied(self, acl):
        assert not acl.can_write("researcher", "security")
        assert not acl.can_write("researcher", "health")

    # Parent grants child (read) — same inheritance as write
    def test_parent_grants_child_read(self, acl):
        assert acl.can_read("reader", "research")
        assert acl.can_read("reader", "research/mario")
        assert acl.can_read("reader", "research/mario/deep")

    def test_read_no_match_denied(self, acl):
        assert not acl.can_read("reader", "security")
        assert not acl.can_read("reader", "ops")

    # Narrow scope — no upward grant
    def test_narrow_cannot_write_parent(self, acl):
        assert acl.can_write("narrow", "research/mario")
        assert acl.can_write("narrow", "research/mario/sub")
        assert not acl.can_write("narrow", "research")
        assert not acl.can_write("narrow", "research/hbcd")

    def test_narrow_cannot_read_parent(self, acl):
        assert acl.can_read("narrow", "research/mario")
        assert not acl.can_read("narrow", "research")

    # Unknown identity
    def test_unknown_identity_denied(self, acl):
        assert not acl.can_read("nobody", "research")
        assert not acl.can_write("nobody", "research")

    # Exact match
    def test_exact_scope_match(self, acl):
        assert acl.can_read("reader", "health")
        assert acl.can_write("reader", "health")


# ---------------------------------------------------------------------------
# Backward compatibility (v0)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_v0_parses_with_warning(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            acl = parse_vault_data(V0_MINIMAL)
        assert acl.vault_version == 0
        assert len(w) == 1
        assert "no vault_version" in str(w[0].message)

    def test_v0_defaults_type_agent(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            acl = parse_vault_data(V0_MINIMAL)
        assert acl.participants[0].type == "agent"

    def test_v0_defaults_transport(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            acl = parse_vault_data(V0_MINIMAL)
        assert acl.participants[0].transport == "ssh-and-git"

    def test_v0_defaults_metadata_empty(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            acl = parse_vault_data(V0_MINIMAL)
        assert acl.participants[0].metadata == {}

    def test_v0_defaults_rate_limits(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            acl = parse_vault_data(V0_MINIMAL)
        assert acl.rate_limits == {}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_domains_list(self):
        data = _v1(domains=[])
        acl = parse_vault_data(data)
        assert acl.domains == []

    def test_no_domains_key(self):
        data = _v1()
        del data["domains"]
        acl = parse_vault_data(data)
        assert acl.domains == []

    def test_scope_with_multiple_segments(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["a/b/c"]
        acl = parse_vault_data(data)
        assert acl.can_write("alice", "a/b/c")
        assert acl.can_write("alice", "a/b/c/d")
        assert not acl.can_write("alice", "a/b")

    def test_non_dict_raises(self):
        with pytest.raises(ACLError, match="must be a YAML mapping"):
            parse_vault_data("not a dict")  # type: ignore

    def test_scope_similar_prefix_no_false_match(self):
        """research-extra should NOT match scope 'research'."""
        data = _v1()
        data["access"]["alice"]["write"] = ["research"]
        acl = parse_vault_data(data)
        assert acl.can_write("alice", "research")
        assert acl.can_write("alice", "research/sub")
        assert not acl.can_write("alice", "research-extra")

    def test_multiple_errors_collected(self):
        """Parser collects multiple errors into a single exception."""
        data = _v1(participants=[
            {"name": "Alice"},
            {"name": "Bob"},
        ], access={
            "Alice": {"read": ["*"], "write": ["*"]},
            "Bob": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError) as exc_info:
            parse_vault_data(data)
        msg = str(exc_info.value)
        assert "Alice" in msg
        assert "Bob" in msg


# ---------------------------------------------------------------------------
# New validation tests (code review fixes)
# ---------------------------------------------------------------------------


class TestScopeValidation:
    """Tests for path traversal, double-slash, and scope syntax rejection."""

    def test_reject_path_traversal_simple(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["research/.."]
        with pytest.raises(ACLError, match="Path traversal"):
            parse_vault_data(data)

    def test_reject_path_traversal_mid_path(self):
        data = _v1()
        data["access"]["alice"]["read"] = ["research/../secrets"]
        with pytest.raises(ACLError, match="Path traversal"):
            parse_vault_data(data)

    def test_reject_path_traversal_start(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["../etc"]
        with pytest.raises(ACLError, match="Path traversal"):
            parse_vault_data(data)

    def test_allow_dots_in_name(self):
        """Double dots inside a segment name (e.g. 'foo..bar') are fine."""
        data = _v1()
        data["access"]["alice"]["write"] = ["foo..bar"]
        acl = parse_vault_data(data)
        assert acl.can_write("alice", "foo..bar")

    def test_reject_double_slash(self):
        data = _v1()
        data["access"]["alice"]["write"] = ["research//mario"]
        with pytest.raises(ACLError, match="Invalid scope syntax"):
            parse_vault_data(data)


class TestRateLimitValidation:
    """Tests for rate limit value and key validation."""

    def test_reject_negative_rate_limit(self):
        data = _v1(rate_limits={
            "alice": {"git_syncs_per_hour": -1},
        })
        with pytest.raises(ACLError, match="positive integer"):
            parse_vault_data(data)

    def test_reject_zero_rate_limit(self):
        data = _v1(rate_limits={
            "alice": {"notes_per_sync": 0},
        })
        with pytest.raises(ACLError, match="positive integer"):
            parse_vault_data(data)

    def test_reject_non_integer_rate_limit(self):
        data = _v1(rate_limits={
            "alice": {"mcp_writes_per_hour": 3.5},
        })
        with pytest.raises(ACLError, match="positive integer"):
            parse_vault_data(data)

    def test_reject_orphaned_rate_limit_key(self):
        data = _v1(rate_limits={
            "nobody": {"git_syncs_per_hour": 5},
        })
        with pytest.raises(ACLError, match="no corresponding participant"):
            parse_vault_data(data)


class TestDomainValidation:
    def test_reject_non_string_domain(self):
        data = _v1(domains=["ai", 42])
        with pytest.raises(ACLError, match="domains.*must be a string"):
            parse_vault_data(data)


class TestDefaultScopeValidation:
    def test_reject_empty_default_scope(self):
        data = _v1(participants=[
            {"name": "alice", "default_scope": ""},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="invalid default_scope"):
            parse_vault_data(data)

    def test_reject_traversal_default_scope(self):
        data = _v1(participants=[
            {"name": "alice", "default_scope": "research/../secrets"},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="invalid default_scope"):
            parse_vault_data(data)

    def test_global_default_scope_ok(self):
        """'global' is special-cased and should not be validated as a scope."""
        data = _v1()
        acl = parse_vault_data(data)
        assert acl.get_participant("alice").default_scope == "global"


class TestMetadataValidation:
    def test_reject_non_string_metadata_value(self):
        data = _v1(participants=[
            {"name": "alice", "metadata": {"count": 42}},
        ], access={
            "alice": {"read": ["*"], "write": ["*"]},
        })
        with pytest.raises(ACLError, match="metadata value.*must be a string"):
            parse_vault_data(data)
