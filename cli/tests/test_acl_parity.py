"""Parity fixtures: assert VaultACL.can_write matches the cases.json contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from schist.acl import parse_vault_yaml, ACLError
from schist.pre_receive import resolve_identity

FIXTURES_DIR = Path(__file__).parent.parent / "schist" / "acl-fixtures"
IDENTITY_CASES = json.loads((FIXTURES_DIR / "identity-resolution.cases.json").read_text())


def _fixture_pairs() -> list:
    return sorted(
        (
            pytest.param(
                yaml_path,
                yaml_path.with_suffix(".cases.json"),
                id=yaml_path.stem,
            )
            for yaml_path in FIXTURES_DIR.glob("*.yaml")
        ),
        key=lambda p: p.id,
    )


@pytest.mark.parametrize("yaml_path,cases_path", _fixture_pairs())
def test_acl_matches_fixture(yaml_path: Path, cases_path: Path) -> None:
    assert cases_path.exists(), f"Missing cases file: {cases_path.name}"
    cases = json.loads(cases_path.read_text())

    # Reject fixture: the strict parser must raise, mirroring TS returning None.
    # A dict-shaped cases file is ALWAYS a reject fixture; guard against a typo'd
    # key or `reject: false` silently falling through to the accept loop (which
    # would crash on `case["identity"]` with an unhelpful error).
    if isinstance(cases, dict):
        assert cases.get("reject") is True, (
            f"{cases_path.name}: dict-shaped cases file must be "
            f'{{"reject": true, ...}}, got {cases!r}'
        )
        with pytest.raises(ACLError):
            parse_vault_yaml(yaml_path)
        return

    acl = parse_vault_yaml(yaml_path)
    for case in cases:
        actual = acl.can_write(case["identity"], case["scope"])
        assert actual == case["canWrite"], (
            f"{yaml_path.name}: identity={case['identity']!r} "
            f"scope={case['scope']!r} expected {case['canWrite']} got {actual}"
        )


@pytest.mark.parametrize("case", IDENTITY_CASES, ids=[c["name"] for c in IDENTITY_CASES])
def test_hub_identity_resolution_matches_fixture(case, monkeypatch) -> None:
    monkeypatch.delenv("SCHIST_IDENTITY", raising=False)
    monkeypatch.delenv("GL_USER", raising=False)
    for key, value in case["env"].items():
        monkeypatch.setenv(key, value)

    assert resolve_identity() == case["hubIdentity"]
