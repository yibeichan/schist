"""schema/frontmatter-contract.json conformance — Python side (#130 slice A).

The frontmatter field lists used to live independently in create_note's
written metadata, update_note's PATCHABLE_FRONTMATTER_KEYS (both TS),
cli_add's written metadata, and ingest's read set here — prose-only in
schema/SCHEMA.md, with no machine check that they agree. The contract JSON is
the single source of truth; this suite pins ingest's read set and coercion
rules plus cli_add's written set to it, and
mcp-server/tests/frontmatter-contract.test.ts pins the two TS write sets. A
field added on either side without updating the contract fails that language's
CI.

Three layers:

1. **Structural** — scan ingest.py's source for the frontmatter keys it
   actually reads (``meta.get(...)`` / ``meta[...]`` / ``'x' in meta`` and the
   ``verification.get(...)`` nested reads) and require set equality with the
   contract's ``readBy: ingest`` fields, in BOTH directions. This is the drift
   detector: a new read without a contract update fails, and so does a
   contract field ingest no longer honors.

2. **Behavioral** — ingest a fixture vault and assert each field's
   ``invalid`` coercion policy against the resulting SQLite columns
   (e.g. off-enum ``confidence`` -> NULL, digit-string ``year`` -> int).

3. **CLI writer** — scan ``commands.add`` for every key assigned to its
   frontmatter dict and require set equality with ``writtenBy: cli_add``.
   Value normalization and validation stay behaviorally pinned in
   ``test_commands.py``.

Consumers must ignore descriptor keys they don't know — new keys may be
added to the contract without breaking either suite.
"""

from __future__ import annotations

import ast
import io
import json
import re
import sqlite3
import tokenize
from pathlib import Path

import pytest

from schist import commands as commands_module
from schist import ingest as ingest_module
from schist.ingest import PAPER_FIELDS, ingest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _contract_document() -> dict:
    fixture = _repo_root() / "schema" / "frontmatter-contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def _contract() -> list[dict]:
    return _contract_document()["fields"]


def _ingest_read_fields() -> set[str]:
    return {d["field"] for d in _contract() if "ingest" in d["readBy"]}


