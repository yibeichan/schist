"""Small shared environment-variable helpers."""

import os

_FALSY_VALUES = frozenset({"", "0", "false", "no", "off"})


def env_flag(name: str) -> bool:
    """Strict boolean read of an environment variable.

    Unset, '', '0', 'false', 'no', 'off' (case-insensitive, surrounding
    whitespace ignored) are False; any other value is True. Plain truthiness
    (`if os.environ.get(NAME)`) treats `NAME=0` / `NAME=false` as *enabled*,
    which for SCHIST_NO_WAL silently flipped journal modes on deployments
    that meant to opt out (#311).
    """
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() not in _FALSY_VALUES
