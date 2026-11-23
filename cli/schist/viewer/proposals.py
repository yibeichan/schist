# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Proposal queue for the viewer agent: the vault's equivalent of a PR.

The agent files proposals (a new note, or a new connection); each is a JSON
file under <vault>/.schist/proposals/, which is gitignored, so filing one
changes nothing in the vault. Approving a proposal runs the ordinary
`schist add` / `schist link` command, which validates it, takes the vault
write lock and commits; the spoke's usual sync then pushes it. Rejecting one
only marks it, unless the reviewer saves the reason as a standard. A proposal
whose command failed stays listed with the command's output, and can be
approved again or rejected.

Standards: a rejection's reason can be saved as a standard, an ordinary note
written by `schist add` into the rejected proposal's directory and tagged
`standard` and `applies-to:<directory>`. Before filing into a directory, the
agent must read that directory's standards (agent.get_standards) and name each
one in `checked_standards`; a proposal that leaves one out is refused, so a
reason given once is checked on every later proposal. Archiving a standard
note retires it.

On a spoke, a proposal may only target a path this spoke's identity can
write under vault.yaml, by the hub's own scope rule (pre_receive.derive_scope
and VaultACL.can_write), checked when it is filed and again when it is
approved: a commit the hub refuses would block every later push from the
spoke. Without a spoke identity or a parseable vault.yaml no check applies;
the hub stays the authority either way.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import re
import threading
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

import frontmatter

from .. import commands, markdown_io, writable
from ..ingest import _normalize_tag, patch_frontmatter_flow_hashtags

MAX_TITLE = 200
MAX_BODY = 50_000
MAX_REASON = 2_000
MAX_STANDARD_CHARS = 2_000   # of each standard's body, as shown to the agent
STANDARDS_BODY_CHARS = 8_000     # all bodies in one get_standards result, shared evenly
STANDARDS_RESULT_CHARS = 11_000  # one get_standards result as JSON; the agent cuts at 12000
MAX_UNSEEN_CHARS = 9_000         # ids a refusal names, so it too stays under the cut
STANDARD_TAG = 'standard'
APPROVE_TIMEOUT_S = 120
PENDING, APPROVED, REJECTED, FAILED = 'pending', 'approved', 'rejected', 'failed'
OPEN = (PENDING, FAILED)   # still awaiting a decision


class ProposalError(Exception):
    pass


def applies_tag(directory: str) -> str:
    return f'applies-to:{directory}'


def standards_for(db, directory: str) -> list[dict]:
    """The standards a proposal into `directory` must be checked against:
    notes directly in that directory tagged both `standard` and
    `applies-to:<directory>`, not archived, oldest id first. Only a note in
    the directory itself counts, so a writer confined to another directory
    cannot set rules for this one (on a hub, the directory's write scope
    decides who may add or retire its standards)."""
    prefix = f'{directory}/'
    rows = db.execute(
        "SELECT id, title, body FROM docs d"
        " WHERE substr(d.id, 1, ?) = ? AND instr(substr(d.id, ? + 1), '/') = 0"
        "   AND EXISTS (SELECT 1 FROM json_each(d.tags) WHERE value = ?)"
        "   AND EXISTS (SELECT 1 FROM json_each(d.tags) WHERE value = ?)"
        "   AND COALESCE(d.status, '') != 'archived'"
        " ORDER BY d.id",
        (len(prefix), prefix, len(prefix), STANDARD_TAG, applies_tag(directory))).fetchall()
    return [{'id': r[0], 'title': r[1], 'body': (r[2] or '')[:MAX_STANDARD_CHARS],
             'body_chars': len(r[2] or '')}
            for r in rows]


def standards_result(directory: str, found: list[dict]) -> dict:
    """get_standards' result for the agent, at most STANDARDS_RESULT_CHARS of
    JSON (the agent cuts any tool result at 12000). Every id comes first:
    bodies are shortened, then titles, before any standard is left out, and
    only when the ids alone do not fit is the tail left out and counted as
    `omitted` (the refusal then names the missing ids, so the agent is never
    locked out of a directory)."""
    share, title_cap = STANDARDS_BODY_CHARS // max(1, len(found)), MAX_TITLE
    keep = len(found)
    while True:
        items = []
        for s in found[:keep]:
            item = {'id': s['id']}
            if title_cap:
                item['title'] = s['title'][:title_cap]
            if share:
                item['body'] = s['body'][:share]
            if (s.get('body_chars', len(s['body'])) > len(item.get('body', ''))
                    or len(s['title']) > title_cap):
                item['truncated'] = True
            items.append(item)
        res = {'directory': directory, 'standards': items,
               'note': ('meet each standard and pass every id as checked_standards; '
                        'read_note a truncated one for its full text'
                        if found else 'no standards for this directory; pass []')}
        if keep < len(found):
            res['omitted'] = len(found) - keep
        if len(json.dumps(res, ensure_ascii=False)) <= STANDARDS_RESULT_CHARS:
            return res
        if share:
            share = share // 2 if share >= 100 else 0
        elif title_cap > 40:
            title_cap = 40
        elif title_cap:
            title_cap = 0
        elif keep:
            keep -= 1
        else:
            return res   # nothing left to cut; callers pass a configured directory


