"""Markdown I/O — read, write, and manipulate vault notes."""

import os
import re
import stat
import tempfile

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


def is_round_trippable_token(text: str) -> bool:
    """True when `text` can occupy a CONNECTION_RE token slot (type or
    target): non-empty and free of every SLUG_WS_CHARS codepoint — the
    regex's token groups are each ONE non-whitespace run. Mirrors mcp-server
    isRoundTrippableTarget (#408); one definition serves both `link`'s
    target guard and the connection-type vocabulary filter (#413) so the
    two can't drift. Note this does NOT encode the target-only rules
    (leading-`[` bracket skip) — those stay at the target call site.
    """
    return bool(text) and not any(ch in SLUG_WS_CHARS for ch in text)


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


def write_note(path: str, fm: dict, body: str, exclusive: bool = False):
    """Write a markdown note with YAML frontmatter.

    exclusive=True opens with 'x' (O_CREAT|O_EXCL): the create FAILS with
    FileExistsError if the path exists — the only race-safe collision check
    (a probe-then-'w' is a TOCTOU; mirrors MCP writeNote's "wx" mode, #406/
    #408). O_EXCL also refuses to follow a symlink at the path, dangling
    included, atomically — 'w' would write the note THROUGH it, outside the
    vault if that's where it points.
    """
    post = frontmatter.Post(body, **fm)
    with open(path, 'x' if exclusive else 'w', encoding='utf-8') as f:
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


def _atomic_write(path: str, content: str, vault_root: str | None = None) -> None:
    """Write `content` to `path` atomically (write-to-temp + os.replace).

    A plain `open(path, 'w')` truncates the target at open() time, before any
    byte is written — a SIGKILL/OOM/power-loss in that window leaves a zero-byte
    note with the previous content gone (#425). We write to a unique temp file
    on the SAME filesystem (⇒ rename is atomic on POSIX) and `os.replace` over
    the target, so a reader/crash sees either the old file intact or the new
    file complete, never an empty intermediate. Mirrors sync.py's
    _atomic_write_hook and git-writer.ts's atomicWriteFile.

    `vault_root` selects WHERE the temp lives. A hard kill in the microsecond
    window between the write and the rename leaves the temp orphaned (the
    `except` cleanup never runs on SIGKILL). If that orphan sits in the target's
    own directory — a synced scope like `notes/` — the next `schist sync push`
    stages and commits it (junk-named, possibly a truncated partial note) and
    fans it out to the hub and every spoke (#433). So when a vault_root is
    given, the temp goes under `<vault_root>/.schist/tmp/` — gitignored
    (`.schist/`) and never a sync scope target (`_global_scope_targets`), so a
    leaked orphan is inert. `.schist/` is inside the vault tree, hence the same
    filesystem as the target, so os.replace stays atomic across the two dirs.
    Falls back to the target's own directory when no vault_root is supplied
    (bare notes outside a vault / unit tests) — still same-fs, still atomic.
    """
    if vault_root:
        dir_ = os.path.join(vault_root, '.schist', 'tmp')
        os.makedirs(dir_, exist_ok=True)
    else:
        dir_ = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        # mkstemp hardcodes the temp to 0600, and os.replace renames that inode
        # OVER the target — so without this the note would silently inherit 0600
        # and lose group/other read. git tracks only the exec bit, so the change
        # is invisible in history but real on disk (bites a shared hub). Preserve
        # the note's existing mode; a not-yet-existing file falls back to the
        # umask default, matching the old open('w', ...) behavior.
        try:
            os.chmod(tmp_path, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            cur = os.umask(0)
            os.umask(cur)
            os.chmod(tmp_path, 0o666 & ~cur)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def append_connection(path: str, connection_type: str, target: str, context: str | None = None,
                      vault_root: str | None = None):
    """Append a connection line to the ## Connections section (creating it if needed).

    Pass `vault_root` for any note inside a synced vault so the atomic-write temp
    lands under `.schist/tmp/` instead of alongside the note — see _atomic_write
    (#433). Omitting it (bare notes / tests) keeps the same-dir fallback.
    """
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

    _atomic_write(path, insert_connection_line(content, line), vault_root=vault_root)
