#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""schist ingestion — parse markdown vault into SQLite."""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import frontmatter
import yaml

try:
    from .env_utils import env_flag
except ImportError:  # pragma: no cover — bare-script mode, same as below.
    try:
        from env_utils import env_flag
    except ImportError:
        # Bare-script copy with no env_utils.py sibling (#369): every
        # pre-#354 deployment is in this state after refreshing just
        # ingest.py, and a hard failure here breaks ingest on EVERY commit
        # with only a buried hook traceback. Unlike index_contract (where a
        # missing sibling must fail loudly — a guessed version re-mints the
        # #339 drift), env_flag has one correct behavior, so inline it.
        # cli/tests/test_ingest.py pins this byte-for-byte against
        # env_utils.env_flag semantics.
        def env_flag(name: str) -> bool:
            value = os.environ.get(name)
            if value is None:
                return False
            return value.strip().lower() not in {"", "0", "false", "no", "off"}

try:
    from .index_contract import INDEX_SCHEMA_VERSION
except ImportError:  # pragma: no cover — running as a bare script (a legacy
    # `.schist/ingest.py` hook copy, see sync.py's POST_COMMIT_HOOK). The
    # script's own directory is on sys.path, so a sibling index_contract.py
    # copy resolves — exactly like the sibling schema.sql this script already
    # requires. No literal fallback here: a bare-script copy old enough to
    # lack the sibling module would stamp a version its schema.sql doesn't
    # match, and failing loudly beats silently re-minting the #339 drift.
    from index_contract import INDEX_SCHEMA_VERSION

SKIP_DIRS = {'.git', '.schist'}
ROOT_SUPPORT_DOCS = {'readme.md', 'schema.md', 'tags.md', 'conventions.md'}
HASHTAG_AT_START_RE = re.compile(r'^#[^\s,\]\}]+')
# CONNECTION_RE is defined below _SLUG_WS_CHARS — it is built from the same
# explicit whitespace class (#338).

# Frontmatter fields whose presence marks a note as a paper (copied into
# paper_metadata). Pinned to schema/frontmatter-contract.json's
# `appliesTo: papers` set by cli/tests/test_frontmatter_contract.py —
# update the contract when this set changes.
PAPER_FIELDS = {
    'authors',
    'year',
    'venue',
    'type',
    'doi',
    'arxiv_id',
    'pubmed_pmid',
    'bibtex_key',
    'url',
    'verification',
}


# Explicit whitespace set shared verbatim with mcp-server's
# normalizeConceptSlug (tools.ts). Python's \s and JS's \s disagree at the
# edges — Python adds U+001C–U+001F (C0 separators) and U+0085 (NEL), JS adds
# U+FEFF (ZWNBSP) — exactly the cross-language drift family that caused #303.
# This is the UNION of both engines' sets (30 codepoints), so either
# language's notion of whitespace becomes a slug separator.
# schema/concept-slug-parity.json pins both implementations to the same
# table. #318.
_SLUG_WS_CHARS = (
    '\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680'
    '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007'
    '\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
)
# Edge-stripping uses str.strip(chars) — LINEAR — not a `^[ws]+|[ws]+$`
# regex, which backtracks quadratically over interior whitespace runs
# (20s on a 100k-space concept string; reachable from agent-supplied
# frontmatter, and ingest runs after every write).
_SLUG_WS_RUN = re.compile(f'[{re.escape(_SLUG_WS_CHARS)}]+')

# Connection lines use the same explicit whitespace union: native \s / \S
# membership differs between Python and JS (see _SLUG_WS_CHARS above), so the
# two languages' CONNECTION_REs parsed divergent codepoints differently —
# e.g. a NEL (U+0085) separator produced an edge under Python's \s but not
# under JS's (#338). `\s` becomes the explicit class, `\S` its negation;
# The context group is [^\n\r]*, not `.*`, because JS's `.` stops at
# U+2028/U+2029, which can sit inside a line. Otherwise the semantics are
# identical. schema/connection-line-parity.json pins
# this regex against mcp-server/src/markdown-parser.ts's CONNECTION_RE.
_WS_CLASS = f'[{re.escape(_SLUG_WS_CHARS)}]'
_NON_WS_CLASS = f'[^{re.escape(_SLUG_WS_CHARS)}]'
CONNECTION_RE = re.compile(
    f'^-{_WS_CLASS}+({_NON_WS_CLASS}+):{_WS_CLASS}+({_NON_WS_CLASS}+)'
    f'(?:{_WS_CLASS}+"([^"]*)")?(?:{_WS_CLASS}+—{_WS_CLASS}+([^\\n\\r]*))?$'
)