def configured_directories(vault) -> list:
    """The vault's configured directories. `commands._directories` exits the
    process on an unreadable schist.yaml; in the server that would kill the
    request without a response, so it becomes a ProposalError here."""
    try:
        return commands._directories(str(vault))
    except (SystemExit, Exception) as e:
        raise ProposalError(f'schist.yaml could not be read: {e or "see the server log"}') from None


def _configured_dir(vault, note_id: str) -> str:
    """The configured directory holding a note: the longest one that is a
    prefix of its id, so a link from notes/sub/x.md answers to notes/'s
    standards, the same directories get_standards accepts. '' for a note in
    no configured directory."""
    dirs = [d for d in configured_directories(vault) if note_id.startswith(f'{d}/')]
    return max(dirs, key=len) if dirs else ''


def _directory_of(vault, p: dict) -> str:
    """The directory a proposal writes into: the new note's, or for a link the
    configured directory of the source note, which `schist link` rewrites.
    '' when there is none. Filing records it as `standards_dir`, which the
    page shows instead of working it out again."""
    if p['kind'] == 'create':
        directory = p['args']['directory']
        return directory if directory in configured_directories(vault) else ''
    return _configured_dir(vault, p['args']['source'])


def _one_line(text: str) -> str:
    """Collapse every line break (\\n, \\r, U+2028, …) and run of spaces."""
    return ' '.join(str(text).split())


def _require_checked(db, directory: str, checked) -> list[str]:
    titles = {s['id']: s['title'] for s in standards_for(db, directory)}
    required = list(titles)
    if checked is None:
        checked = []
    elif isinstance(checked, str):
        checked = checked.split(',')
    named = {str(c).strip() for c in checked}
    missing = [sid for sid in required if sid not in named]
    if missing:
        # Name the missing ids (as many as fit under the tool cut): a refusal
        # that hides them has locked the agent out of a directory whenever
        # get_standards could not show one.
        listed, rest = [], list(missing)
        while rest and len(', '.join(listed + rest[:1])) <= MAX_UNSEEN_CHARS:
            listed.append(rest.pop(0))
        # Keep the complete IDs that fit before spending any space on titles.
        room = MAX_UNSEEN_CHARS - len(', '.join(listed))
        for i, sid in enumerate(listed):
            title = _one_line(titles[sid] or '')[:min(120, max(0, room - 3))]
            if title:
                listed[i] = f'{sid} ({title})'
                room -= len(title) + 3
        msg = (f'{directory}/ has {len(required)} standard(s) and this proposal names '
               f'{len(required) - len(missing)} of them. Missing: {", ".join(listed)}')
        msg += (f', and {len(rest)} more that do not fit here; {directory}/ has too many '
                'standards to check, so ask the user to archive some' if rest else '')
        msg += (f'. Read each one (get_standards with directory {directory!r}, or read_note), '
                'make the proposal meet it, and list every id in checked_standards')
        raise ProposalError(msg)
    return required


_RESERVED_TAG = re.compile(r'(standard|applies-to\s*:.*)\Z', re.DOTALL)


def _tag_forms(tag) -> list[str]:
    """Every form a tag takes between the agent and the index. `schist add`
    stores it through ingest's `_normalize_tag` and ingest applies the same
    function again when it indexes the note, and one pass does not reach a
    fixed point (it strips leading whitespace and `#` only once), so the
    function is applied until the value stops changing and every stage is
    kept. Each starting point (the raw text, and the text with compatibility
    forms folded and invisible format characters removed) is followed the
    same way."""
    raw = str(tag)
    folded = ''.join(c for c in unicodedata.normalize('NFKC', raw)
                     if unicodedata.category(c) != 'Cf')
    forms = []
    for t in (raw, folded):
        while True:
            forms.append(t)
            nxt = _normalize_tag(t)
            if nxt == t:
                break
            t = nxt
    return forms


