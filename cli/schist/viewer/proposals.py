"""Proposal queue for the viewer agent: the vault's equivalent of a PR.

The agent files proposals (a new note, or a new connection); each is a JSON
file under <vault>/.schist/proposals/, which is gitignored, so filing one
changes nothing in the vault. Approving a proposal runs the ordinary
`schist add` / `schist link` command, which validates it, takes the vault
write lock and commits; the spoke's usual sync then pushes it. Rejecting one
only marks it.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .. import commands, markdown_io

MAX_TITLE = 200
MAX_BODY = 50_000
PENDING, APPROVED, REJECTED, FAILED = 'pending', 'approved', 'rejected', 'failed'


class ProposalError(Exception):
    pass


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

    # -- storage ------------------------------------------------------------

    def _path(self, pid: str) -> Path:
        if not pid or not all(c.isalnum() or c == '-' for c in pid):
            raise ProposalError('bad proposal id')
        return self.dir / f'{pid}.json'

    def _save(self, p: dict) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(p['id']).with_suffix('.tmp')
        tmp.write_text(json.dumps(p, indent=2, ensure_ascii=False))
        os.replace(tmp, self._path(p['id']))
        return p

    def get(self, pid: str) -> dict:
        path = self._path(pid)
        if not path.exists():
            raise ProposalError(f'no proposal {pid}')
        return json.loads(path.read_text())

    def list(self, status: str | None = PENDING) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for f in sorted(self.dir.glob('*.json')):
            try:
                p = json.loads(f.read_text())
            except ValueError:
                continue
            if status is None or p.get('status') == status:
                out.append(p)
        return out

    def _new(self, kind: str, **fields) -> dict:
        now = datetime.now(timezone.utc)
        pid = now.strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
        return self._save({'id': pid, 'kind': kind, 'status': PENDING,
                           'created_at': now.isoformat(timespec='seconds'), **fields})

    # -- filing (called by the agent) ---------------------------------------

    def propose_note(self, db, *, title: str, body: str, rationale: str, question: str = '',
                     directory: str = 'notes', tags=None, concepts=None) -> dict:
        title = str(title or '').strip()
        body = str(body or '')
        if not title or len(title) > MAX_TITLE or markdown_io.contains_line_boundary(title):
            raise ProposalError(f'title must be one line of 1-{MAX_TITLE} characters')
        if not body.strip() or len(body) > MAX_BODY:
            raise ProposalError(f'body must be 1-{MAX_BODY} characters')
        directory = str(directory or 'notes').strip().strip('/')
        allowed = [d for d in commands._directories(str(self.vault)) if d != 'concepts']
        if directory not in allowed:
            raise ProposalError(f'directory must be one of: {", ".join(allowed)}')
        tags = _clean_list(tags, 'tag')
        concepts = _clean_list(concepts, 'concept')
        known = {r[0] for r in db.execute('SELECT slug FROM concepts')}
        unknown = [c for c in concepts if c not in known]
        if unknown:
            raise ProposalError(f'unknown concept slugs: {", ".join(unknown)}; use find_concepts')
        target = f'{directory}/{date.today().isoformat()}-{markdown_io.slugify(title)}.md'
        fm = [f'title: {title}', 'status: draft']
        if tags:
            fm.append(f'tags: [{", ".join(tags)}]')
        if concepts:
            fm.append(f'concepts: [{", ".join(concepts)}]')
        diff = '\n'.join(f'+{line}' for line in ['---', *fm, '---', '', *body.splitlines()])
        return self._new('create', target=target, title=title, rationale=str(rationale or ''),
                         question=str(question or ''), diff=diff,
                         args={'title': title, 'body': body, 'directory': directory,
                               'tags': tags, 'concepts': concepts})

    def propose_link(self, db, *, source: str, target: str, type: str, rationale: str,
                     question: str = '', context: str | None = None) -> dict:
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
        context = markdown_io.sanitize_context(context) if context else None
        line = f'- {link_type}: {target}' + (f' "{context}"' if context else '')
        return self._new('link', target=source, title=f'{src[0]} —{link_type}→ {tgt[0]}',
                         rationale=str(rationale or ''), question=str(question or ''),
                         diff=f'@@ {source} · ## Connections @@\n+{line}',
                         args={'source': source, 'target': target, 'type': link_type,
                               'context': context})

    # -- review (called by the human, via the page) -------------------------

    def _command(self, p: dict) -> list[str]:
        a = p['args']
        base = [sys.executable, '-m', 'schist', '--vault', str(self.vault), '--db', str(self.db_path)]
        if p['kind'] == 'create':
            cmd = base + ['add', '--title', a['title'], '--body', a['body'], '--dir', a['directory']]
            if a.get('tags'):
                cmd += ['--tags', ','.join(a['tags'])]
            if a.get('concepts'):
                cmd += ['--concepts', ','.join(a['concepts'])]
            return cmd
        cmd = base + ['link', '--source', a['source'], '--target', a['target'], '--type', a['type']]
        if a.get('context'):
            cmd += ['--context', a['context']]
        return cmd

    def approve(self, pid: str, runner=subprocess.run) -> dict:
        with self._lock:
            p = self.get(pid)
            if p['status'] != PENDING:
                raise ProposalError(f'proposal {pid} is already {p["status"]}')
            r = runner(self._command(p), capture_output=True, text=True, timeout=120,
                       stdin=subprocess.DEVNULL)
            p['decided_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            p['output'] = ((r.stdout or '') + (r.stderr or ''))[-2000:]
            p['status'] = APPROVED if r.returncode == 0 else FAILED
            return self._save(p)

    def reject(self, pid: str) -> dict:
        with self._lock:
            p = self.get(pid)
            if p['status'] != PENDING:
                raise ProposalError(f'proposal {pid} is already {p["status"]}')
            p['status'] = REJECTED
            p['decided_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            return self._save(p)