# The connections rule, shared by every reader and writer of a note's
# `## Connections` section (parse_connections here; markdown_io's insert and
# append; the concept checks and `schist schema --validate` in commands.py).
# It lives in ingest.py, not markdown_io, because the bare-script hook copy of
# this file cannot import markdown_io. mcp-server/src/markdown-parser.ts
# mirrors it (scanConnections and friends); schema/connection-line-parity.json
# pins the two, and mcp-server/tests/connection-marked.test.ts checks every
# edge either reader finds against marked 12.0.2, the renderer the viewer
# uses.
#
# The rule is conservative by construction. It indexes an edge only where
# the viewer shows it as a top-level list item under the `## Connections`
# heading, and when a note holds Markdown whose rendering the rule does not
# follow, it indexes nothing from that note and reports why
# (ConnectionScan.problem; `schist schema --validate` and the MCP server's
# get_note show it, and the writers refuse to edit the section).
#
# The rule reads the note body as connection_body returns it: CRLF and CR
# become LF, the text after the frontmatter's closing line (note_body_offset),
# without leading and trailing blank lines. Ingest stores exactly that text
# in docs.body, and the viewer renders it unchanged. Lines are split on LF.
# - The heading is the first line that is exactly `## Connections` at column
#   0, optionally followed by spaces or tabs, outside a fence.
# - The section ends at the next line of 0-3 spaces, 1-6 `#`, then whitespace
#   (_SLUG_WS_CHARS) or the end of the line: any ATX heading, of any level,
#   empty or not. Otherwise it runs to the end of the body.
# - A fence opens on a line of spaces, then 3+ backticks or 3+ tildes, then
#   an optional info word of ASCII letters, digits and `_+.#-`, then optional
#   spaces. It closes on the first later line that is the same spaces and the
#   same run, then optional spaces; every line between must be blank or start
#   with the same spaces, and none may start, after spaces and tabs, with
#   ``` or ~~~. Lines from opener to closer are never a heading, an edge or a
#   section end. In the section, an opener indented 4+ spaces is a problem.
# - Problems, from the body start to the end of the section: a line holding
#   U+000B, U+000C, U+001C-U+001F, U+0085, U+2028 or U+2029; a line that
#   starts, after spaces and tabs, with ``` or ~~~ and is not part of a
#   fence as above; a line that starts, after spaces and tabs, with `<`; a
#   line that starts, after spaces and tabs, with `[` unless its first
#   unescaped `]` comes before any other `[` and is not followed by `:` (so
#   it cannot open a link reference definition, whose label and title can
#   run over later lines).
# - Problems inside the section, outside fences: a line that starts, after
#   spaces and tabs, with `>`; a non-blank line indented by a tab or 4+
#   spaces; a non-blank line made only of `-`, `=`, `*`, `_`, `+`, `|`, `:`,
#   spaces and tabs, ignoring _SLUG_WS_CHARS at either end (setext
#   underlines, thematic breaks, table delimiter rows); a list marker
#   followed by a tab or 4+ spaces.
# - An edge line, in the section and outside fences: `-` at column 0, 1-3
#   spaces, then text that, trimmed with the explicit _SLUG_WS_CHARS set,
#   matches CONNECTION_RE with a type that starts with an ASCII letter or
#   digit and a target that does not start with `[`.
#
# Every step is one pass over the lines, so reading a note is linear in its
# size.
#
# CONNECTIONS_RULE_VERSION is bumped whenever this rule changes; `schist
# doctor` compares it against a hand-provisioned .schist/ingest.py hook copy.
CONNECTIONS_RULE_VERSION = 2
CONNECTIONS_HEADING_TEXT = '## Connections'
_CONNECTIONS_HEADING_RE = re.compile(r'## Connections[ \t]*')
_SECTION_END_RE = re.compile(f' {{0,3}}#{{1,6}}(?:{_WS_CLASS}|$)')
_FENCE_OPEN_RE = re.compile(r'( *)(`{3,}|~{3,})[A-Za-z0-9_+.#-]* *')
_FENCE_LIKE_RE = re.compile(r'[ \t]*(?:```|~~~)')
_LIST_MARKER_WS_RE = re.compile(r'[ \t]*(?:[-+*]|[0-9]{1,9}[.)])([ \t]+)')
_EDGE_PREFIX_RE = re.compile(r'- {1,3}[^ \t]')
# Characters a renderer reads as a line break or that Python's
# str.splitlines() splits on, but a Markdown line does not end at.
_NON_MARKDOWN_SEPARATORS = '\x0b\x0c\x1c\x1d\x1e\x1f\x85\u2028\u2029'
_NON_MARKDOWN_SEPARATOR_RE = re.compile(f'[{_NON_MARKDOWN_SEPARATORS}]')
_RULE_LINE_CHARS = frozenset('-=*_+|: \t')
_ASCII_ALNUM = frozenset(
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
)


class ConnectionScan:
    """What the connection rule finds in a list of lines.

    heading: index of the `## Connections` heading, or None. When a problem
        comes before it, the first later line of that shape.
    end: index of the line that ends the section, or len(lines).
    edges: (line index, {'type', 'target', 'context'}) for each edge line,
        always empty when `problem` is set.
    problem: (line index, reason) for the first line that stops the note's
        edges from being indexed, or None.
    """

    __slots__ = ('heading', 'end', 'edges', 'problem')

    def __init__(self, heading, end, edges, problem):
        self.heading = heading
        self.end = end
        self.edges = edges
        self.problem = problem


def is_connections_heading(line: str) -> bool:
    """True when `line` (already split, no terminator) has the shape of the
    connections heading. It says nothing about fences; scan_connections
    checks them."""
    return _CONNECTIONS_HEADING_RE.fullmatch(line) is not None


def is_section_end(line: str) -> bool:
    """True when `line` has the shape of an ATX heading, which ends a
    connections section."""
    return _SECTION_END_RE.match(line) is not None


def trim_connection_ws(text: str) -> str:
    """Trim the explicit whitespace union shared with markdown-parser.ts."""
    return text.strip(_SLUG_WS_CHARS)


def normalize_newlines(text: str) -> str:
    """Turn CRLF and CR into LF, as Python's text-mode file reads do. Every
    connection reader and writer starts here, and the MCP server reads note
    files the same way (markdown-parser.ts normalizeNewlines)."""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _fence_end(lines: list[str], i: int) -> int | None:
    """If lines[i] opens a fence the rule accepts, the index of its closing
    line, else None."""
    m = _FENCE_OPEN_RE.fullmatch(lines[i])
    if m is None:
        return None
    prefix, run = m.group(1), m.group(2)
    for j in range(i + 1, len(lines)):
        ln = lines[j]
        if ln.strip(' \t') == '':
            continue
        if not ln.startswith(prefix):
            return None
        if _FENCE_LIKE_RE.match(ln):
            rest = ln[len(prefix):]
            if rest.startswith(run) and rest[len(run):].strip(' ') == '':
                return j
            return None
    return None


def _may_open_definition(s: str) -> bool:
    """For a line starting with `[`: False only when its first unescaped
    `]` comes before any other `[` and is not followed by `:`, so the line
    cannot start a link reference definition (whose label may run onto later
    lines). `[text](url)` and `[text] more` are safe; `[label]: /url`,
    `[a [b]`, `[a\\]` and `[open` are not."""
    i = 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == '\\':
            i += 2
            continue
        if c == '[':
            return True
        if c == ']':
            return s[i + 1:i + 2] == ':'
        i += 1
    return True


def _line_problem(line: str, in_section: bool) -> str | None:
    """Why `line` stops a note's edges from being indexed, or None. Fences
    are handled by scan_connections before this is called."""
    if _NON_MARKDOWN_SEPARATOR_RE.search(line):
        ch = _NON_MARKDOWN_SEPARATOR_RE.search(line).group(0)
        return (
            f'the line holds U+{ord(ch):04X}, which some readers take as a '
            'line break and Markdown does not'
        )
    s = line.lstrip(' \t')
    if s.startswith('```') or s.startswith('~~~'):
        return (
            'a fence the connection rule does not follow (it must open and '
            'close with the same run at the same indentation, with an info '
            'word of letters, digits and _+.#- only)'
        )
    if s.startswith('<'):
        return 'the line starts with "<" (HTML)'
    if s.startswith('[') and _may_open_definition(s):
        return 'the line starts like a link reference definition ("[label]:")'
    if not in_section or s == '':
        return None
    if s.startswith('>'):
        return 'a block quote in the Connections section'
    lead = line[:len(line) - len(s)]
    if '\t' in lead or len(lead) >= 4:
        return 'a line indented by a tab or 4+ spaces in the Connections section'
    # Whitespace at either end does not count: marked trims the end of a
    # list's last item with JS trimEnd(), so `=` + U+FEFF underlines it.
    rule = trim_connection_ws(s)
    if rule and all(c in _RULE_LINE_CHARS for c in rule):
        return (
            'a line of only "-", "=", "*", "_", "+", "|" or ":" in the '
            'Connections section (a heading underline, rule or table)'
        )
    m = _LIST_MARKER_WS_RE.match(line)
    if m is not None:
        ws = m.group(1)
        if '\t' in ws or len(ws) >= 4:
            return (
                'a list marker followed by a tab or 4+ spaces in the '
                'Connections section'
            )
    return None


