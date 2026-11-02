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
import stat
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .. import commands, markdown_io
from ..acl import ACLError, parse_vault_yaml
from ..pre_receive import derive_scope
from ..spoke_config import is_spoke, load_spoke_config

MAX_TITLE = 200
MAX_BODY = 50_000
MAX_REASON = 2_000
MAX_STANDARD_CHARS = 2_000   # of each standard's body, as shown to the agent
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
    notes tagged both `standard` and `applies-to:<directory>`, not archived,
    oldest id first."""
    rows = db.execute(
        "SELECT id, title, body FROM docs d"
        " WHERE EXISTS (SELECT 1 FROM json_each(d.tags) WHERE value = ?)"
        "   AND EXISTS (SELECT 1 FROM json_each(d.tags) WHERE value = ?)"
        "   AND COALESCE(d.status, '') != 'archived'"
        " ORDER BY d.id",
        (STANDARD_TAG, applies_tag(directory))).fetchall()
    return [{'id': r[0], 'title': r[1], 'body': (r[2] or '')[:MAX_STANDARD_CHARS]}
            for r in rows]


def _directory_of(p: dict) -> str:
    """The directory a proposal writes into: the new note's, or for a link the
    source note's, which `schist link` rewrites."""
    if p['kind'] == 'create':
        return p['args']['directory']
    return p['args']['source'].split('/', 1)[0]


def _require_checked(db, directory: str, checked) -> list[str]:
    required = [s['id'] for s in standards_for(db, directory)]
    named = set(_clean_list(checked, 'checked_standards entry'))
    missing = [sid for sid in required if sid not in named]
    if missing:
        raise ProposalError(
            f'{directory}/ has standards this proposal was not checked against: '
            f'{", ".join(missing)}. Call get_standards with directory {directory!r}, make the '
            'proposal meet each one, and list every id in checked_standards')
    return required


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
        acl_path = self.vault / 'vault.yaml'
        if not is_spoke(str(self.vault)) or not acl_path.is_file():
            return
        try:
            identity = load_spoke_config(str(self.vault)).identity
            acl = parse_vault_yaml(acl_path)
        except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError, ACLError):
            return
        scope = derive_scope(relpath)
        if scope == '':   # a root-level file: only wildcard writers (pre_receive)
            entry = acl.access.get(identity)
            ok = entry is not None and '*' in entry.write
        else:
            ok = acl.can_write(identity, scope)
        if not ok:
            raise ProposalError(f'spoke {identity!r} may not write {scope or "the vault root"!r} '
                                'under vault.yaml, so the hub would refuse this commit')

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
        allowed = [d for d in commands._directories(str(self.vault))
                   if d != 'concepts' and self._writable_dir(d)]
        if directory not in allowed:
            raise ProposalError(f'directory must be one of: {", ".join(allowed) or "(none this spoke can write)"}')
        tags = _clean_list(tags, 'tag')
        concepts = _clean_list(concepts, 'concept')
        known = {r[0] for r in db.execute('SELECT slug FROM concepts')}
        unknown = [c for c in concepts if c not in known]
        if unknown:
            raise ProposalError(f'unknown concept slugs: {", ".join(unknown)}; use find_concepts')
        checked = _require_checked(db, directory, checked_standards)
        target =f'{directory}/{date.today().isoformat()}-{markdown_io.slugify(title)}.md'
        fm = [f'title: {title}', 'status: draft']
        if tags:
            fm.append(f'tags: [{", ".join(tags)}]')
        if concepts:
            fm.append(f'concepts: [{", ".join(concepts)}]')
        diff = '\n'.join(f'+{line}' for line in ['---', *fm, '---', '', *body.splitlines()])
        return self._new('create', target=target, title=title, rationale=str(rationale or ''),
                         question=str(question or ''), diff=diff, checked_standards=checked,
                         args={'title': title, 'body': body, 'directory': directory,
                               'tags': tags, 'concepts': concepts})

    def propose_link(self, db, *, source: str, target: str, type: str, rationale: str,
                     question: str = '', context: str | None = None,
                     checked_standards=None) -> dict:
        source, target, link_type = (str(x or '').strip() for x in (source, target, type))
        src = db.execute('SELECT title FROM docs WHERE id = ?', (source,)).fetchone()
        if src is None:
            raise ProposalError(f'source {source!r} is not a note id')
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
        checked = _require_checked(db, source.split('/', 1)[0], checked_standards)
        context = markdown_io.sanitize_context(context) if context else None
        line = f'- {link_type}: {target}' + (f' "{context}"' if context else '')
        return self._new('link', target=source, title=f'{src[0]} —{link_type}→ {tgt[0]}',
                         rationale=str(rationale or ''), question=str(question or ''),
                         diff=f'@@ {source} · ## Connections @@\n+{line}', checked_standards=checked,
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
            return self._save(p)

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
        directory = _directory_of(p)
        self.check_writable(f'{directory}/x.md')   # vault.yaml may have changed since filing
        first = reason.splitlines()[0].strip()
        title = first if len(first) <= MAX_TITLE else first[:MAX_TITLE - 1].rstrip() + '…'
        when = datetime.now(timezone.utc).date().isoformat()
        body = (f'{reason}\n\nRecorded on {when} when the reviewer rejected a proposal: '
                f'"{p["title"]}" ({p["kind"]}, {p["target"]}).\n')
        cmd = [sys.executable, '-m', 'schist', f'--vault={self.vault}', f'--db={self.db_path}',
               'add', f'--title={title}', f'--dir={directory}',
               f'--tags={STANDARD_TAG},{applies_tag(directory)}']
        try:
            r = runner(cmd, input=body, capture_output=True, text=True, timeout=APPROVE_TIMEOUT_S)
        except OSError as e:
            return {'status': 'failed', 'output': f'could not run schist: {e}'}
        except subprocess.TimeoutExpired:
            # Possibly committed: reject anyway rather than invite a duplicate.
            return {'status': 'unknown', 'output': (
                f'timed out after {APPROVE_TIMEOUT_S} s; the standard may already be '
                f'committed in {directory}/. Check before adding it again.')}
        output = ((r.stdout or '') + (r.stderr or ''))[-2000:]
        return {'status': 'saved' if r.returncode == 0 else 'failed',
                'directory': directory, 'output': output}
