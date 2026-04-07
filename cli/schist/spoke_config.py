"""Spoke configuration persistence (.schist/spoke.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

SPOKE_CONFIG_FILE = ".schist/spoke.yaml"


@dataclass
class SpokeConfig:
    hub: str
    identity: str
    scope: str
    scope_convention: str = "subdirectory"


def spoke_config_path(vault_path: str) -> Path:
    return Path(vault_path) / SPOKE_CONFIG_FILE


def is_spoke(vault_path: str) -> bool:
    return spoke_config_path(vault_path).is_file()


def load_spoke_config(vault_path: str) -> SpokeConfig:
    path = spoke_config_path(vault_path)
    with open(path) as f:
        data = yaml.safe_load(f)
    return SpokeConfig(
        hub=data["hub"],
        identity=data["identity"],
        scope=data["scope"],
        scope_convention=data.get("scope_convention", "subdirectory"),
    )


def save_spoke_config(vault_path: str, config: SpokeConfig) -> None:
    path = spoke_config_path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "hub": config.hub,
        "identity": config.identity,
        "scope": config.scope,
        "scope_convention": config.scope_convention,
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
