"""Markdown I/O — read, write, and manipulate vault notes."""

import re

import frontmatter

# Explicit whitespace set shared verbatim with ingest.py's _SLUG_WS_CHARS and
# mcp-server's SLUG_WS_CHARS (markdown-parser.ts): the UNION of Python's and
# JS's \s (30 codepoints). Native \s drifts between engines — Python adds
# U+001C–U+001F (C0 separators) and U+0085 (NEL), JS adds U+FEFF (ZWNBSP) —
# so under native \s a title containing e.g. U+0085 slugged to note id `a-b`
# from Python but `ab` from TS (#338). schema/title-slug-parity.json pins
# both implementations to the same table, like #318 did for concept slugs.
SLUG_WS_CHARS = (
    '\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680'
    '\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007'
    '\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
)
_NON_SLUG_RE = re.compile(f'[^a-z0-9{re.escape(SLUG_WS_CHARS)}-]')
_SLUG_WS_RUN_RE = re.compile(f'[{re.escape(SLUG_WS_CHARS)}]+')
_DASH_RUN_RE = re.compile(r'-+')


def slugify(title: str) -> str:
    """Lowercase, whitespace to hyphens, strip non-alphanum except hyphens.

    Mirrors mcp-server tools.ts titleSlug byte-for-byte (#338). Edge dashes
    are stripped with str.strip — LINEAR — never an anchored alternated
    regex (`^-+|-+$` backtracks quadratically over interior runs; see
    ingest.py's _SLUG_WS_RUN comment).
    """
    s = title.lower()
    s = _NON_SLUG_RE.sub('', s)
    s = _SLUG_WS_RUN_RE.sub('-', s)
    s = _DASH_RUN_RE.sub('-', s)
    return s.strip('-')


# The exact set of codepoints str.splitlines() treats as line boundaries —
# mirrors mcp-server markdown-parser.ts LINE_BOUNDARY (#398/#405). This is
# NOT the same set as SLUG_WS_CHARS above (that is the \s union; this is the
# strict splitlines set — e.g. \t and NBSP are whitespace but not boundaries).
# test_markdown_io.py pins this string against an exhaustive splitlines scan
# so it can never drift from the real splitter.
LINE_BOUNDARY_CHARS = '\n\x0b\x0c\r\x1c\x1d\x1e\x85\u2028\u2029'


def contains_line_boundary(text: str) -> bool:
    """True when `text` would split into more than one line on read.

    Derived from str.splitlines() itself — the SAME splitter
    insert_connection_line and ingest.parse_connections use — so the
    write-time guard and the read-time splitter can never disagree on what
    ends a line (the #359 principle; TS gets the same property by sharing
    LINE_BOUNDARY between containsLineBoundary and splitLinesLikePython).
    A connection target carrying such a character serializes into
    `- type: target` and splits back into MORE than one line on read, so
    ingest indexes a forged extra edge the caller never wrote (#398/#405).
    """
    return bool(text) and text.splitlines() != [text]


_FORGED_PREFIX_RE = re.compile(
    f'^-[{re.escape(SLUG_WS_CHARS)}]+[^{re.escape(SLUG_WS_CHARS)}]+:[{re.escape(SLUG_WS_CHARS)}]+'
)


def sanitize_context(context: str) -> str:
    """Flatten a connection context to a single safe line.

    Mirrors mcp-server markdown-parser.ts sanitizeContext (#398/#405):
    every splitlines boundary becomes a space FIRST (so the context can
    never split the serialized line and forge an edge on read), then a
    leading connection-entry-looking prefix is dropped (defense-in-depth),
    then embedded double-quotes become single-quotes because the serialized
    format delimits context with "..." (CONNECTION_RE: [^"]*). The prefix
    regex and the edge strip use the pinned SLUG_WS_CHARS union class per
    the #338 convention rather than replicating JS's \\s/trim() exactly, so
    the two implementations CAN disagree on exotic control chars (e.g. TS
    trim() drops a leading U+FEFF but keeps U+001F; the union strips both,
    and a U+001F embedded in a prefix word stops Python's non-ws run where
    TS's \\S continues). With boundaries already flattened every such
    divergence is cosmetic — a byte-different context string, never a
    forged edge.
    """
    safe = context
    for ch in LINE_BOUNDARY_CHARS:
        if ch in safe:
            safe = safe.replace(ch, ' ')
    safe = _FORGED_PREFIX_RE.sub('', safe)
    safe = safe.replace('"', "'")
    return safe.strip(SLUG_WS_CHARS)


def read_note(path: str) -> dict:
    """Read a markdown note, return dict with 'frontmatter' and 'body' keys."""
    post = frontmatter.load(path)
    return {'frontmatter': dict(post.metadata), 'body': post.content}


def write_note(path: str, fm: dict, body: str):
    """Write a markdown note with YAML frontmatter."""
    post = frontmatter.Post(body, **fm)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(frontmatter.dumps(post) + '\n')


def insert_connection_line(content: str, line: str) -> str:
    """Insert a connection line into content's ## Connections section.

    Splits with splitlines() — the SAME boundaries ingest.py:parse_connections
    reads with — never split('\\n'). split('\\n') left splitlines-only
    separators (\\v, \\f, NEL, U+2028/U+2029 — which universal-newline reads
    do NOT translate, unlike \\r/\\r\\n) fused into one giant "line", so the
    heading was "found" but the next `## ` break wasn't, and the new edge was
    appended after the following section — where parse_connections silently
    ignores it (#365). The section gate is the same line-scan the readers use
    (not a substring test): a mid-line prose mention of "## Connections" is
    not a heading, so the edge gets a real section instead of landing outside
    any section. Output is always '\\n'-joined with a single trailing newline,
    healing exotic separators on write the way the TS repair paths do.

    Byte-identical to mcp-server markdown-parser.ts insertConnectionLine;
    schema/connection-append-parity.json pins both, like #318/#338 did for
    slugs.
    """
    lines = content.splitlines()
    section_found = False
    in_section = False
    insert_idx = None
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not section_found and stripped.startswith('## Connections'):
            section_found = True
            in_section = True
            continue
        if in_section and stripped.startswith('## '):
            insert_idx = i
            break
    if not section_found:
        # Create section at end. Blank line after the heading matches the
        # MCP server's create-path shape so both writers mint one format.
        return content.rstrip() + '\n\n## Connections\n\n' + line + '\n'
    if insert_idx is None:
        lines.append(line)
    else:
        lines.insert(insert_idx, line)
    return '\n'.join(lines) + '\n'


def append_connection(path: str, connection_type: str, target: str, context: str | None = None):
    """Append a connection line to the ## Connections section (creating it if needed)."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Mirror mcp-server buildConnectionLine: context is sanitized here, at the
    # point of serialization, so EVERY caller gets a single-line entry — an
    # unsanitized context splits the line on read and its tail forges an edge
    # (#398/#405). An all-boundary context that sanitizes to '' is omitted,
    # matching the TS `if (safeContext)` gate.
    safe_context = sanitize_context(context) if context else ''
    if safe_context:
        line = f'- {connection_type}: {target} "{safe_context}"'
    else:
        line = f'- {connection_type}: {target}'

    with open(path, 'w', encoding='utf-8') as f:
        f.write(insert_connection_line(content, line))
