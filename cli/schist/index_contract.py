"""Vault-index contract — single-sourced from ``schema/index-contract.json``.

The vault index (``.schist/schist.db``) is disposable and rebuilt from
markdown by ingest. Its cross-language contract — which tables schema.sql
creates, which tables/columns readers require before trusting a DB, which
tables survive rebuilds, the schema version ingest stamps into
``PRAGMA user_version``, and a digest of the materialized DDL — is defined
once in ``<repo>/schema/index-contract.json`` and consumed by this module
and by ``mcp-server/src/sqlite-reader.ts``. Duplicated per-language
constants drift (#339: the TS mirror dropped ``docs`` from its required
set); see #130 D3.

Versioning model: ``user_version = 0`` while an ingest is in flight (and on
pre-marker DBs, #244), ``user_version = INDEX_SCHEMA_VERSION`` stamped
atomically with the data commit. ``INDEX_SCHEMA_VERSION`` is bumped only on
DDL changes to ``schema.sql``; a reader finding a non-zero ``user_version``
different from ``INDEX_SCHEMA_VERSION`` treats the DB as stale and forces a
rebuild — the index is disposable, so rebuild IS the migration path (no
ALTER migrations). Note the exemption is exactly ``user_version == 0``:
anything else that mismatches (including a corrupt negative value) is
stale.

``schemaSqlDigest`` pins the materialized schema itself: a version bump is
a human judgment, but the digest changes on ANY DDL edit, so forgetting the
bump (or a required-columns update) forces a visible contract diff and a
failing parity test in both suites. Recompute recipe: execute schema.sql
into ``:memory:``, take ``sqlite_master`` rows whose name starts with
neither ``sqlite_`` nor ``docs_fts_``, sort the strings
``f"{type}\\x1f{name}\\x1f{sql or ''}"``, join with ``\\x1e``, SHA-256 the
UTF-8 bytes. The parity test in each suite recomputes it the same way.

``rebuildSurvivors`` is declarative: it is enforced against schema.sql's
DROP/CREATE structure and against sync.py's ``_SIDE_TABLE_COLUMNS`` by
tests, not consumed at runtime. Changing a SURVIVOR table's DDL needs an
explicit copy-forward migration — a version bump alone rebuilds every
dropped table but silently keeps the survivor's old shape (its CREATE is
``IF NOT EXISTS``) while stamping the new version.

Packaging: repo-root ``schema/`` files are not package data — an installed
``schist`` wheel does not carry them — so this module bakes in a mirror of
the contract and prefers the canonical file only when the monorepo checkout
is present (the ``default.yaml`` pattern from ``mcp-server/src/tools.ts``).
The mirror-vs-``schema/`` drift test in ``cli/tests/test_index_contract.py``
is what keeps the mirror honest. Hand-provisioned bare-script deployments
(a ``.schist/ingest.py`` hook copy) must carry this module as a sibling
file, exactly like the sibling schema.sql — ``schist doctor``'s Ingest
check warns on copies that predate it.
"""

import json
import re
import sys
from pathlib import Path

# Baked-in mirror of <repo>/schema/index-contract.json. Do not edit one
# without the other — the drift test pins them equal (as parsed JSON).
INDEX_CONTRACT_FALLBACK: dict = {
    'schemaVersion': 1,
    'tables': [
        'docs', 'concepts', 'edges', 'docs_fts', 'paper_metadata', 'concept_aliases',
    ],
    'requiredTables': ['docs', 'paper_metadata', 'concept_aliases'],
    'requiredDocsColumns': [
        'id', 'title', 'date', 'status', 'tags', 'concepts',
        'body', 'scope', 'source', 'confidence', 'file_ref',
    ],
    'rebuildSurvivors': ['concept_aliases'],
    'schemaSqlDigest': '6cd775da8d6592bdf38b4fa246f6d49400ee7d70b23934843c0232148f56e212',
}

_CONTRACT_KEYS = ('tables', 'requiredTables', 'requiredDocsColumns', 'rebuildSurvivors')
_HEX_DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
# `PRAGMA user_version` is a signed 32-bit header field; a larger stamp
# would truncate on write, readers would never see their expected version,
# and every access would trigger a full rebuild — forever.
_MAX_SCHEMA_VERSION = 2**31 - 1


def _is_str_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _validate(raw) -> dict | None:
    """Return the contract with schemaVersion coerced to int, or None."""
    if not isinstance(raw, dict):
        return None
    version = raw.get('schemaVersion')
    if isinstance(version, bool):
        return None
    if isinstance(version, float) and version.is_integer():
        # JSON has one number type: JS parses `2.0` as the integer 2 and
        # cannot even observe the difference, so a hand-edited `2.0` must
        # not desync the two loaders. Coerce, don't reject.
        version = int(version)
    if not isinstance(version, int) or not 0 < version <= _MAX_SCHEMA_VERSION:
        return None
    if not all(_is_str_list(raw.get(key)) for key in _CONTRACT_KEYS):
        return None
    digest = raw.get('schemaSqlDigest')
    if not isinstance(digest, str) or not _HEX_DIGEST_RE.match(digest):
        return None
    validated = dict(raw)
    validated['schemaVersion'] = version
    return validated


def load_index_contract(path: Path | None = None) -> dict:
    """Return the canonical contract when readable and well-formed, else the mirror.

    A missing/unreadable file is the NORMAL installed-wheel state and stays
    silent; a file that is present but not valid JSON — or parses but fails
    validation — is a broken checkout and warns to stderr (mirroring the
    ``console.warn`` in the TS ``loadIndexContract``).
    """
    if path is None:
        path = Path(__file__).resolve().parents[2] / 'schema' / 'index-contract.json'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        # Installed wheel — no repo checkout; the baked-in mirror IS the contract.
        return INDEX_CONTRACT_FALLBACK

    try:
        raw = json.loads(text)
    except ValueError:
        raw = None
    validated = _validate(raw)
    if validated is None:
        print(
            f'WARN: {path} is malformed; using the baked-in index-contract mirror',
            file=sys.stderr,
        )
        return INDEX_CONTRACT_FALLBACK
    return validated


_CONTRACT = load_index_contract()

# Stamped into `PRAGMA user_version` by ingest on completion; checked by
# get_db. The load-time validation above guarantees this is a positive
# 32-bit int, which ingest.py relies on when interpolating it into the
# PRAGMA statement.
INDEX_SCHEMA_VERSION: int = _CONTRACT['schemaVersion']

# Every read/queryable table schema.sql creates (docs_fts shadow tables are
# implementation details and are special-cased where needed).
TABLES: frozenset[str] = frozenset(_CONTRACT['tables'])

# Tables a DB must have before readers trust it; anything less re-triggers
# ingest. concept_aliases is included so vaults upgraded from a
# pre-concept_aliases schema (which still have docs + paper_metadata)
# re-trigger ingest instead of failing later with "no such table" (#224).
REQUIRED_TABLES: frozenset[str] = frozenset(_CONTRACT['requiredTables'])
