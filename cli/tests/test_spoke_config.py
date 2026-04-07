"""Tests for spoke configuration read/write."""

from schist.spoke_config import SpokeConfig, is_spoke, load_spoke_config, save_spoke_config


class TestSpokeConfig:
    def test_roundtrip(self, tmp_path):
        vault = str(tmp_path / "vault")
        config = SpokeConfig(
            hub="git@pi.local:vault.git",
            identity="cluster-mario",
            scope="research/mario",
        )
        save_spoke_config(vault, config)
        loaded = load_spoke_config(vault)
        assert loaded.hub == config.hub
        assert loaded.identity == config.identity
        assert loaded.scope == config.scope
        assert loaded.scope_convention == "subdirectory"

    def test_is_spoke_true(self, tmp_path):
        vault = str(tmp_path / "vault")
        config = SpokeConfig(hub="url", identity="id", scope="s")
        save_spoke_config(vault, config)
        assert is_spoke(vault) is True

    def test_is_spoke_false(self, tmp_path):
        vault = str(tmp_path / "vault")
        assert is_spoke(vault) is False

    def test_custom_scope_convention(self, tmp_path):
        vault = str(tmp_path / "vault")
        config = SpokeConfig(hub="url", identity="id", scope="s", scope_convention="flat")
        save_spoke_config(vault, config)
        loaded = load_spoke_config(vault)
        assert loaded.scope_convention == "flat"