def connection_line_edge(line: str) -> dict | None:
    """The edge `line` carries under the connection rule, or None: `-` at
    column 0, 1-3 spaces, then CONNECTION_RE on the trimmed line, a type that
    starts with an ASCII letter or digit, and a target that is not a bracket
    reference (`[moltbook]`, `[[wiki-note]]`, #415). Whether the line is in a
    section is scan_connections's business."""
    if not _EDGE_PREFIX_RE.match(line):
        return None
    m = CONNECTION_RE.match(trim_connection_ws(line))
    if m is None:
        return None
    conn_type, target, quoted_ctx, dash_ctx = m.groups()
    if conn_type[0] not in _ASCII_ALNUM or target.startswith('['):
        return None
    return {
        'type': conn_type,
        'target': target,
        'context': quoted_ctx or dash_ctx or None,
    }


def scan_connections(lines: list[str], start: int = 0) -> ConnectionScan:
    """Apply the connection rule to `lines` (LF-split, see
    normalize_newlines), ignoring lines before `start` (a writer passes the
    frontmatter's line count, note_body_start_line). One pass."""
    n = len(lines)
    heading = None
    end = n
    edges: list[tuple[int, dict]] = []
    problem = None
    i = start
    while i < n:
        line = lines[i]
        in_section = heading is not None
        if in_section and is_section_end(line):
            end = i
            break
        close = _fence_end(lines, i)
        if close is not None:
            if in_section and len(line) - len(line.lstrip(' ')) >= 4:
                problem = (i, 'a fence indented 4+ spaces in the Connections section')
                break
            for j in range(i, close + 1):
                if _NON_MARKDOWN_SEPARATOR_RE.search(lines[j]):
                    problem = (j, _line_problem(lines[j], False))
                    break
            if problem is not None:
                break
            i = close + 1
            continue
        if not in_section and is_connections_heading(line):
            heading = i
            i += 1
            continue
        reason = _line_problem(line, in_section)
        if reason is not None:
            problem = (i, reason)
            break
        if in_section:
            edge = connection_line_edge(line)
            if edge is not None:
                edges.append((i, edge))
        i += 1
    if problem is not None:
        edges = []
        if heading is None:
            # Report whether a heading follows, so the problem can be shown.
            for k in range(problem[0] + 1, n):
                if is_connections_heading(lines[k]):
                    heading = k
                    break
        end = n
        if heading is not None:
            for k in range(max(heading, problem[0]) + 1, n):
                if is_section_end(lines[k]):
                    end = k
                    break
    return ConnectionScan(heading, end, edges, problem)


def has_connections_heading(body: str) -> bool:
    """True when `body` has a connections heading line (found even after a
    problem, so a concept body with one is refused either way).
    markdown-parser.ts hasConnectionsHeading is its twin."""
    return scan_connections(split_connection_lines(normalize_newlines(body))).heading is not None


def connections_heading_near_misses(
    lines: list[str], heading: int | None = None,
) -> list[tuple[int, str]]:
    """Lines whose stripped text starts with `## Connections` (the earlier,
    looser heading rule) that the current rule does not take as the heading,
    each with the reason. `heading` is the heading scan_connections found on
    the same lines (or None). `schist schema --validate` warns on these."""
    out: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        if not ln.strip().startswith(CONNECTIONS_HEADING_TEXT):
            continue
        if is_connections_heading(ln):
            if heading is None or i < heading:
                out.append((i, 'inside a fenced code block'))
            elif i > heading:
                out.append((
                    i, 'a later ## Connections heading; only the first section is read',
                ))
            continue
        indent = ln[:len(ln) - len(ln.lstrip())]
        if indent.strip(' \t'):
            odd = next(c for c in indent if c not in ' \t')
            out.append((
                i,
                f'starts with U+{ord(odd):04X}, which is not a space or tab, '
                'so the line is not a heading',
            ))
        elif indent:
            out.append((i, 'indented (the heading must start at column 0)'))
        else:
            out.append((
                i, f"heading text is not exactly '{CONNECTIONS_HEADING_TEXT}'",
            ))
    return out


# str.isspace() (and Python's re `\s`): the whitespace python-frontmatter's
# text.strip() and FM_BOUNDARY `\s*` use. markdown-parser.ts carries the same
# string; schema/frontmatter-boundary-parity.json pins both.
PY_WHITESPACE = (
    '\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0 '
    '        '
    '       　'
)

_CM_LINE_END_RE = re.compile(r'\r\n|\r|\n')


def split_connection_lines(text: str) -> list[str]:
    """Split on CommonMark line endings only (\\n, \\r\\n, \\r), the way a
    Markdown renderer does. VT, FF, U+001C-U+001E, NEL, LS and PS stay inside
    a line. A final line ending adds no empty line (like str.splitlines()).
    Every connection reader and writer splits with this; markdown-parser.ts
    splitConnectionLines is its twin."""
    if not text:
        return []
    parts = _CM_LINE_END_RE.split(text)
    if parts[-1] == '':
        parts.pop()
    return parts


def _delimiter_match(s: str, p: int, kind: str) -> int | None:
    """If a frontmatter boundary of `kind` starts at line start `p` of `s`,
    return where python-frontmatter's FM_BOUNDARY match ends, else None.
    YAML is `^-{3,}\\s*$` and TOML `^\\+{3,}\\s*$` (re.MULTILINE, so `$` is
    only before "\\n" or at the end); JSON is `^(?:{|})$`."""
    n = len(s)
    if kind == 'json':
        if p < n and s[p] in '{}' and (p + 1 == n or s[p + 1] == '\n'):
            return p + 1
        return None
    ch = '-' if kind == 'yaml' else '+'
    q = p
    while q < n and s[q] == ch:
        q += 1
    if q - p < 3:
        return None
    e = q
    while e < n and s[e] in PY_WHITESPACE:
        e += 1
    if e == n:
        return n  # greedy `\\s*` reaches the end
    last_nl = s.rfind('\n', q, e)
    return None if last_nl == -1 else last_nl


