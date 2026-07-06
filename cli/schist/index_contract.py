"""Vault-index contract — single-sourced from ``schema/index-contract.json``.

The vault index (``.schist/schist.db``) is disposable and rebuilt from
markdown by ingest. Its cross-language contract — which tables schema.sql
creates, which tables/columns readers require before trusting a DB, which
tables survive rebuilds, and the schema version ingest stamps into
``PRAGMA user_version`` on completion — is defined once in
``<repo>/schema/index-contract.json`` and consumed by this module and by
``mcp-server/src/sqlite-reader.ts``. Duplicated per-language constants drift
(#339: the TS mirror dropped ``docs`` from its required set); see #130 D3.

Versioning model: ``user_version = 0`` while an ingest is in flight (and on
pre-marker DBs, #244), ``user_version = INDEX_SCHEMA_VERSION`` stamped
atomically with the data commit. ``INDEX_SCHEMA_VERSION`` is bumped only on
DDL changes to ``schema.sql``; a reader finding ``0 < user_version !=
INDEX_SCHEMA_VERSION`` treats the DB as stale and forces a rebuild — the
index is disposable, so rebuild IS the migration path (no ALTER migrations).

Packaging: repo-root ``schema/`` files are not package data — an installed
``schist`` wheel does not carry them — so this module bakes in a mirror of
the contract and prefers the canonical file only when the monorepo checkout
is present (the ``default.yaml`` pattern from ``mcp-server/src/tools.ts``).
The mirror-vs-``schema/`` drift test in ``cli/tests/test_index_contract.py``
is what keeps the mirror honest.
"""

import json
import sys
from pathlib import Path

# Baked-in mirror of <repo>/schema/index-contract.json. Do not edit one
# without the other — the drift test pins them byte-equal (as parsed JSON).
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
}

_CONTRACT_KEYS = ('tables', 'requiredTables', 'requiredDocsColumns', 'rebuildSurvivors')


def _is_str_list(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def load_index_contract() -> dict:
    """Return the canonical contract when readable and well-formed, else the mirror.

    Missing file is the normal installed-wheel state and stays silent; a file
    that is present but malformed is a broken checkout and warns to stderr
    (mirroring the ``console.warn`` in the TS ``loadIndexContract``).
    """
    path = Path(__file__).resolve().parents[2] / 'schema' / 'index-contract.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        # Installed wheel — no repo checkout; the baked-in mirror IS the contract.
        return INDEX_CONTRACT_FALLBACK

    version = raw.get('schemaVersion') if isinstance(raw, dict) else None
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version > 0
        and all(_is_str_list(raw.get(key)) for key in _CONTRACT_KEYS)
    ):
        return raw

    print(
        f'WARN: {path} is malformed; using the baked-in index-contract mirror',
        file=sys.stderr,
    )
    return INDEX_CONTRACT_FALLBACK


_CONTRACT = load_index_contract()

# Stamped into `PRAGMA user_version` by ingest on completion; checked by
# get_db. The load-time validation above guarantees this is a positive int,
# which ingest.py relies on when interpolating it into the PRAGMA statement.
INDEX_SCHEMA_VERSION: int = _CONTRACT['schemaVersion']

# Every read/queryable table schema.sql creates (docs_fts shadow tables are
# implementation details and are special-cased where needed).
TABLES: frozenset[str] = frozenset(_CONTRACT['tables'])

# Tables a DB must have before readers trust it; anything less re-triggers
# ingest. concept_aliases is included so vaults upgraded from a
# pre-concept_aliases schema (which still have docs + paper_metadata)
# re-trigger ingest instead of failing later with "no such table" (#224).
REQUIRED_TABLES: frozenset[str] = frozenset(_CONTRACT['requiredTables'])
