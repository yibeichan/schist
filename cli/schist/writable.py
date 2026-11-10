# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Spoke-side write permission checks against the hub's vault.yaml ACL.

One function, shared by the proposal queue (which files a note or a link) and
`schist study grade` (which rewrites one card's review fields). Both write
through git and then let the spoke's normal sync push: a commit the hub would
refuse blocks every later push from this spoke, so the check is made before
the write instead of discovering it at push time.

Fails open when there is no spoke identity (a standalone vault, or the hub
itself) or when vault.yaml cannot be read; the hub stays the authority either
way, as `pre_receive` enforces the same rule on arrival.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .acl import ACLError, parse_vault_yaml
from .pre_receive import derive_scope
from .spoke_config import is_spoke, load_spoke_config


class WritableError(Exception):
    """The spoke's identity may not write the target path under vault.yaml."""


def check_writable(vault: str | Path, relpath: str) -> None:
    """Raise WritableError for a path the hub would refuse from this spoke.

    The identity is spoke.yaml's, the one `schist sync push` pushes as (and
    `schist doctor` checks).
    """
    vault = Path(vault)
    acl_path = vault / 'vault.yaml'
    if not is_spoke(str(vault)) or not acl_path.is_file():
        return
    try:
        identity = load_spoke_config(str(vault)).identity
        acl = parse_vault_yaml(acl_path)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, ACLError):
        return
    # `schist edit_scope`/`pre_receive.derive_scope` rule: a root-level file
    # needs a wildcard write grant, everything else is matched by its parent
    # directory.
    scope = derive_scope(relpath)
    if scope == '':
        entry = acl.access.get(identity)
        ok = entry is not None and '*' in entry.write
    else:
        ok = acl.can_write(identity, scope)
    if not ok:
        raise WritableError(
            f'spoke {identity!r} may not write {scope or "the vault root"!r} '
            'under vault.yaml, so the hub would refuse this commit'
        )
