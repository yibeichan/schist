"""Tests for the strict env_flag helper (#311 item 1).

Plain truthiness (`if os.environ.get(NAME)`) treats NAME=0 / NAME=false as
*enabled*; env_flag must read those as disabled.
"""

from __future__ import annotations

import pytest

from schist.env_utils import env_flag

_VAR = "SCHIST_TEST_FLAG"


@pytest.mark.parametrize("value", [
    "", "0", "false", "False", "FALSE", "no", "No", "off", "OFF",
    "  0  ", " false ",
])
def test_env_flag_falsy_values(monkeypatch, value):
    monkeypatch.setenv(_VAR, value)
    assert env_flag(_VAR) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "2", "anything"])
def test_env_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv(_VAR, value)
    assert env_flag(_VAR) is True


def test_env_flag_unset_is_false(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    assert env_flag(_VAR) is False