def note_body_offset(text: str) -> int:
    """Index in `text` where the note body starts: just after the frontmatter's
    closing line. This is python-frontmatter's rule (what ingest parses
    metadata with), written out so markdown-parser.ts frontmatterLength can
    match it exactly: turn CRLF and CR into LF (as a text-mode file read
    does; ingest and the MCP server read note files that way, see
    normalize_newlines), strip str.isspace() whitespace,
    detect YAML (`---`), then JSON (`{`), then TOML (`+++`) on the first line,
    and close at the next boundary line. 0 when there is no frontmatter (a
    BOM before `---` means none) or it is never closed (python-frontmatter
    then treats the whole text as the body). Unlike post.content, the body is
    not stripped, so a body that opens with an indented line keeps its
    indentation. schema/frontmatter-boundary-parity.json pins this in both
    languages.
    """
    # CR alone becomes LF too (same length, so offsets map back as below).
    norm = normalize_newlines(text)
    lead = 0
    while lead < len(norm) and norm[lead] in PY_WHITESPACE:
        lead += 1
    end = len(norm)
    while end > lead and norm[end - 1] in PY_WHITESPACE:
        end -= 1
    s = norm[lead:end]
    for kind in ('yaml', 'json', 'toml'):
        first = _delimiter_match(s, 0, kind)
        if first is not None:
            break
    else:
        return 0
    p = s.find('\n', first)
    while p != -1:
        start = p + 1
        if _delimiter_match(s, start, kind) is not None:
            nl = norm.find('\n', lead + start)
            off = len(norm) if nl == -1 else nl + 1
            # Map the offset in `norm` back onto `text`: each CRLF before it
            # was one character shorter in `norm`.
            pairs = 0
            i = text.find('\r\n')
            while i != -1 and i - pairs < off:
                pairs += 1
                i = text.find('\r\n', i + 2)
            return off + pairs
        p = s.find('\n', start)
    return 0


def note_body_start_line(text: str) -> int:
    """Number of split_connection_lines lines before the body (see
    note_body_offset). Writers pass it as scan_connections's `start`."""
    return len(split_connection_lines(text[:note_body_offset(text)]))


def _normalize_concept_slug(value: str) -> str:
    """Mirror mcp-server normalizeConceptSlug: strip edge whitespace,
    lowercase, collapse each whitespace RUN to one dash (not just single
    spaces). The run-collapse matters: delete_note's cascade compares slugs
    it normalizes in TS against slugs this function stored in the index, and
    a `foo--bar` / `foo-bar` skew silently leaves dangling refs. See #303."""
    return _SLUG_WS_RUN.sub('-', value.strip(_SLUG_WS_CHARS).lower())


def _normalize_tag(value: str) -> str:
    return value.strip().lstrip('#').strip()


def _default_schema_path() -> Path:
    return Path(__file__).parent / 'default.yaml'


class SchemaConfigError(RuntimeError):
    """A schema-config problem the OPERATOR can act on, as opposed to a defect.

    Two conditions reach a user this way: a packaged ``default.yaml`` that is
    missing or defines no usable ``directories`` (a broken install), and a
    schema config that cannot be read or parsed. Both used to surface as a
    bare ``RuntimeError``, which the console-script entrypoints did not catch
    — so an actionable "reinstall the CLI" message arrived as a raw Python
    traceback, indistinguishable from an internal crash to a reader or to an
    agent parsing stderr (#603).

    Subclasses ``RuntimeError`` deliberately. The two ``except (OSError,
    RuntimeError)`` symlink-loop guards, and any caller written against the
    older contract, keep working unchanged; the narrower type only lets the
    entrypoints tell "the install is broken" apart from "this is a bug",
    which must still surface its traceback rather than be dressed up as
    operator error.
    """


def _read_schema_config(path: Path) -> dict:
    """Parse a schema config, reporting every failure as the same RuntimeError.

    Both reads in ``_configured_content_roots`` go through here.  The fallback
    read used to be bare, so a vault with no ``schist.yaml`` — the common case,
    and the only one that reaches the fallback at all — surfaced a raw
    ``OSError`` where the guarded first read promised a ``RuntimeError`` (#578).
    """
    try:
        parsed = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        raise SchemaConfigError(f'could not read schema config {path}: {e}') from e
    return parsed if isinstance(parsed, dict) else {}


def _configured_content_roots(vault: Path) -> set[str]:
    """Return configured top-level content directories for this vault.

    ``schist.yaml`` is the write-side boundary used by both CLI and MCP.  The
    derived index must honor the same boundary; otherwise repository support
    files (README, shared skills, CI documentation) silently become notes and
    make schema validation impossible to use as a quality gate.
    """
    vault_schema = vault / 'schist.yaml'
    default_path = _default_schema_path()
    config_path = vault_schema if vault_schema.exists() else default_path
    parsed = _read_schema_config(config_path)

    directories = parsed.get('directories')
    if not isinstance(directories, list):
        # Fall back to the packaged canonical list, whose `directories` is a
        # dict.  When config_path IS default.yaml we already hold that parse:
        # re-reading the same file was redundant, and the gap between the two
        # reads was a window in which the file could change underneath us.
        default = parsed if config_path == default_path else _read_schema_config(default_path)
        canonical = default.get('directories') or {}
        if isinstance(canonical, dict):
            directories = list(canonical.values())
        elif isinstance(canonical, list):
            directories = canonical
        else:
            # A scalar would otherwise be iterated CHARACTERWISE below, making
            # roots like {'n', 'o', 't', ...} — every real directory excluded,
            # no error.
            directories = []
        if not directories:
            # The packaged list is the last authority; if it yields nothing,
            # every file below the vault root is excluded (see the roots check
            # in `ingest`) and the index comes out near-empty with no error.
            # That is the same class of defect as #574 in the other direction,
            # and it means a broken install rather than a choice: an explicit
            # `directories: []` in schist.yaml is a list, so it never reaches
            # here. Fail loud instead.
            raise SchemaConfigError(
                f'no content directories configured: {default_path} defines no usable '
                '`directories`. The packaged schema config is missing or corrupt; '
                'reinstall the CLI (`uv tool install --reinstall <path-to-cli>`).',
            )

    roots = set()
    for value in directories or []:
        parts = Path(str(value).rstrip('/')).parts
        if parts:
            roots.add(parts[0].casefold())
    return roots


# Authority tiers for a concept definition, LOWER wins. Three tiers and not
# two, because `concepts/` and `Concepts/` are the SAME directory on a
# case-insensitive filesystem (APFS/HFS+) but DIFFERENT ones on a
# case-sensitive filesystem (ext4 — the hub). Collapsing the two axis tiers
# into one would let `Concepts/x.md`, which sorts BEFORE `concepts/x.md`, take
# the slug from the literal axis on ext4 — the very hijack this ranking exists
# to prevent.
CONCEPT_AXIS_EXACT = 1      # `concepts/` — the reserved axis, always wins
CONCEPT_AXIS_CASEFOLD = 2   # `Concepts/` — the reserved axis on APFS/HFS+ only
CONCEPT_AXIS_NONE = 3       # a `concept:`-keyed file anywhere else


def _concept_axis_rank(rel: Path) -> int:
    """Authority rank of the concept definition at `rel` (lower wins).

    Sort order is NOT a proxy for authority: ANY directory sorting before
    `concepts/` — `Archive/`, `Papers/`, `2026-inbox/`, anything capitalized,
    since uppercase precedes lowercase in ASCII — is processed first. Ranking
    by path instead of arrival order is what keeps the reserved axis
    authoritative regardless of what else claims the slug.
    """
    if len(rel.parts) > 1:
        top = rel.parts[0]
        if top == 'concepts':
            return CONCEPT_AXIS_EXACT
        if top.casefold() == 'concepts':
            return CONCEPT_AXIS_CASEFOLD
    return CONCEPT_AXIS_NONE