def _cli_add_written_fields() -> set[str]:
    """Return literal keys written to cli_add's ``fm`` frontmatter dict.

    Fail closed on mutation shapes this scanner does not understand. Otherwise
    a future ``fm.update(...)`` or dynamic subscript could add an on-disk field
    while the conformance test continued to report the old write set.
    """
    source = Path(commands_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    add_fn = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "add"
    )
    fields: set[str] = set()
    initializers = 0
    unsupported: list[str] = []
    for node in ast.walk(add_fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "fm":
                    if not isinstance(node.value, ast.Dict):
                        unsupported.append(f"line {node.lineno}: non-literal fm assignment")
                        continue
                    initializers += 1
                    if any(
                        not isinstance(key, ast.Constant) or not isinstance(key.value, str)
                        for key in node.value.keys
                    ):
                        unsupported.append(f"line {node.lineno}: non-literal fm initializer key")
                        continue
                    fields.update(key.value for key in node.value.keys)
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "fm"
                ):
                    if not (
                        isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        unsupported.append(f"line {node.lineno}: dynamic fm subscript")
                        continue
                    fields.add(target.slice.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "fm"
        ):
            unsupported.append(f"line {node.lineno}: fm.{node.func.attr}(...) mutation")
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            target = node.target
            if (
                isinstance(target, ast.Name) and target.id == "fm"
            ) or (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "fm"
            ):
                unsupported.append(f"line {node.lineno}: unsupported fm assignment")

    assert initializers == 1, (
        f"expected exactly one literal fm initializer in commands.add, got {initializers}"
    )
    assert not unsupported, (
        "commands.add mutates fm in forms the contract scanner cannot prove: "
        + "; ".join(unsupported)
    )
    return fields


def _docs_enum_fields() -> list[dict]:
    """Enum-typed docs fields whose invalid policy is coerce-null — the
    behavioral fixture below injects one invalid and one valid sample per
    field, so adding an enum field to the contract extends these tests."""
    return [
        d
        for d in _contract()
        if d["type"].startswith("enum:")
        and d["invalid"] == "coerce-null"
        and (d["indexColumn"] or "").startswith("docs.")
    ]


# ---------------------------------------------------------------------------
# Structural: contract fixture sanity + read-set equality via source scan
# ---------------------------------------------------------------------------


def test_contract_fixture_is_nontrivial() -> None:
    """An emptied/mangled fixture must fail loudly, not vacuously pass the
    set-equality checks below (same guard as the slug-parity fixture)."""
    assert _contract_document()["schemaVersion"] == 1
    contract = _contract()
    assert len(contract) >= 20
    names = [d["field"] for d in contract]
    assert len(names) == len(set(names)), "duplicate field names in contract"


def test_contract_descriptors_use_known_vocabulary() -> None:
    """Typos in enum-like descriptor values would silently drop a field from
    the filtered sets both suites assert against — fail them here instead."""
    applies_to = {"documents", "concepts", "papers"}
    written_by = {"create_note", "update_note", "cli_add"}
    read_by = {"ingest", "parseNote"}
    invalid_policies = {
        "coerce-null", "coerce-int-or-null", "stringify", "stringify-scalar",
        "drop-invalid-items", "fallback",
    }
    violations: list[str] = []
    for d in _contract():
        field = d["field"]
        violations += [f"{field}: unknown appliesTo '{v}'" for v in d["appliesTo"] if v not in applies_to]
        violations += [f"{field}: unknown writtenBy '{v}'" for v in d["writtenBy"] if v not in written_by]
        violations += [f"{field}: unknown readBy '{v}'" for v in d["readBy"] if v not in read_by]
        if d["invalid"] is not None and d["invalid"] not in invalid_policies:
            violations.append(f"{field}: unknown invalid-policy '{d['invalid']}'")
        if "ingest" in d["readBy"] and d["invalid"] is None:
            violations.append(f"{field}: read by ingest but has no invalid-coercion policy")
    assert not violations, "contract vocabulary violations: " + "; ".join(violations)


def test_cli_add_written_set_matches_contract() -> None:
    """The Python CLI writer is an enforced contract consumer, not prose-only."""
    scanned = _cli_add_written_fields()
    contract_fields = {
        d["field"] for d in _contract()
        if "cli_add" in d["writtenBy"]
    }
    missing = sorted(contract_fields - scanned)
    extra = sorted(scanned - contract_fields)
    assert not missing and not extra, (
        "cli_add write-set drift vs schema/frontmatter-contract.json — "
        f"in contract but never written by commands.add: {missing}; "
        f"written by commands.add but missing from the contract: {extra}. "
        "Update the contract before changing CLI frontmatter fields."
    )


# \w+ (not [a-z_]+): a digit-bearing field like s2_id must scan correctly —
# a partial match would report the drift in a MISLEADING direction.
_META_READ_RE = re.compile(r"\bmeta(?:\.get\(|\.pop\(|\.setdefault\(|\[)\s*['\"](\w+)['\"]")
_META_CONTAINS_RE = re.compile(r"['\"](\w+)['\"]\s+in\s+meta\b")
_VERIFICATION_READ_RE = re.compile(r"\bverification(?:\.get\(|\.pop\(|\.setdefault\(|\[)\s*['\"](\w+)['\"]")


def _ingest_source_without_comments() -> str:
    """ingest.py source with `#` comments blanked via tokenize, so a comment
    mentioning e.g. meta.get('old_field') never counts as a live read.
    Docstrings are NOT stripped — keep scannable field references out of them."""
    src = Path(ingest_module.__file__).read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            line = lines[row - 1]
            newline = "\n" if line.endswith("\n") else ""
            lines[row - 1] = line[:col].rstrip() + newline
    return "".join(lines)


def test_ingest_read_set_matches_contract() -> None:
    source = _ingest_source_without_comments()
    scanned = set(_META_READ_RE.findall(source)) | set(_META_CONTAINS_RE.findall(source))
    contract_fields = {f for f in _ingest_read_fields() if "." not in f}
    missing = sorted(contract_fields - scanned)
    extra = sorted(scanned - contract_fields)
    assert not missing and not extra, (
        "ingest read-set drift vs schema/frontmatter-contract.json — "
        f"in contract but never read by ingest.py: {missing}; "
        f"read by ingest.py but missing from the contract: {extra}. "
        "Update the contract AND the TS conformance suite when the field set changes."
    )


def test_verification_read_set_matches_contract() -> None:
    source = _ingest_source_without_comments()
    scanned = set(_VERIFICATION_READ_RE.findall(source))
    contract_fields = {
        f.split(".", 1)[1] for f in _ingest_read_fields() if f.startswith("verification.")
    }
    missing = sorted(contract_fields - scanned)
    extra = sorted(scanned - contract_fields)
    assert not missing and not extra, (
        "verification.* read-set drift vs schema/frontmatter-contract.json — "
        f"in contract but never read: {missing}; read but missing from the contract: {extra}"
    )


def test_paper_fields_constant_matches_contract() -> None:
    """PAPER_FIELDS drives the 'is this a paper?' trigger; it must equal the
    contract's top-level papers fields or the trigger and the contract skew."""
    contract_paper_fields = {
        d["field"] for d in _contract() if "papers" in d["appliesTo"] and "." not in d["field"]
    }
    missing = sorted(contract_paper_fields - PAPER_FIELDS)
    extra = sorted(PAPER_FIELDS - contract_paper_fields)
    assert not missing and not extra, (
        "PAPER_FIELDS drift vs schema/frontmatter-contract.json — "
        f"in contract but not in PAPER_FIELDS: {missing}; "
        f"in PAPER_FIELDS but not in the contract: {extra}"
    )


def test_invalid_policy_map_is_pinned() -> None:
    """Swapping a field's `invalid` between two LEGAL vocabulary values (e.g.
    confidence coerce-null -> stringify) passes the vocabulary check while
    silently reshuffling which behavioral tests run (the enum parametrization
    below is contract-driven). Pin the full policy map so a policy change must
    consciously touch this test together with the behavioral sample proving
    the new policy."""
    declared = {d["field"]: d["invalid"] for d in _contract() if "ingest" in d["readBy"]}
    assert declared == {
        "title": "fallback",
        "date": "stringify",
        "status": "coerce-null",
        "tags": "drop-invalid-items",
        "concepts": "drop-invalid-items",
        "confidence": "coerce-null",
        "file_ref": "coerce-null",
        "scope": "fallback",
        "source": "coerce-null",
        "topic": "fallback",
        "concept": "fallback",
        "authors": "drop-invalid-items",
        "year": "coerce-int-or-null",
        "venue": "stringify-scalar",
        "type": "stringify-scalar",
        "doi": "stringify-scalar",
        "arxiv_id": "stringify-scalar",
        "pubmed_pmid": "stringify-scalar",
        "bibtex_key": "stringify-scalar",
        "url": "stringify-scalar",
        "verification": "coerce-null",
        "verification.verified_on": "stringify-scalar",
        "verification.verified_by": "stringify-scalar",
        "verification.verified_against": "drop-invalid-items",
    }


def test_enum_parametrization_is_not_vacuous() -> None:
    """_docs_enum_fields drives parametrized behavioral tests below; if a type
    or policy edit filtered everything out, those tests would vanish silently
    (fewer collected tests, no failure)."""
    assert len(_docs_enum_fields()) >= 2


# ---------------------------------------------------------------------------
# Behavioral: coercion policies against a real ingest run
# ---------------------------------------------------------------------------

INVALID_NOTE_ID = "notes/2026-07-06-invalid-coercions.md"
VALID_NOTE_ID = "notes/2026-07-06-valid-fields.md"
TOPIC_NOTE_ID = "notes/2026-07-06-topic-fallback.md"
FILENAME_NOTE_ID = "notes/2026-07-06-filename-fallback.md"
NONSTRING_TITLE_NOTE_ID = "notes/2026-07-06-nonstring-title.md"
NONSTRING_TITLE_TOPIC_NOTE_ID = "notes/2026-07-06-nonstring-title-topic.md"
ROOT_NOTE_ID = "2026-07-06-root-scope.md"
GOOD_PAPER_ID = "papers/2026-07-06-good-paper.md"
BAD_PAPER_ID = "papers/2026-07-06-bad-paper.md"
FIELD_TRIGGERED_PAPER_ID = "notes/2026-07-06-field-triggered-paper.md"


def _enum_samples() -> tuple[list[str], list[str]]:
    """Frontmatter lines for enum fields, derived FROM the contract: one
    off-enum value (must coerce to NULL) and the first declared value (must
    round-trip). New enum fields in the contract flow into the fixture."""
    invalid_lines = [f"{d['field']}: off-enum-value" for d in _docs_enum_fields()]
    valid_lines = [
        f"{d['field']}: {d['type'].removeprefix('enum:').split('|')[0]}"
        for d in _docs_enum_fields()
    ]
    return invalid_lines, valid_lines


def _vault_files() -> dict[str, str]:
    invalid_enum_lines, valid_enum_lines = _enum_samples()
    return {
        INVALID_NOTE_ID: (
            "---\n"
            "title: Invalid Coercions\n"
            "date: 2026-07-06\n"
            "status: 42\n"
            "tags: [ok-tag, \"#hash-tag\", 7]\n"
            "concepts: [\"Sparse  Coding\", 9, \"   \"]\n"
            "file_ref: [not, a, string]\n"
            "scope: \"\"\n"
            + "\n".join(invalid_enum_lines) + "\n"
            "---\n\nBody.\n"
        ),
        VALID_NOTE_ID: (
            "---\n"
            "title: Valid Fields\n"
            "date: 2026-07-06\n"
            "status: draft\n"
            "tags: [alpha]\n"
            "concepts: [beta-concept]\n"
            "file_ref: /data/ref.pdf\n"
            "scope: custom-scope\n"
            + "\n".join(valid_enum_lines) + "\n"
            "---\n\nBody.\n"
        ),
        TOPIC_NOTE_ID: "---\ntopic: Topic Fallback Title\n---\n\nBody.\n",
        FILENAME_NOTE_ID: "---\nstatus: draft\n---\n\nBody.\n",
        # Truthy NON-STRING title candidates: a list/dict title used to abort
        # the entire rebuild with a sqlite3 binding error, an int landed with
        # native affinity — both must fall through the fallback chain instead.
        NONSTRING_TITLE_NOTE_ID: (
            "---\ntitle: [not, a, string]\ntopic: Topic Wins\n---\n\nBody.\n"
        ),
        NONSTRING_TITLE_TOPIC_NOTE_ID: (
            "---\ntitle: {k: v}\ntopic: 42\n---\n\nBody.\n"
        ),
        ROOT_NOTE_ID: "---\ntitle: Root Scope\n---\n\nBody.\n",
        "concepts/messy-concept.md": (
            "---\nconcept: \"Messy   Concept\"\n---\n\nDescription paragraph.\n"
        ),
        "concepts/stem-fallback.md": (
            "---\nconcept: [not-a-string]\n---\n\nDescription paragraph.\n"
        ),
        GOOD_PAPER_ID: (
            "---\n"
            "title: Good Paper\n"
            "authors: [\"Doe, Jane\", 42]\n"
            "year: \"2020\"\n"
            "venue: 2024\n"
            "type: preprint\n"
            "doi: 10.1234/abc\n"
            "arxiv_id: \"2004.00001\"\n"
            "pubmed_pmid: 12345\n"
            "bibtex_key: doe2020good\n"
            "url: https://example.org/paper\n"
            "verification:\n"
            "  verified_on: 2026-07-01\n"
            "  verified_by: claude-code\n"
            "  verified_against:\n"
            "    - \"crossref:10.1234/abc\"\n"
            "    - pubmed: 999\n"
            "---\n\nBody.\n"
        ),
        BAD_PAPER_ID: (
            "---\n"
            "title: Bad Paper\n"
            "year: nineteen-ninety\n"
            "verification: not-an-object\n"
            "---\n\nBody.\n"
        ),
        FIELD_TRIGGERED_PAPER_ID: (
            "---\ntitle: Field Triggered Paper\ndoi: 10.9/xyz\nyear: true\n---\n\nBody.\n"
        ),
    }


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> sqlite3.Connection:
    root = tmp_path_factory.mktemp("frontmatter-contract")
    vault = root / "vault"
    for rel, text in _vault_files().items():
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    db_path = root / "schist.db"
    ingest(str(vault), str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _docs_value(conn: sqlite3.Connection, doc_id: str, column: str):
    row = conn.execute(f"SELECT {column} AS v FROM docs WHERE id = ?", (doc_id,)).fetchone()
    assert row is not None, f"doc {doc_id} missing from index"
    return row["v"]


def _paper_value(conn: sqlite3.Connection, doc_id: str, column: str):
    row = conn.execute(
        f"SELECT {column} AS v FROM paper_metadata WHERE doc_id = ?", (doc_id,)
    ).fetchone()
    assert row is not None, f"paper_metadata row for {doc_id} missing from index"
    return row["v"]


@pytest.mark.parametrize("descriptor", _docs_enum_fields(), ids=lambda d: d["field"])
def test_off_enum_value_coerces_to_null(indexed: sqlite3.Connection, descriptor: dict) -> None:
    column = descriptor["indexColumn"].split(".", 1)[1]
    value = _docs_value(indexed, INVALID_NOTE_ID, column)
    assert value is None, (
        f"contract says invalid '{descriptor['field']}' is {descriptor['invalid']}, "
        f"but ingest stored {value!r}"
    )


@pytest.mark.parametrize("descriptor", _docs_enum_fields(), ids=lambda d: d["field"])
def test_first_enum_value_round_trips(indexed: sqlite3.Connection, descriptor: dict) -> None:
    column = descriptor["indexColumn"].split(".", 1)[1]
    expected = descriptor["type"].removeprefix("enum:").split("|")[0]
    value = _docs_value(indexed, VALID_NOTE_ID, column)
    assert value == expected, (
        f"valid '{descriptor['field']}: {expected}' must be stored verbatim, got {value!r}"
    )


def test_non_string_status_coerces_to_null(indexed: sqlite3.Connection) -> None:
    assert _docs_value(indexed, INVALID_NOTE_ID, "status") is None
    assert _docs_value(indexed, VALID_NOTE_ID, "status") == "draft"


def test_non_string_file_ref_coerces_to_null(indexed: sqlite3.Connection) -> None:
    assert _docs_value(indexed, INVALID_NOTE_ID, "file_ref") is None
    assert _docs_value(indexed, VALID_NOTE_ID, "file_ref") == "/data/ref.pdf"


def test_tags_drop_invalid_items_and_strip_hash(indexed: sqlite3.Connection) -> None:
    tags = json.loads(_docs_value(indexed, INVALID_NOTE_ID, "tags"))
    assert tags == ["ok-tag", "hash-tag"], (
        "tags must drop non-string items and strip the '#' prefix"
    )


def test_concepts_drop_invalid_items_and_normalize_slugs(indexed: sqlite3.Connection) -> None:
    concepts = json.loads(_docs_value(indexed, INVALID_NOTE_ID, "concepts"))
    assert concepts == ["sparse-coding"], (
        "concepts must drop non-string/blank items and slug-normalize the rest"
    )


def test_date_is_stringified(indexed: sqlite3.Connection) -> None:
    # Unquoted YAML dates parse as datetime.date; ingest must store the
    # ISO date STRING so date-equality queries keep working.
    assert _docs_value(indexed, VALID_NOTE_ID, "date") == "2026-07-06"


def test_title_fallback_chain(indexed: sqlite3.Connection) -> None:
    assert _docs_value(indexed, TOPIC_NOTE_ID, "title") == "Topic Fallback Title"
    assert _docs_value(indexed, FILENAME_NOTE_ID, "title") == "filename fallback"
    assert _docs_value(indexed, "concepts/messy-concept.md", "title") == "Messy   Concept"


def test_nonstring_title_candidates_fall_through_without_aborting(
    indexed: sqlite3.Connection,
) -> None:
    """A list/dict `title:` used to abort the ENTIRE rebuild with a sqlite3
    binding error (one bad note = permanent read outage, #296 family); the
    fixture ingesting at all proves the crash is gone, and non-string
    candidates must fall through the chain rather than store with native
    affinity."""
    assert _docs_value(indexed, NONSTRING_TITLE_NOTE_ID, "title") == "Topic Wins"
    assert (
        _docs_value(indexed, NONSTRING_TITLE_TOPIC_NOTE_ID, "title")
        == "nonstring title topic"
    )


def test_scope_fallback_chain(indexed: sqlite3.Connection) -> None:
    assert _docs_value(indexed, VALID_NOTE_ID, "scope") == "custom-scope"
    # Empty frontmatter scope falls back to the parent directory ...
    assert _docs_value(indexed, INVALID_NOTE_ID, "scope") == "notes"
    # ... and a root-level note falls back to 'global'.
    assert _docs_value(indexed, ROOT_NOTE_ID, "scope") == "global"


def test_concept_field_marks_and_slugs_concept_nodes(indexed: sqlite3.Connection) -> None:
    slugs = {row["slug"] for row in indexed.execute("SELECT slug FROM concepts")}
    assert "messy-concept" in slugs, "concept value must be slug-normalized"
    assert "stem-fallback" in slugs, "non-string concept must fall back to the filename stem"


def test_paper_field_coercions(indexed: sqlite3.Connection) -> None:
    assert json.loads(_paper_value(indexed, GOOD_PAPER_ID, "authors")) == ["Doe, Jane"]
    assert _paper_value(indexed, GOOD_PAPER_ID, "year") == 2020, "digit-string year must coerce to int"
    assert _paper_value(indexed, GOOD_PAPER_ID, "venue") == "2024", "scalar venue must be stringified"
    assert _paper_value(indexed, GOOD_PAPER_ID, "paper_type") == "preprint"
    assert _paper_value(indexed, GOOD_PAPER_ID, "doi") == "10.1234/abc"
    assert _paper_value(indexed, GOOD_PAPER_ID, "arxiv_id") == "2004.00001"
    assert _paper_value(indexed, GOOD_PAPER_ID, "pubmed_pmid") == "12345"
    assert _paper_value(indexed, GOOD_PAPER_ID, "bibtex_key") == "doe2020good"
    assert _paper_value(indexed, GOOD_PAPER_ID, "url") == "https://example.org/paper"


def test_verification_object_maps_to_verified_columns(indexed: sqlite3.Connection) -> None:
    assert _paper_value(indexed, GOOD_PAPER_ID, "verified") == 1
    assert _paper_value(indexed, GOOD_PAPER_ID, "verified_by") == "claude-code"
    assert _paper_value(indexed, GOOD_PAPER_ID, "verified_date") == "2026-07-01"
    sources = json.loads(_paper_value(indexed, GOOD_PAPER_ID, "verification_sources"))
    assert sources == ["crossref:10.1234/abc", "pubmed:999"], (
        "verified_against must keep string items and flatten mapping items to 'key:value'"
    )


def test_invalid_paper_values_coerce_to_null(indexed: sqlite3.Connection) -> None:
    assert _paper_value(indexed, BAD_PAPER_ID, "year") is None, "non-numeric year must be NULL"
    # bool is an int subclass in Python: `year: true` must not store 1.
    assert _paper_value(indexed, FIELD_TRIGGERED_PAPER_ID, "year") is None, (
        "boolean year must coerce to NULL"
    )
    assert _paper_value(indexed, BAD_PAPER_ID, "verified") == 0, (
        "non-object verification must read as unverified"
    )
    assert _paper_value(indexed, BAD_PAPER_ID, "verified_date") is None
    assert _paper_value(indexed, BAD_PAPER_ID, "verification_sources") is None


def test_any_paper_field_triggers_paper_metadata_row(indexed: sqlite3.Connection) -> None:
    # A doc OUTSIDE papers/ still gets a paper_metadata row when any
    # contract paper field is present (PAPER_FIELDS trigger).
    assert _paper_value(indexed, FIELD_TRIGGERED_PAPER_ID, "doi") == "10.9/xyz"
    # And a plain note with no paper fields must NOT get one.
    row = indexed.execute(
        "SELECT 1 FROM paper_metadata WHERE doc_id = ?", (TOPIC_NOTE_ID,)
    ).fetchone()
    assert row is None


def test_index_columns_exist_in_materialized_schema(indexed: sqlite3.Connection) -> None:
    """Every non-null indexColumn must name a real column in the schema
    ingest materializes (schema.sql, via the fixture's real ingest run). A
    typo — or a slice-B column rename that skips the contract — would
    otherwise silently drop the field from behavioral enforcement while
    documenting a nonexistent column."""
    problems: list[str] = []
    for d in _contract():
        ref = d["indexColumn"]
        if ref is None:
            continue
        table, _, column = ref.partition(".")
        columns = {row[1] for row in indexed.execute(f"PRAGMA table_info({table})")}
        if not columns:
            problems.append(f"{d['field']}: indexColumn table '{table}' does not exist")
        elif column not in columns:
            problems.append(
                f"{d['field']}: indexColumn '{ref}' not found (has: {sorted(columns)})"
            )
    assert not problems, "indexColumn drift vs cli/schist/schema.sql: " + "; ".join(problems)