def is_reserved_tag(tag) -> bool:
    """True when any form of the tag (see `_tag_forms`), compared ignoring
    case, is `standard` or `applies-to:<directory>`, the tags that make a
    note a standard."""
    return any(_RESERVED_TAG.match(f.strip().casefold()) for f in _tag_forms(tag))


STANDARDS_ONLY_BY_REJECTING = (
    'standards are set only by rejecting a proposal: the tags "standard" and "applies-to:*" '
    'cannot be proposed, and a standard note cannot be edited by a proposal')


def _clean_list(values, what: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [v for v in values.split(',')]
    out = []
    for v in values:
        v = str(v).strip()
        if not v:
            continue
        if ',' in v or markdown_io.contains_line_boundary(v):
            raise ProposalError(f'{what} entry {v!r} may not contain commas or line breaks')
        out.append(v)
    return out


class ProposalStore:
    def __init__(self, vault_path: Path, db_path: Path | None = None):
        self.vault = Path(vault_path)
        self.db_path = Path(db_path) if db_path else self.vault / '.schist' / 'schist.db'
        self.dir = self.vault / '.schist' / 'proposals'
        self._lock = threading.Lock()

    # -- hub ACL --------------------------------------------------------------

    def check_writable(self, relpath: str) -> None:
        """Raise ProposalError for a path the hub would refuse from this
        spoke. The identity is spoke.yaml's, the one `schist sync push` pushes
        as (and `schist doctor` checks). Falls open when there is no spoke
        identity or vault.yaml cannot be read, as the MCP server's check does."""
        try:
            writable.check_writable(self.vault, relpath)
        except writable.WritableError as exc:
            raise ProposalError(str(exc)) from None

    def _writable_dir(self, directory: str) -> bool:
        try:
            self.check_writable(f'{directory}/x.md')
        except ProposalError:
            return False
        return True

    # -- storage ------------------------------------------------------------

    def _path(self, pid: str) -> Path:
        if not pid or not all(c.isalnum() or c == '-' for c in pid):
            raise ProposalError('bad proposal id')
        return self.dir / f'{pid}.json'

    def _private_dir(self) -> None:
        """Make the queue readable by its owner only. A proposal holds the
        proposed note's body and the question and rationale the agent wrote,
        and on a shared host (an HPC login node) the default umask leaves new
        files world-readable (#758). It also tightens a queue an earlier
        version created with the default mode, files included, so a copy of
        .schist/ does not carry them out world-readable."""
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        for f in self.dir.glob('*.json'):
            if stat.S_IMODE(f.lstat().st_mode) & 0o077 and not f.is_symlink():
                os.chmod(f, 0o600)

    def _save(self, p: dict) -> dict:
        self._private_dir()
        # Created 0600 from the start (no window at the umask's mode), under a
        # unique name so two saves never meet at O_EXCL.
        final = self._path(p['id'])
        tmp = final.with_name(f'{final.stem}.{secrets.token_hex(4)}.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(json.dumps(p, indent=2, ensure_ascii=False))
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return p

    def get(self, pid: str) -> dict:
        path = self._path(pid)
        if not path.exists():
            raise ProposalError(f'no proposal {pid}')
        return json.loads(path.read_text(encoding='utf-8'))

    def list(self, status: tuple | None = OPEN) -> list[dict]:
        if isinstance(status, str):
            status = (status,)
        if not self.dir.exists():
            return []
        self._private_dir()
        out = []
        for f in sorted(self.dir.glob('*.json')):
            try:
                p = json.loads(f.read_text(encoding='utf-8'))
            except ValueError:
                continue
            if status is None or p.get('status') in status:
                out.append(p)
        return out

    def _new(self, kind: str, **fields) -> dict:
        now = datetime.now(timezone.utc)
        pid = now.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
        return self._save({'id': pid, 'kind': kind, 'status': PENDING,
                           'created_at': now.isoformat(timespec='seconds'), **fields})

    # -- filing (called by the agent) ---------------------------------------

    def propose_note(self, db, *, title: str, body: str, rationale: str, question: str = '',
                     directory: str = 'notes', tags=None, concepts=None,
                     checked_standards=None) -> dict:
        title = str(title or '').strip()
        body = str(body or '')
        if not title or len(title) > MAX_TITLE or markdown_io.contains_line_boundary(title):
            raise ProposalError(f'title must be one line of 1-{MAX_TITLE} characters')
        if not body.strip() or len(body) > MAX_BODY:
            raise ProposalError(f'body must be 1-{MAX_BODY} characters')
        directory = str(directory or 'notes').strip().strip('/')
        allowed = [d for d in configured_directories(self.vault)
                   if d != 'concepts' and self._writable_dir(d)]
        if directory not in allowed:
            raise ProposalError(f'directory must be one of: {", ".join(allowed) or "(none this spoke can write)"}')
        tags = _clean_list(tags, 'tag')
        if any(is_reserved_tag(t) for t in tags):
            raise ProposalError(STANDARDS_ONLY_BY_REJECTING)
        concepts = _clean_list(concepts, 'concept')
        known = {r[0] for r in db.execute('SELECT slug FROM concepts')}
        unknown = [c for c in concepts if c not in known]
        if unknown:
            raise ProposalError(f'unknown concept slugs: {", ".join(unknown)}; use find_concepts')
        checked = _require_checked(db, directory, checked_standards)
        target = f'{directory}/{date.today().isoformat()}-{markdown_io.slugify(title)}.md'
        fm = [f'title: {title}', 'status: draft']
        if tags:
            fm.append(f'tags: [{", ".join(tags)}]')
        if concepts:
            fm.append(f'concepts: [{", ".join(concepts)}]')
        diff = '\n'.join(f'+{line}' for line in ['---', *fm, '---', '', *body.splitlines()])
        return self._new('create', target=target, title=title, rationale=str(rationale or ''),
                         question=str(question or ''), diff=diff, checked_standards=checked,
                         standards_dir=directory,
                         args={'title': title, 'body': body, 'directory': directory,
                               'tags': tags, 'concepts': concepts})

    def propose_link(self, db, *, source: str, target: str, type: str, rationale: str,
                     question: str = '', context: str | None = None,
                     checked_standards=None) -> dict:
        source, target, link_type = (str(x or '').strip() for x in (source, target, type))
        src = db.execute('SELECT title, tags FROM docs WHERE id = ?', (source,)).fetchone()
        if src is None:
            raise ProposalError(f'source {source!r} is not a note id')
        # `schist link` writes into the source note; a standard's text is a rule
        # the agent reads back, so the agent may not append to one.
        if any(is_reserved_tag(t) for t in json.loads(src[1] or '[]')):
            raise ProposalError(STANDARDS_ONLY_BY_REJECTING)
        tgt = (db.execute('SELECT title FROM docs WHERE id = ?', (target,)).fetchone()
               or db.execute('SELECT title FROM concepts WHERE slug = ?', (target,)).fetchone())
        if tgt is None:
            raise ProposalError(f'target {target!r} is not a note id or concept slug')
        types = commands._connection_types(str(self.vault))
        if link_type not in types:
            raise ProposalError(f'type must be one of: {", ".join(types)}')
        if any(markdown_io.contains_line_boundary(x) for x in (source, target)):
            raise ProposalError('ids may not contain line breaks')
        self.check_writable(source)   # `schist link` rewrites and commits the source note
        standards_dir = _configured_dir(self.vault, source)
        checked = _require_checked(db, standards_dir, checked_standards)
        context = markdown_io.sanitize_context(context) if context else None
        line = f'- {link_type}: {target}' + (f' "{context}"' if context else '')
        return self._new('link', target=source, title=f'{src[0]} —{link_type}→ {tgt[0]}',
                         rationale=str(rationale or ''), question=str(question or ''),
                         diff=f'@@ {source} · ## Connections @@\n+{line}', checked_standards=checked,
                         standards_dir=standards_dir,
                         args={'source': source, 'target': target, 'type': link_type,
                               'context': context})

    # -- review (called by the human, via the page) -------------------------

    def _command(self, p: dict) -> list[str]:
        """Every value goes in `--opt=value` form: argparse reads a separate
        argument that starts with '-' (a title "-draft") as an option and
        fails. A note's body goes on stdin, which `schist add` reads when
        --body is absent: Linux caps one argument at 128 KiB, and MAX_BODY
        characters of UTF-8 can exceed that."""
        a = p['args']
        base = [sys.executable, '-m', 'schist', f'--vault={self.vault}', f'--db={self.db_path}']
        if p['kind'] == 'create':
            cmd = base + ['add', f'--title={a["title"]}', f'--dir={a["directory"]}']
            if a.get('tags'):
                cmd.append(f'--tags={",".join(a["tags"])}')
            if a.get('concepts'):
                cmd.append(f'--concepts={",".join(a["concepts"])}')
            return cmd
        cmd = base + ['link', f'--source={a["source"]}', f'--target={a["target"]}', f'--type={a["type"]}']
        if a.get('context'):
            cmd.append(f'--context={a["context"]}')
        return cmd

    def approve(self, pid: str, runner=subprocess.run) -> dict:
        with self._lock:
            p = self.get(pid)
            if p['status'] not in OPEN:
                raise ProposalError(f'proposal {pid} is already {p["status"]}')
            # vault.yaml may have changed since the proposal was filed.
            self.check_writable(p['args']['source'] if p['kind'] == 'link' else p['target'])
            if p['kind'] == 'create' and not _directory_of(self.vault, p):
                raise ProposalError('the vault no longer configures this proposal directory')
            self._refuse_standard_edit(p)
            p['decided_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            body = p['args']['body'] if p['kind'] == 'create' else ''
            try:
                r = runner(self._command(p), input=body, capture_output=True, text=True,
                           timeout=APPROVE_TIMEOUT_S)
            except OSError as e:
                p['status'] = FAILED
                p['output'] = f'could not run schist: {e}'
                return self._save(p)
            except subprocess.TimeoutExpired:
                # The command was killed, possibly after it committed: say so,
                # since approving again would then write the note twice.
                p['status'] = FAILED
                p['output'] = (f'timed out after {APPROVE_TIMEOUT_S} s (the vault write lock may '
                               'be busy). The change may already be committed: check the vault '
                               'before approving again.')
                return self._save(p)
            p['output'] = ((r.stdout or '') + (r.stderr or ''))[-2000:]
            p['status'] = APPROVED if r.returncode == 0 else FAILED
            if p['kind'] == 'create' and p['status'] == APPROVED:
                # add prints the written path on stdout, including any collision
                # suffix. Commit and hook warnings are reported on stderr.
                written = (r.stdout or '').removesuffix('\n')
                if written and '\n' not in written:
                    p['written_path'] = written
            return self._save(p)

    def _refuse_standard_edit(self, p: dict) -> None:
        """The filing checks again at approval, for a proposal filed before
        they existed or edited on disk since: a note may not carry the standard
        tags, and a link may not write into a standard."""
        if p['kind'] == 'create':
            tags = p['args'].get('tags') or []
            if any(is_reserved_tag(t) for t in tags):
                raise ProposalError(STANDARDS_ONLY_BY_REJECTING + '; reject this proposal')
            return
        try:
            uri = Path(self.db_path).absolute().as_uri() + '?mode=ro'
            db = sqlite3.connect(uri, uri=True)
            try:
                row = db.execute('SELECT tags FROM docs WHERE id = ?', (p['args']['source'],)).fetchone()
            finally:
                db.close()
        except sqlite3.Error as e:
            raise ProposalError(f'cannot check whether the source note is a standard: {e}') from None
        tags = json.loads(row[0] or '[]') if row else []
        # The index can lag the file (a note edited or pulled since the last
        # reindex), and the file is what `schist link` rewrites, so read it too.
        # `schist link` takes the vault write lock in its own process, which this
        # one cannot hold across it without deadlocking, so the read is made
        # immediately before the command starts.
        tags += self._file_tags(p['args']['source'])
        if any(is_reserved_tag(t) for t in tags):
            raise ProposalError(STANDARDS_ONLY_BY_REJECTING + '; reject this proposal')

    def _file_tags(self, note_id: str) -> list:
        """The tags in a note's frontmatter, parsed as ingest parses it. A note
        that is missing has none (the command will report it); one that cannot
        be read or parsed is refused, since it cannot be shown not to be a standard."""
        root = Path(self.vault).resolve()
        path = (root / note_id).resolve()
        if root not in path.parents:
            raise ProposalError(f'source {note_id!r} is outside the vault')
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as e:
            raise ProposalError(f'cannot read the source note to check it: {e}') from None
        try:
            tags = frontmatter.loads(patch_frontmatter_flow_hashtags(text)).metadata.get('tags')
        except Exception as e:  # noqa: BLE001 - any parser error means it cannot be checked
            raise ProposalError(f'cannot parse the source note to check it: {e}') from None
        if tags is None:
            return []
        if isinstance(tags, (str, int, float)):
            return [str(tags)]
        if isinstance(tags, dict):
            return [str(k) for k in tags]
        return [str(t) for t in tags]

    def reject(self, pid: str, reason: str = '', save_standard: bool = False,
               runner=subprocess.run) -> dict:
        """Reject a proposal, keeping the reviewer's reason. With save_standard
        the reason is also written as a standard note first; if that write
        fails the proposal stays open, so the reviewer can try again."""
        reason = str(reason or '').strip()
        if len(reason) > MAX_REASON:
            raise ProposalError(f'reason must be at most {MAX_REASON} characters')
        if save_standard and not reason:
            raise ProposalError('a standard needs a reason')
        with self._lock:
            p = self.get(pid)
            if p['status'] not in OPEN:
                raise ProposalError(f'proposal {pid} is already {p["status"]}')
            if reason:
                p['reason'] = reason
            if save_standard:
                outcome = self._save_standard(p, reason, runner)
                p['standard'] = outcome
                if outcome['status'] == 'failed':
                    return self._save(p)
            p['status'] = REJECTED
            p['decided_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            return self._save(p)

    def _save_standard(self, p: dict, reason: str, runner) -> dict:
        # The directory recorded at filing is the one the reviewer was shown.
        # A proposal without one (filed by an earlier build) or in none, or one
        # whose directory is no longer what the vault configures, saves nothing.
        if 'standards_dir' not in p:
            raise ProposalError('this proposal does not record its directory, so it cannot '
                                'carry a standard; reject it without saving one')
        directory = p['standards_dir']
        if not directory:
            raise ProposalError('this proposal is in no configured directory, so it cannot '
                                'carry a standard; reject it without saving one')
        if _directory_of(self.vault, p) != directory:
            raise ProposalError(f'the vault no longer configures {directory}/ for this proposal; '
                                'reject it without saving a standard')
        self.check_writable(f'{directory}/x.md')   # vault.yaml may have changed since filing
        # The prefix keeps the slug non-empty (a reason in CJK or punctuation
        # only) and the title from starting with a date, both of which
        # `schist add` refuses.
        title = 'Standard: ' + _one_line(reason.splitlines()[0])
        if len(title) > MAX_TITLE:
            title = title[:MAX_TITLE - 1].rstrip() + '…'
        when = datetime.now(timezone.utc).date().isoformat()
        # Only the reviewer's words and the proposal's target path: its title
        # comes from note titles other writers control, so it stays out of a
        # note committed under the reviewer's name.
        kind = 'new note' if p['kind'] == 'create' else 'new connection in'
        body = (f'{reason}\n\nRecorded on {when} when the reviewer rejected a proposal '
                f'({kind} {_one_line(p["target"])}).\n')
        cmd = [sys.executable, '-m', 'schist', f'--vault={self.vault}', f'--db={self.db_path}',
               'add', f'--title={title}', f'--dir={directory}',
               f'--tags={STANDARD_TAG},{applies_tag(directory)}']
        try:
            r = runner(cmd, input=body, capture_output=True, text=True, timeout=APPROVE_TIMEOUT_S)
        except OSError as e:
            return {'status': 'failed', 'output': f'could not run schist: {e}'}
        except subprocess.TimeoutExpired:
            # Possibly committed: reject anyway rather than invite a duplicate.
            return {'status': 'unknown', 'directory': directory, 'output': (
                f'timed out after {APPROVE_TIMEOUT_S} s; the standard may already be '
                f'committed in {directory}/. Check before adding it again.')}
        output = ((r.stdout or '') + (r.stderr or ''))[-2000:]
        if r.returncode:
            return {'status': 'failed', 'directory': directory, 'output': output}
        path = (r.stdout or '').removesuffix('\n')
        # add can write the file even when a commit fails. Never retry an
        # indeterminate write automatically: a second add allocates a new name.
        if (Path(path).parent.as_posix() == directory and path.endswith('.md')
                and not path.startswith('/') and '\n' not in path
                and 'Warning: git commit failed:' not in (r.stderr or '')):
            try:
                landed = subprocess.run(['git', 'cat-file', '-e', f'HEAD:{path}'],
                                        cwd=self.vault, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                landed = None
            if landed is not None and landed.returncode == 0:
                return {'status': 'saved', 'directory': directory, 'path': path, 'output': output}
        return {'status': 'unknown', 'directory': directory,
                'output': f'could not confirm the standard was committed. Check the vault. {output}'}