def _opens_quoted_scalar(last_significant: str) -> bool:
    return last_significant in {'', '[', '{', ',', ':'}


def _trailing_backslashes(line: str, i: int) -> int:
    count = 0
    j = i - 1
    while j >= 0 and line[j] == '\\':
        count += 1
        j -= 1
    return count


def _quote_flow_hashtags(line: str) -> str:
    """Quote unquoted #tags inside YAML flow collections before YAML parsing."""
    if '#' not in line or ('[' not in line and '{' not in line):
        return line

    result: list[str] = []
    flow_depth = 0
    in_single = False
    in_double = False
    last_significant = ''

    i = 0
    while i < len(line):
        ch = line[i]

        if in_single:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    result.append("''")
                    i += 2
                    continue
                in_single = False
                last_significant = "'"
            result.append(ch)
            i += 1
            continue

        if in_double:
            if ch == '"' and _trailing_backslashes(line, i) % 2 == 0:
                in_double = False
                last_significant = '"'
            result.append(ch)
            i += 1
            continue

        if ch == "'" and _opens_quoted_scalar(last_significant):
            in_single = True
            last_significant = "'"
            result.append(ch)
            i += 1
            continue
        if ch == '"' and _opens_quoted_scalar(last_significant):
            in_double = True
            last_significant = '"'
            result.append(ch)
            i += 1
            continue
        if ch in {'[', '{'}:
            flow_depth += 1
            last_significant = ch
            result.append(ch)
            i += 1
            continue
        if ch in {']', '}'}:
            if flow_depth > 0:
                flow_depth -= 1
            last_significant = ch
            result.append(ch)
            i += 1
            continue
        if ch == '#' and flow_depth > 0:
            match = HASHTAG_AT_START_RE.match(line[i:])
            if match:
                tag = match.group(0)
                result.append(f'"{tag}"')
                last_significant = tag[-1]
                i += len(tag)
                continue

        result.append(ch)
        if ch not in {' ', '\t'}:
            last_significant = ch
        i += 1

    return ''.join(result)


def patch_frontmatter_flow_hashtags(content: str) -> str:
    """Mirror mcp-server parseNote's pre-patch before frontmatter parsing."""
    lines = content.split('\n')
    in_fm = False
    patched = False
    patched_lines = []
    for i, ln in enumerate(lines):
        if i == 0 and ln.strip() == '---':
            in_fm = True
            patched_lines.append(ln)
            continue
        if in_fm and ln.strip() == '---':
            in_fm = False
            patched_lines.append(ln)
            continue
        if in_fm:
            next_ln = _quote_flow_hashtags(ln)
            patched = patched or next_ln != ln
            ln = next_ln
        patched_lines.append(ln)

    return '\n'.join(patched_lines) if patched else content


def scan_note_connections(text: str) -> tuple[list[str], ConnectionScan]:
    """The connection rule applied to a whole note file's text: its lines
    (LF-split after normalize_newlines) and the scan of its body, with line
    indexes into those lines. Writers use this; it finds the same edges as
    parse_connections(connection_body(text))."""
    text = normalize_newlines(text)
    lines = split_connection_lines(text)
    return lines, scan_connections(lines, note_body_start_line(text))


def connection_body(text: str) -> str:
    """The note body the connection rule reads, and the text ingest stores in
    docs.body for the viewer to render: CRLF and CR turned into LF, the text
    after the frontmatter's closing line (note_body_offset), without leading
    and trailing blank lines (lines of only spaces and tabs). Nothing else is
    stripped, so the viewer renders the lines the rule read.
    markdown-parser.ts connectionBody is its twin."""
    text = normalize_newlines(text)
    lines = text[note_body_offset(text):].split('\n')
    a, b = 0, len(lines)
    while a < b and lines[a].strip(' \t') == '':
        a += 1
    while b > a and lines[b - 1].strip(' \t') == '':
        b -= 1
    return '\n'.join(lines[a:b])


def parse_connection_lines(body: str) -> list[tuple[int, dict]]:
    """Edges of the ## Connections section of a note body (see
    scan_connections for the rule), each with its index in
    split_connection_lines(normalize_newlines(body)). Empty when the rule
    finds a problem."""
    return scan_connections(split_connection_lines(normalize_newlines(body))).edges


def parse_connections(body: str) -> list[dict]:
    """Extract connections from the ## Connections section of a note body,
    as connection_body returns it."""
    return [edge for _i, edge in parse_connection_lines(body)]


def title_from_filename(filename: str) -> str:
    """Derive title from filename: 2026-03-26-foo-bar.md -> foo bar."""
    stem = Path(filename).stem
    # Strip leading date pattern
    stem = re.sub(r'^\d{4}-\d{2}-\d{2}-?', '', stem)
    return stem.replace('-', ' ').strip() or stem


def _string_or_none(value) -> str | None:
    if isinstance(value, str):
        return value if value else None
    if value is not None and not isinstance(value, (list, dict)):
        return str(value)
    return None


def _int_or_none(value) -> int | None:
    # bool is an int subclass: `year: true` must coerce to NULL, not store 1.
    if isinstance(value, bool):
        return None
    # isdecimal, NOT isdigit: superscripts like "²" are isdigit()-True but
    # int() rejects them (ValueError aborted the whole rebuild pre-#351);
    # isdecimal() is False for them and True for exactly the digits int()
    # parses (including e.g. Arabic-Indic "٢٠٢٠").
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, int):
        # SQLite INTEGER is signed 64-bit; a wider Python int raises
        # OverflowError at bind time, which would savepoint-drop the WHOLE
        # note — coerce out-of-range values to NULL like every other
        # invalid year instead.
        return value if -(2**63) <= value < 2**63 else None
    return None


def _authors_json(value) -> str | None:
    if isinstance(value, list):
        authors = [a for a in value if isinstance(a, str) and a]
        return json.dumps(authors) if authors else None
    if isinstance(value, str) and value:
        return json.dumps([value])
    return None


def _verification_sources_json(value) -> str | None:
    sources: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                sources.append(item)
            if isinstance(item, dict) and item:
                for key, val in item.items():
                    sources.append(f'{key}:{val}' if val is not None else str(key))
        return json.dumps(sources) if sources else None
    if isinstance(value, str) and value:
        return json.dumps([value])
    return None


