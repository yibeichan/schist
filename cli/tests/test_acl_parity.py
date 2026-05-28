"""Parity fixtures: assert VaultACL.can_write matches the cases.json contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schist.acl import parse_vault_yaml

FIXTURES_DIR = Path(__file__).parent.parent / "schist" / "acl-fixtures"


def _fixture_pairs() -> list[tuple[Path, Path]]:
    return sorted(
        (yaml_path, yaml_path.with_suffix(".cases.json"))
        for yaml_path in FIXTURES_DIR.glob("*.yaml")
    )


@pytest.mark.parametrize("yaml_path,cases_path", _fixture_pairs())
def test_can_write_matches_fixture(yaml_path: Path, cases_path: Path) -> None:
    acl = parse_vault_yaml(yaml_path)
    cases = json.loads(cases_path.read_text())
    for case in cases:
        actual = acl.can_write(case["identity"], case["scope"])
        assert actual == case["canWrite"], (
            f"{yaml_path.name}: identity={case['identity']!r} "
            f"scope={case['scope']!r} expected {case['canWrite']} got {actual}"
        )