def paper_metadata_from_frontmatter(meta: dict, rel: Path) -> tuple | None:
    """Return a paper_metadata row tuple for paper-like frontmatter."""
    is_paper_path = len(rel.parts) > 1 and rel.parts[0] == 'papers'
    has_paper_fields = any(field in meta for field in PAPER_FIELDS)
    if not is_paper_path and not has_paper_fields:
        return None

    verification = meta.get('verification')
    if not isinstance(verification, dict):
        verification = {}
    verified_date = _string_or_none(verification.get('verified_on'))
    verified_by = _string_or_none(verification.get('verified_by'))
    verification_sources = _verification_sources_json(verification.get('verified_against'))
    verified = 1 if verified_date else 0

    return (
        _authors_json(meta.get('authors')),
        _int_or_none(meta.get('year')),
        _string_or_none(meta.get('venue')),
        _string_or_none(meta.get('type')),
        _string_or_none(meta.get('doi')),
        _string_or_none(meta.get('arxiv_id')),
        _string_or_none(meta.get('pubmed_pmid')),
        _string_or_none(meta.get('bibtex_key')),
        verified,
        verified_by,
        verified_date,
        verification_sources,
        _string_or_none(meta.get('url')),
    )


def ingest(vault_path: str, db_path: str):
    vault = Path(vault_path).resolve()
    schema_path = Path(__file__).parent / 'schema.sql'

    conn = sqlite3.connect(db_path)
    try:
        _ingest_into(conn, vault, schema_path)
    finally:
        conn.close()


def _ingest_into(conn: sqlite3.Connection, vault: Path, schema_path: Path) -> None:
    """Run the actual ingest against an open connection.

    Split out from `ingest()` so the connection close is guaranteed via
    try/finally even when the loop raises. The split also lets the
    schema executescript stay outside the transaction (it commits
    implicitly), while the data INSERTs are wrapped in a transaction
    that rolls back on failure — `_run_ingest` then deletes the
    schema-only DB so the next get_db() rebuilds from scratch instead
    of trusting an empty index.
    """
    # WAL lets the MCP server keep reading while ingest rebuilds the tables;
    # rollback-journal mode holds an EXCLUSIVE lock through the DDL phase and
    # concurrent readers fail with SQLITE_BUSY. But WAL is unsafe when the
    # vault lives on a network filesystem — SQLite requires all processes
    # using a WAL DB to be on the same host, and HPC spokes (e.g. orcd) may
    # serve the vault over NFS/Lustre to login and compute nodes at once.
    # SCHIST_NO_WAL=1 keeps the pre-#254 rollback-journal behavior there.
    # Must run BEFORE executescript so the DROP/CREATE phase never touches
    # WAL mode on such deployments; journal_mode requires autocommit, which
    # holds here (fresh connection, no open transaction). See #254.
    mode = 'DELETE' if env_flag('SCHIST_NO_WAL') else 'WAL'
    conn.execute(f'PRAGMA journal_mode={mode}')

    # Completion marker AND schema version in one value (#244, #130 D3):
    # clear it before the schema DDL commits, stamp INDEX_SCHEMA_VERSION back
    # atomically with the data commit at the end — "complete at schema
    # version N". A SIGKILL between the two (OOM killer, CI timeout, kill -9)
    # leaves user_version=0 with empty committed tables, which get_db()
    # detects and heals by re-ingesting — the on-failure unlink in
    # _run_ingest only covers Python exceptions.
    conn.execute('PRAGMA user_version = 0')

    conn.executescript(schema_path.read_text())

    # All data INSERTs run in ONE explicit transaction (see the docstring
    # above: rollback-on-failure + _run_ingest's partial-DB unlink). The
    # explicit BEGIN matters now that each file opens a SAVEPOINT: a
    # savepoint issued in autocommit mode starts its own transaction, so
    # its RELEASE would COMMIT after every file instead of nesting.
    conn.execute('BEGIN')

    doc_count = 0
    edge_count = 0
    # WARN-skipped files (surfaced in the summary line). WARNs themselves go
    # to STDERR: every agent-driven ingest channel discards stdout
    # (triggerIngestion spawns with stdio "ignore", runIngestSync pipes only
    # stderr, the post-commit hook's stdout is dropped by git-writer), so a
    # stdout-only WARN vanishes without trace.
    skipped_count = 0

    # Slug -> best (axis tier, stem agreement) pair claimed by a REAL concept
    # definition this run; see _concept_axis_rank for the tiers and the loop for
    # the stem component. Compared as a tuple, so the axis dominates and the
    # filename only breaks ties WITHIN a tier.
    # The concept UPSERT (below) overwrites placeholder stub rows to fix
    # #468, but it must not let a second is_concept file silently clobber a
    # MORE AUTHORITATIVE one: is_concept is true for any `concept:`-keyed file
    # anywhere, not just under concepts/. #470 resolved that by arrival order,
    # which is wrong whenever a claimant sorts before the axis it is competing
    # with (see _concept_axis_rank). A definition is replaced only by a
    # STRICTLY better rank, so within one tier the first still wins and #470's
    # duplicate-definition behavior is unchanged; placeholders always yield.
    concept_slug_rank: dict = {}

    # rglob('*.md') follows symlinks — a *.md FILE symlink is yielded on
    # every Python version, and on 3.12 patch releases predating the glob
    # reimplementation backport rglob also RECURSES INTO symlinked
    # directories (project floor is >=3.12, any patch) — so anything whose
    # real path escapes the vault would be indexed (#342). is_symlink()
    # alone can't catch the symlinked-dir case (files inside it are not
    # themselves symlinks), hence the containment check on the resolved
    # path.
    vault_real = vault.resolve()
    content_roots = _configured_content_roots(vault)

    for md_file in sorted(vault.rglob('*.md')):
        rel = md_file.relative_to(vault)
        if not rel.parts:
            continue
        if rel.name.casefold() == 'readme.md':
            continue
        if len(rel.parts) == 1:
            if rel.name.casefold() in ROOT_SUPPORT_DOCS:
                continue
        elif rel.parts[0].casefold() not in content_roots:
            continue
        # Skip hidden/excluded dirs
        if any(part in SKIP_DIRS for part in rel.parts):
            continue

        try:
            resolved = md_file.resolve()
        except (OSError, RuntimeError) as e:
            # resolve() raises RuntimeError (not OSError) on a symlink LOOP on
            # Python <=3.12 — the project floor — so both must be caught, else
            # one looping .md symlink crashes the whole ingest (#342 follow-up).
            print(f'  WARN: skipping {rel} — unresolvable path: {e}', file=sys.stderr)
            skipped_count += 1
            continue
        if not resolved.is_relative_to(vault_real):
            print(f'  WARN: skipping {rel} — resolves outside the vault (symlink)', file=sys.stderr)
            skipped_count += 1
            continue
        # The SKIP_DIRS filter above checks the symlink's OWN path, so a
        # symlink like notes/leak.md -> ../.git/config would pass both it and
        # the containment check while exposing excluded content (e.g. remote
        # URLs with credentials) to the index — re-check the RESOLVED path.
        if any(part in SKIP_DIRS for part in resolved.relative_to(vault_real).parts):
            print(f'  WARN: skipping {rel} — resolves into an excluded directory (symlink)', file=sys.stderr)
            skipped_count += 1
            continue

        # One SAVEPOINT per file (#351): any exception past this point —
        # read, YAML parse, type-guarding, sqlite binding, paper/concept/
        # edge processing — rolls back just this file's writes and the loop
        # moves on. Previously only the read+parse phase was guarded, so an
        # INSERT-phase exception rolled back the single transaction and
        # _run_ingest deleted the partial DB: one bad note = a vault-wide
        # read outage (#296 family).
        conn.execute('SAVEPOINT file_sp')
        try:
            raw_text = md_file.read_text(encoding='utf-8')
            patched_text = patch_frontmatter_flow_hashtags(raw_text)
            post = frontmatter.loads(patched_text)
            meta = post.metadata
            # docs.body and the connections come from one text,
            # connection_body, not python-frontmatter's post.content: its
            # strip() would turn an indented first line (a code block) into
            # a heading, and the viewer renders docs.body, so it must show
            # the lines the edges were read from.
            body = connection_body(patched_text)

            # Per-file edge counter, folded into the total only after RELEASE —
            # a rolled-back file must not inflate the printed count. Concepts
            # are counted authoritatively after the loop (SELECT count) so the
            # tally can't drift with file-processing order (#486, #492).
            file_edges = 0

            # Frontmatter reads (here and in paper_metadata_from_frontmatter) must
            # stay literal meta.get('<field>') / verification.get('<field>')
            # expressions in THIS file — cli/tests/test_frontmatter_contract.py
            # scans this source to pin the read set to
            # schema/frontmatter-contract.json, and indirect or variable-key reads
            # evade that check. Update the contract when adding a field here.

            # Normalize tags: strip # prefix
            raw_tags = meta.get('tags', [])
            if isinstance(raw_tags, list):
                tags = [
                    tag
                    for t in raw_tags
                    if isinstance(t, str) and (tag := _normalize_tag(t))
                ]
            else:
                tags = []

            tags_json = json.dumps(tags) if tags else None

            # Concepts from frontmatter
            raw_concepts = meta.get('concepts', [])
            if isinstance(raw_concepts, list):
                concepts = [
                    _normalize_concept_slug(c)
                    for c in raw_concepts
                    if isinstance(c, str) and c.strip()
                ]
            else:
                concepts = []
            concepts_json = json.dumps(concepts) if concepts else None

            # Scope: explicit frontmatter > directory path > 'global'
            raw_scope = meta.get('scope')
            if isinstance(raw_scope, str) and raw_scope:
                scope = raw_scope
            elif len(rel.parts) > 1:
                scope = rel.parent.as_posix()
            else:
                scope = 'global'

            # Source: from frontmatter, defaults to None
            raw_source = meta.get('source')
            source = raw_source if raw_source in {"human", "agent"} else None

            # Confidence: from frontmatter, validated against enum.
            # NULL when not declared — distinguishes "agent didn't say" from
            # "agent said medium" (don't default to medium here, see issue #69).
            raw_confidence = meta.get('confidence')
            confidence = raw_confidence if raw_confidence in {"low", "medium", "high"} else None
            raw_file_ref = meta.get('file_ref')
            file_ref = raw_file_ref if isinstance(raw_file_ref, str) and raw_file_ref else None

            doc_id = str(rel)

            # Determine if this is a concept file (in concepts/ dir or has
            # 'concept' key). Case-folded to match the write guards (add/link,
            # #454/#472) and `schema --validate`: on a case-insensitive
            # filesystem `Concepts/` IS the reserved axis, and a byte-exact
            # test here made the indexer store as a plain document exactly the
            # files the validator reports as malformed concepts.
            is_concept = 'concept' in meta or (
                rel.parts[0].casefold() == 'concepts'
                if len(rel.parts) > 1 else False
            )

            # Title: explicit title > topic > concept key > derive from filename.
            # Each candidate must be a NON-EMPTY STRING to be picked: a truthy
            # non-string (title: [a, b], title: {k: v}) previously flowed into the
            # docs INSERT and aborted the ENTIRE rebuild with a sqlite3 binding
            # error — one bad note must never take down the index (#296 family) —
            # while title: 42 landed with native affinity like the status case
            # (#278). Non-string candidates fall through the chain instead.
            title = next(
                (
                    v
                    for v in (meta.get('title'), meta.get('topic'), meta.get('concept'))
                    if isinstance(v, str) and v
                ),
                title_from_filename(rel.name),
            )

            # Status: type-guard like every other scalar field above. A non-string
            # value (status: 42, status: true, status: [draft]) would otherwise be
            # stored with SQLite's native affinity, so `WHERE status = 'draft'`
            # silently misses it and TS readers treating status as string break.
            # (#278; distinct from #276 which validates the string against
            # config.statuses in create_note.)
            raw_status = meta.get('status')
            status = raw_status if isinstance(raw_status, str) else None

            # Insert into docs
            date_val = meta.get('date')
            if date_val is not None:
                date_val = str(date_val)
            conn.execute(
                'INSERT INTO docs (id, title, date, status, tags, concepts, body, scope, source, confidence, file_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (doc_id, title, date_val, status, tags_json, concepts_json, body, scope, source, confidence, file_ref),
            )

            paper_row = paper_metadata_from_frontmatter(meta, rel)
            if paper_row is not None:
                conn.execute(
                    """
                    INSERT INTO paper_metadata (
                        doc_id, authors, year, venue, paper_type, doi, arxiv_id,
                        pubmed_pmid, bibtex_key, verified, verified_by,
                        verified_date, verification_sources, url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (doc_id, *paper_row),
                )

            # Insert concept record if this is a concept file
            slug = ''
            if is_concept:
                raw_slug = meta.get('concept', rel.stem)
                slug = _normalize_concept_slug(
                    raw_slug if isinstance(raw_slug, str) else rel.stem
                )
                # `concept: ""` (or all-whitespace) normalizes to "" and used to
                # land a slug-"" row in the concepts table, poison that doc's
                # `concepts` array, and — because "" was recorded as claimed —
                # silently drop every LATER blank-key file too (#482). The
                # `isinstance(str)` fallback above cannot catch it: "" IS a str,
                # so `rel.stem` is never reached. What to do depends on why the
                # file counts as a concept at all.
                if not slug and _concept_axis_rank(rel) < CONCEPT_AXIS_NONE:
                    # It lives ON the concept axis, so the directory already
                    # declares it a concept — fall back to the stem exactly as a
                    # file carrying no `concept:` key would. Refusing here would
                    # drop a real concept out of the graph over a blank field.
                    slug = _normalize_concept_slug(rel.stem)
                if not slug:
                    # Off-axis with a blank key, or on-axis with a stem that
                    # yields nothing either: the only thing marking this a
                    # concept was the empty key, so there is no name to give it.
                    # Skip rather than invent one, and say so on stderr — the
                    # channel the agent-driven ingest paths actually surface.
                    print(
                        f'  WARN: {rel} — `concept:` is empty and no slug can be '
                        f'derived from the filename; not indexed as a concept',
                        file=sys.stderr,
                    )
                    is_concept = False

            if is_concept:
                desc = body.split('\n\n')[0].strip() if body else None
                concept_tags = json.dumps(tags) if tags else None
                # Second component: does the FILENAME agree with the slug? A
                # `concept:` key that disagrees with its own stem is a squatter
                # — `concepts/aaa.md` carrying `concept: zzz` claimed `zzz` and
                # blocked canonical `concepts/zzz.md`, which sorts later (#479).
                # Both are tier 1, so the axis rank alone cannot separate them.
                # 0 = stem agrees (the shape add-concept/create_concept write,
                # and every file with no `concept:` key at all), 1 = mismatch.
                stem_rank = 0 if slug == _normalize_concept_slug(rel.stem) else 1
                axis_rank = (_concept_axis_rank(rel), stem_rank)
                prior_rank = concept_slug_rank.get(slug)
                if prior_rank is not None and prior_rank <= axis_rank:
                    # An equally- or more-authoritative definition already
                    # claimed this slug — keep it (first-definition-wins within
                    # a rank, matching pre-#470 behavior) instead of clobbering
                    # it with a later duplicate is_concept file. Only a
                    # strictly better rank takes the slug back.
                    pass
                else:
                    concept_slug_rank[slug] = axis_rank
                    conn.execute(
                        """
                        INSERT INTO concepts (slug, title, description, tags)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(slug) DO UPDATE SET
                            title = excluded.title,
                            description = excluded.description,
                            tags = excluded.tags
                        """,
                        (slug, title, desc, concept_tags),
                    )
            # Also insert concepts referenced in frontmatter
            for c in concepts:
                conn.execute(
                    'INSERT OR IGNORE INTO concepts (slug, title) VALUES (?, ?)',
                    (c, c.replace('-', ' ').title()),
                )
                if not is_concept:
                    result = conn.execute(
                        'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                        (doc_id, c, 'references', None),
                    )
                    file_edges += result.rowcount

            # Parse and insert edges
            for edge in parse_connections(body):
                try:
                    conn.execute(
                        'INSERT OR IGNORE INTO edges (source, target, type, context) VALUES (?, ?, ?, ?)',
                        (doc_id, edge['target'], edge['type'], edge['context']),
                    )
                    file_edges += 1
                except sqlite3.IntegrityError:
                    pass

            conn.execute('RELEASE SAVEPOINT file_sp')
            doc_count += 1
            edge_count += file_edges
        except Exception as e:
            # Only CONTENT-shaped failures may be skipped per-file: bad
            # YAML, invalid UTF-8 (#296 — a single corrupt note must never
            # leave the vault in a permanent read outage), type/binding
            # errors (ValueError/OverflowError/InterfaceError), constraint
            # violations (IntegrityError), and unsupported-type binds
            # (ProgrammingError on 3.12+, InterfaceError historically).
            # ENVIRONMENTAL DB failures (disk full, I/O error, corruption —
            # OperationalError and the rest of the DatabaseError family)
            # must fail LOUD instead: WARN-skipping them would stamp a
            # mostly-empty index as complete (user_version set), and
            # readers would serve that truncation as authoritative long
            # after the disk recovers. Re-raising keeps the pre-existing
            # fail-and-unlink path in _run_ingest. This also keeps
            # SQLITE_FULL/IOERR-class errors — whose automatic rollback can
            # destroy the savepoint — away from the ROLLBACK below.
            if isinstance(e, sqlite3.DatabaseError) and not isinstance(
                e, (sqlite3.IntegrityError, sqlite3.ProgrammingError)
            ):
                raise
            try:
                conn.execute('ROLLBACK TO SAVEPOINT file_sp')
                conn.execute('RELEASE SAVEPOINT file_sp')
            except sqlite3.Error:
                # The failure already destroyed the savepoint (automatic
                # rollback). The transaction is gone; propagate the ORIGINAL
                # error — not the cleanup's — so _run_ingest unlinks the DB.
                raise e
            if isinstance(e, UnicodeDecodeError):
                print(f'  WARN: skipping {rel} — invalid UTF-8: {e}', file=sys.stderr)
            else:
                print(f'  WARN: skipping {rel} — {type(e).__name__}: {e}', file=sys.stderr)
            skipped_count += 1
            continue

    conn.execute(
        """
        DELETE FROM concept_aliases
        WHERE duplicate_slug NOT IN (SELECT slug FROM concepts)
           OR canonical_slug NOT IN (SELECT slug FROM concepts)
        """
    )
    # Self-referential rows are semantically void and would loop any resolver.
    # New MCP writes reject them (#490/#495); this sweeps rows stored by
    # pre-#489 servers, mirroring the orphan prune above.
    conn.execute(
        "DELETE FROM concept_aliases WHERE duplicate_slug = canonical_slug"
    )
    # user_version lives in the DB header and is transactional, so the
    # completion marker lands atomically with the data commit (#244). The
    # stamped value is the index schema version (schema/index-contract.json;
    # bumped only on DDL changes to schema.sql) — readers that find a
    # different non-zero version force a rebuild, which IS the migration
    # path (#130 D3). PRAGMA takes no bound parameters; the :d format spec
    # guarantees an int lands in the statement.
    conn.execute(f'PRAGMA user_version = {INDEX_SCHEMA_VERSION:d}')
    conn.commit()
    # Authoritative concept tally: the concepts table is DROP/CREATEd every run
    # and ends holding exactly the distinct slugs this ingest produced (concept
    # definitions + first-seen frontmatter references). Counting the table
    # directly is immune to file-processing order — the per-file accumulator
    # double-counted in BOTH orderings (definition-before-reference #486,
    # stub-before-definition #492), because the INSERT OR IGNORE / ON CONFLICT
    # dedup that collapses duplicates happens in the table, not in the loop.
    concept_count = conn.execute('SELECT count(*) FROM concepts').fetchone()[0]
    summary = f'Ingested: {doc_count} docs, {concept_count} concepts, {edge_count} edges'
    if skipped_count:
        summary += f' ({skipped_count} skipped)'
    print(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description='Ingest markdown vault into SQLite')
    parser.add_argument('--vault', required=True, help='Path to vault root')
    parser.add_argument('--db', required=True, help='Path to SQLite database')
    args = parser.parse_args()
    # A broken install or an unreadable schema config is operator-actionable,
    # and this is the shape every other error path in the CLI uses. The MCP
    # server spawns this console script (tools.ts) and surfaces its stderr, so
    # a traceback here reached an agent as an internal crash (#603).
    # A broken install or an unreadable schema config is operator-actionable,
    # and this is the shape every other error path in the CLI uses. The MCP
    # server spawns this console script (tools.ts) and surfaces its stderr, so
    # a traceback here reached an agent as an internal crash (#603).
    try:
        ingest(args.vault, args.db)
    except SchemaConfigError as e:
        print(f'Error: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
