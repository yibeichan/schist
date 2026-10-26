# SPDX-FileCopyrightText: 2026 Yibei Chen
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for `schist serve`: the request guards, rebuild, the agent's tools and
loop (against a fake Ollama), and the proposal queue end to end."""

from __future__ import annotations

import http.client
import io
import json
import os
import socket
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from schist import sqlite_query
from schist.viewer import agent as agent_mod
from schist.viewer import proposals as proposals_mod
from schist.viewer import serve as serve_mod


def _git(vault: Path, *args: str) -> str:
    return subprocess.run(['git', '-C', str(vault), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def vault(tmp_path) -> Path:
    v = tmp_path / 'vault'
    (v / 'notes').mkdir(parents=True)
    (v / 'concepts').mkdir()
    (v / 'schist.yaml').write_text(
        'connection_types: [related, supports]\n'
        'directories:\n  notes: notes/\n  concepts: concepts/\n', encoding='utf-8')
    (v / '.gitignore').write_text('.schist/\n', encoding='utf-8')
    (v / 'notes' / 'lock.md').write_text(
        '---\ntitle: Vault write lock\nstatus: draft\nconcepts: [locking]\n---\n\n'
        'The vault write lock serializes the CLI and every MCP server.\n\n'
        '## Connections\n- related: notes/sync.md\n', encoding='utf-8')
    (v / 'notes' / 'sync.md').write_text(
        '---\ntitle: Spoke sync\nstatus: draft\n---\n\nSync pulls from the hub.\n',
        encoding='utf-8')
    (v / 'concepts' / 'locking.md').write_text(
        '---\ntitle: Locking\n---\n\nMutual exclusion.\n', encoding='utf-8')
    _git(v, 'init', '-q', '-b', 'main')
    # CI has no global identity, and the suite pins global config to empty.
    _git(v, 'config', 'user.email', 't@example.com')
    _git(v, 'config', 'user.name', 't')
    _git(v, 'add', '-A')
    _git(v, 'commit', '-q', '-m', 'seed')
    sqlite_query.get_db(str(v), str(v / '.schist' / 'schist.db')).close()
    return v


@pytest.fixture
def db_path(vault) -> Path:
    return vault / '.schist' / 'schist.db'


@pytest.fixture
def ro(db_path):
    db = agent_mod.connect_ro(db_path)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

class TestReadTools:
    def test_search_all_words_then_any_word(self, ro):
        hit = agent_mod.search_notes(ro, 'write lock')
        assert hit['match'] == 'all words'
        assert [r['id'] for r in hit['results']] == ['notes/lock.md']
        # No note has both words: falls back to OR instead of an empty answer.
        fallback = agent_mod.search_notes(ro, 'hub serializes')
        assert fallback['match'] == 'any word'
        assert {r['id'] for r in fallback['results']} == {'notes/lock.md', 'notes/sync.md'}

    @pytest.mark.parametrize('query', ['notes/lock.md', 'notes/lock', '[[notes/lock.md]]',
                                       '  notes/lock.md  '])
    def test_search_finds_a_note_by_its_id(self, ro, query):
        # A word search on an id matches its path words ('notes', 'lock', 'md'),
        # which can rank other notes first; an id is looked up exactly.
        hit = agent_mod.search_notes(ro, query)
        assert hit['match'] == 'id'
        assert [r['id'] for r in hit['results']] == ['notes/lock.md']
        assert hit['results'][0]['title'] == 'Vault write lock'

    def test_search_on_an_unknown_id_falls_back_to_words(self, ro):
        assert agent_mod.search_notes(ro, 'notes/nope.md')['match'] != 'id'
        # Words with a slash are not ids unless one matches.
        assert agent_mod.search_notes(ro, 'lock and/or sync')['match'] != 'id'

    def test_cited_titles(self, ro):
        out = agent_mod.cited_titles(
            ro, 'A [[notes/lock.md]], B [[ locking ]], C [[notes/made-up.md]], again [[notes/lock.md]].')
        assert out == {'notes/lock.md': 'Vault write lock', 'locking': 'Locking',
                       'notes/made-up.md': None}
        assert agent_mod.cited_titles(ro, 'no citations') == {}

    def test_search_ignores_fts_syntax(self, ro):
        assert agent_mod.search_notes(ro, 'lock" OR NEAR(') ['results']

    def test_read_note_and_unknown_id(self, ro):
        note = agent_mod.read_note(ro, 'notes/lock.md')
        assert note['title'] == 'Vault write lock' and note['concepts'] == ['locking']
        assert 'error' in agent_mod.read_note(ro, 'notes/nope.md')

    def test_related_both_directions(self, ro):
        out = agent_mod.related(ro, 'notes/sync.md')['connections']
        assert {'direction': 'in', 'type': 'related', 'id': 'notes/lock.md',
                'title': 'Vault write lock', 'context': None} in out

    def test_db_is_read_only(self, ro):
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM docs")

    @pytest.mark.parametrize('name', ['a#b', 'a?b', 'a%20b', 'a b'])
    def test_read_only_index_in_a_path_with_uri_characters(self, db_path, tmp_path, name):
        import shutil
        import sqlite3
        d = tmp_path / name / '.schist'
        d.mkdir(parents=True)
        shutil.copy(db_path, d / 'schist.db')
        db = agent_mod.connect_ro(d / 'schist.db')
        try:
            assert db.execute('SELECT count(*) FROM docs').fetchone()[0] > 0
            with pytest.raises(sqlite3.OperationalError):
                db.execute('DELETE FROM docs')
        finally:
            db.close()
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(['vault', name])


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

class TestProposals:
    def test_note_validation(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        with pytest.raises(proposals_mod.ProposalError, match='directory'):
            store.propose_note(ro, title='T', body='b', rationale='r', directory='concepts')
        with pytest.raises(proposals_mod.ProposalError, match='unknown concept'):
            store.propose_note(ro, title='T', body='b', rationale='r', concepts=['made-up'])
        with pytest.raises(proposals_mod.ProposalError, match='one line'):
            store.propose_note(ro, title='a\nb', body='b', rationale='r')

    def test_link_validation(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        with pytest.raises(proposals_mod.ProposalError, match='source'):
            store.propose_link(ro, source='notes/x.md', target='locking', type='related', rationale='r')
        with pytest.raises(proposals_mod.ProposalError, match='type'):
            store.propose_link(ro, source='notes/sync.md', target='locking', type='made-up', rationale='r')

    def test_proposal_files_are_private_to_the_owner(self, vault, ro):
        """#758: a proposal holds note text and the agent's question, so
        neither the queue nor its files may be readable by other users, even
        under a permissive umask."""
        old = os.umask(0o022)
        try:
            store = proposals_mod.ProposalStore(vault)
            p = store.propose_note(ro, title='New', body='secret body', rationale='r')
        finally:
            os.umask(old)
        assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(store._path(p['id']).stat().st_mode) == 0o600
        assert list(store.dir.glob('*.tmp')) == []

    def test_a_queue_created_world_readable_is_tightened(self, vault, ro):
        """A directory an earlier version made with the default mode is
        closed on the next save, which covers the old files inside it."""
        store = proposals_mod.ProposalStore(vault)
        store.dir.mkdir(parents=True)
        os.chmod(store.dir, 0o755)
        store.propose_note(ro, title='New', body='b', rationale='r')
        assert stat.S_IMODE(store.dir.stat().st_mode) == 0o700

    def test_filing_does_not_touch_the_vault(self, vault, ro):
        head = _git(vault, 'rev-parse', 'HEAD')
        proposals_mod.ProposalStore(vault).propose_note(ro, title='New', body='b', rationale='r')
        assert _git(vault, 'rev-parse', 'HEAD') == head
        assert _git(vault, 'status', '--porcelain') == ''

    def test_approve_note_commits_through_schist_add(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='Agent note', body='From the agent.', rationale='r',
                               concepts=['locking'])
        done = store.approve(p['id'])
        assert done['status'] == proposals_mod.APPROVED, done['output']
        written = list((vault / 'notes').glob('*agent-note.md'))
        assert len(written) == 1 and 'From the agent.' in written[0].read_text()
        assert _git(vault, 'status', '--porcelain') == ''          # committed
        assert store.list() == []                                   # no longer pending
        with pytest.raises(proposals_mod.ProposalError, match='already approved'):
            store.approve(p['id'])

    def test_approve_link_commits_through_schist_link(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_link(ro, source='notes/sync.md', target='locking', type='supports',
                               context='sync takes the lock', rationale='r')
        assert store.approve(p['id'])['status'] == proposals_mod.APPROVED
        assert '- supports: locking' in (vault / 'notes' / 'sync.md').read_text()

    def test_reject_writes_nothing(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        head = _git(vault, 'rev-parse', 'HEAD')
        p = store.propose_note(ro, title='No', body='b', rationale='r')
        assert store.reject(p['id'])['status'] == proposals_mod.REJECTED
        assert _git(vault, 'rev-parse', 'HEAD') == head

    def test_values_starting_with_a_dash_are_not_options(self, vault, ro):
        # Passed as separate arguments, argparse read "-draft" and "--" as
        # options and the approve failed.
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='-draft', body='--', rationale='r')
        done = store.approve(p['id'])
        assert done['status'] == proposals_mod.APPROVED, done['output']
        written = list((vault / 'notes').glob('*draft.md'))
        assert len(written) == 1 and '--' in written[0].read_text()

    def test_failed_proposal_stays_listed_and_can_be_retried(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='Retry me', body='b', rationale='r')
        failed = store.approve(p['id'], runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'boom'))
        assert failed['status'] == proposals_mod.FAILED and 'boom' in failed['output']
        assert [x['id'] for x in store.list()] == [p['id']]
        assert store.approve(p['id'])['status'] == proposals_mod.APPROVED
        assert store.list() == []

    def test_approve_timeout_is_recorded_not_raised(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='Slow', body='b', rationale='r')

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw['timeout'])
        done = store.approve(p['id'], runner=hang)
        assert done['status'] == proposals_mod.FAILED
        assert 'check the vault before approving again' in done['output']

    def test_bad_ids_cannot_escape_the_queue(self, vault):
        store = proposals_mod.ProposalStore(vault)
        for pid in ('../schist', 'a/b', ''):
            with pytest.raises(proposals_mod.ProposalError):
                store.get(pid)


# ---------------------------------------------------------------------------
# Agent loop against a fake Ollama
# ---------------------------------------------------------------------------

class FakeOllama:
    """Replays scripted /api/chat rounds; records what the agent sent."""

    def __init__(self, rounds, models=('qwen3.5:9b',), capabilities=('completion', 'tools')):
        self.rounds = list(rounds)
        self.models = models
        self.capabilities = capabilities   # None: an Ollama whose /api/show has none
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith('/api/tags'):
            return io.BytesIO(json.dumps({'models': [{'name': m} for m in self.models]}).encode())
        if url.endswith('/api/show'):
            info = {} if self.capabilities is None else {'capabilities': list(self.capabilities)}
            return io.BytesIO(json.dumps(info).encode())
        self.requests.append(json.loads(req.data))
        chunks = self.rounds.pop(0)
        return io.BytesIO(b''.join(json.dumps(c).encode() + b'\n' for c in chunks))


def _call(name, **args):
    return [{'message': {'role': 'assistant', 'content': '',
                         'tool_calls': [{'function': {'name': name, 'arguments': args}}]}}]


def _say(*parts):
    return [{'message': {'role': 'assistant', 'content': p}} for p in parts] + [{'done': True}]


class TestAgentLoop:
    def _agent(self, vault, db_path, fake):
        store = proposals_mod.ProposalStore(vault)
        return agent_mod.Agent(db_path, store, opener=fake), store

    def test_tools_then_streamed_answer(self, vault, db_path):
        fake = FakeOllama([_call('search_notes', query='write lock'),
                           _call('read_note', id='notes/lock.md'),
                           _say('It serializes writes ', '[[notes/lock.md]].')])
        agent, _ = self._agent(vault, db_path, fake)
        events = list(agent.run([{'role': 'user', 'content': 'what is the write lock?'}]))
        kinds = [e['type'] for e in events]
        assert kinds[0] == 'step' and kinds[-1] == 'done'
        assert ''.join(e['text'] for e in events if e['type'] == 'token') == \
            'It serializes writes [[notes/lock.md]].'
        assert any('reading Vault write lock' in e.get('text', '') for e in events)
        # Before done, the page is told which cited ids the index knows.
        assert events[-2] == {'type': 'cited', 'ids': {'notes/lock.md': 'Vault write lock'}}
        # The tool result went back to the model as a tool message.
        last = fake.requests[-1]['messages']
        assert last[-1]['role'] == 'tool' and 'serializes the CLI' in last[-1]['content']
        assert fake.requests[0]['think'] is False and fake.requests[0]['tools']

    def test_proposal_is_filed_not_written(self, vault, db_path):
        fake = FakeOllama([_call('propose_note', title='Lock summary', body='x', rationale='asked'),
                           _say('Filed for your review.')])
        agent, store = self._agent(vault, db_path, fake)
        head = _git(vault, 'rev-parse', 'HEAD')
        events = list(agent.run([{'role': 'user', 'content': 'save a summary'}]))
        prop = [e for e in events if e['type'] == 'proposal']
        assert len(prop) == 1 and store.list()[0]['id'] == prop[0]['id']
        assert store.list()[0]['question'] == 'save a summary'
        assert _git(vault, 'rev-parse', 'HEAD') == head

    def test_text_before_a_tool_call_is_marked_interim(self, vault, db_path):
        plan = [{'message': {'role': 'assistant', 'content': 'Let me search.'}}] + \
            _call('search_notes', query='lock')
        fake = FakeOllama([plan, _say('Answer.')])
        agent, _ = self._agent(vault, db_path, fake)
        kinds = [e['type'] for e in agent.run([{'role': 'user', 'content': 'q'}])]
        first_tool = kinds.index('step', 1)
        assert kinds[first_tool - 1] == 'interim' and kinds.count('interim') == 1

    def test_round_limit(self, vault, db_path):
        fake = FakeOllama([_call('search_notes', query='x')] * agent_mod.MAX_ROUNDS)
        agent, _ = self._agent(vault, db_path, fake)
        events = list(agent.run([{'role': 'user', 'content': 'loop'}]))
        assert events[-1]['type'] == 'error' and 'tool rounds' in events[-1]['message']

    def test_unavailable_model(self, vault, db_path):
        agent, _ = self._agent(vault, db_path, FakeOllama([], models=('other:1b',)))
        events = list(agent.run([{'role': 'user', 'content': 'hi'}]))
        assert events == [{'type': 'error', 'message': 'model not pulled: run `ollama pull qwen3.5:9b`'}]

    def test_untagged_model_matches_latest(self, vault, db_path):
        store = proposals_mod.ProposalStore(vault)
        fake = FakeOllama([], models=('llama3:latest', 'reg.example:5000/ns/m:latest'))
        assert agent_mod.Agent(db_path, store, model='llama3', opener=fake).status()['available']
        assert agent_mod.Agent(db_path, store, model='reg.example:5000/ns/m', opener=fake).status()['available']
        assert not agent_mod.Agent(db_path, store, model='llama3:8b', opener=fake).status()['available']

    def test_bad_tool_arguments_go_back_to_the_model(self, vault, db_path):
        bad_limit = _call('search_notes', query='lock', limit='five')
        not_a_dict = [{'message': {'role': 'assistant', 'content': '',
                                   'tool_calls': [{'function': {'name': 'read_note', 'arguments': '[1]'}}]}}]
        broken = _call('propose_note', title='T', body='b', rationale='r', tags=5)  # TypeError
        fake = FakeOllama([bad_limit, not_a_dict, broken, _say('Answer.')])
        agent, _ = self._agent(vault, db_path, fake)
        events = list(agent.run([{'role': 'user', 'content': 'q'}]))
        assert events[-1] == {'type': 'done'}
        tool_msgs = [m for m in fake.requests[-1]['messages'] if m['role'] == 'tool']
        assert len(tool_msgs) == 3 and 'propose_note failed' in tool_msgs[2]['content']

    def test_ollama_down(self, vault, db_path):
        def down(req, timeout=None):
            raise urllib.error.URLError('refused')
        agent, _ = self._agent(vault, db_path, down)
        assert agent.status()['available'] is False


# ---------------------------------------------------------------------------
# HTTP server guards
# ---------------------------------------------------------------------------

@pytest.fixture
def server(vault, db_path):
    store = proposals_mod.ProposalStore(vault)
    fake = FakeOllama([_say('hello ', '[[notes/lock.md]]')])
    agent = agent_mod.Agent(db_path, store, opener=fake)
    srv = serve_mod.ViewerServer(vault, db_path, vault / '.schist' / 'data', '127.0.0.1', 0,
                                 agent, store, TOKEN)
    port = srv.server_address[1]
    with srv.build_lock:
        assert srv.rebuild()[0]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f'http://127.0.0.1:{port}'
    srv.shutdown()
    srv.server_close()


TOKEN = 'test-token-' + 'x' * 32


def _req(url, method='GET', headers=None, body=None, token=TOKEN):
    """A request carrying the access token unless token=None."""
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(headers or {})
    if token is not None:
        headers.setdefault('X-Schist-Token', token)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestServer:
    def test_site_and_status(self, server):
        srv, base = server
        assert _req(base + '/')[0] == 200
        code, body = _req(base + '/api/status')
        st = json.loads(body)
        assert code == 200 and st['built_head'] == st['vault_head']

    def test_bad_host_rejected(self, server):
        _, base = server
        assert _req(base + '/', headers={'Host': 'evil.example:8420'})[0] == 403

    def test_every_request_needs_the_token(self, server, ro):
        # Binding to 127.0.0.1 does not keep out other users of the host.
        srv, base = server
        p = srv.store.propose_note(ro, title='Not yours', body='b', rationale='r')
        agent_h = {'X-Schist-Agent': '1'}
        for token in (None, 'wrong'):
            assert _req(base + '/', token=token)[0] == 401
            assert _req(base + '/graph.json', token=token)[0] == 401
            assert _req(base + '/api/proposals', token=token)[0] == 401
            assert _req(base + f'/api/proposals/{p["id"]}/approve', 'POST', agent_h, token=token)[0] == 401
        assert srv.store.get(p['id'])['status'] == proposals_mod.PENDING

    def test_token_url_sets_a_cookie_the_page_then_uses(self, server):
        srv, base = server
        host, port = base.removeprefix('http://').split(':')
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.request('GET', '/?token=wrong')
        r = conn.getresponse(); r.read()
        assert r.status == 401
        conn.request('GET', f'/?token={TOKEN}')
        r = conn.getresponse(); r.read()
        cookie = r.getheader('Set-Cookie')
        assert r.status == 303 and r.getheader('Location') == '/'
        assert cookie.startswith(f'schist_token_{port}={TOKEN};')
        assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie
        jar = cookie.split(';')[0]
        assert _req(base + '/api/status', headers={'Cookie': jar}, token=None)[0] == 200
        assert _req(base + '/api/status', headers={'Cookie': f'schist_token_{port}=nope'}, token=None)[0] == 401

    def test_rebinding_host_cannot_post(self, server, ro):
        # A page that rebinds its own name to 127.0.0.1 is same-origin, so it
        # can send the custom header; only the Host check stops it.
        srv, base = server
        p = srv.store.propose_note(ro, title='Rebound', body='b', rationale='r')
        evil = {'Host': 'evil.example:' + base.rsplit(':', 1)[1], 'X-Schist-Agent': '1'}
        assert _req(base + f'/api/proposals/{p["id"]}/approve', 'POST', evil)[0] == 403
        assert _req(base + '/api/agent/chat', 'POST', evil,
                    {'messages': [{'role': 'user', 'content': 'x'}]})[0] == 403
        assert srv.store.get(p['id'])['status'] == proposals_mod.PENDING

    def test_chat_rejects_malformed_bodies(self, server):
        _, base = server
        h = {'X-Schist-Agent': '1'}
        for body in ([1, 2], {'messages': ['hi']}, {'messages': [{'role': 'user', 'content': 3}]}):
            assert _req(base + '/api/agent/chat', 'POST', h, body)[0] == 400

    def test_posts_need_custom_headers(self, server):
        _, base = server
        assert _req(base + '/api/rebuild', 'POST')[0] == 403
        assert _req(base + '/api/agent/chat', 'POST',
                    body={'messages': [{'role': 'user', 'content': 'x'}]})[0] == 403
        # The preflight a cross-origin page would need is never approved.
        assert _req(base + '/api/rebuild', 'OPTIONS',
                    headers={'Origin': 'https://evil.example'})[0] >= 400

    def test_rebuild_that_times_out_answers_500_and_frees_the_lock(self, server, monkeypatch):
        srv, base = server
        page = (srv.out / 'index.html').read_bytes()
        real_run = subprocess.run

        def slow_build(cmd, **kw):
            if 'build' in cmd:
                raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
            return real_run(cmd, **kw)
        monkeypatch.setattr(serve_mod.subprocess, 'run', slow_build)
        code, body = _req(base + '/api/rebuild', 'POST', {'X-Schist-Rebuild': '1'})
        out = json.loads(body)
        assert code == 500 and out['ok'] is False and 'timed out' in out['log']
        assert not srv.build_lock.locked() and not srv.staging.exists()
        assert (srv.out / 'index.html').read_bytes() == page

    def test_rebuild_skip_and_cooldown(self, server):
        _, base = server
        h = {'X-Schist-Rebuild': '1'}
        code, body = _req(base + '/api/rebuild?if_changed=1', 'POST', h)
        assert code == 200 and json.loads(body)['skipped'] == 'unchanged'
        assert _req(base + '/api/rebuild', 'POST', h)[0] == 200
        assert _req(base + '/api/rebuild', 'POST', h)[0] == 429

    def test_chat_streams_sse(self, server):
        _, base = server
        code, body = _req(base + '/api/agent/chat', 'POST', {'X-Schist-Agent': '1'},
                          {'messages': [{'role': 'user', 'content': 'hi'}]})
        events = [json.loads(line[6:]) for line in body.decode().split('\n\n') if line.startswith('data: ')]
        assert code == 200 and events[-1] == {'type': 'done'}
        assert ''.join(e['text'] for e in events if e['type'] == 'token') == 'hello [[notes/lock.md]]'
        assert {'type': 'cited', 'ids': {'notes/lock.md': 'Vault write lock'}} in events

    def test_proposal_approve_over_http(self, server, ro):
        srv, base = server
        p = srv.store.propose_note(ro, title='Via http', body='b', rationale='r')
        code, body = _req(base + '/api/proposals')
        assert [x['id'] for x in json.loads(body)] == [p['id']]
        assert _req(base + f'/api/proposals/{p["id"]}/approve', 'POST')[0] == 403
        before = srv.status()['built_head']
        code, body = _req(base + f'/api/proposals/{p["id"]}/approve', 'POST', {'X-Schist-Agent': '1'})
        out = json.loads(body)
        assert code == 200 and out['status'] == 'approved' and out['rebuilding'] is True
        # A background rebuild runs at the new commit. (Whether the site shows
        # the note depends on the index, which a spoke's post-commit hook
        # refreshes; this fixture vault has no hook.)
        for _ in range(100):
            st = srv.status()
            if not st['building'] and st['built_head'] != before:
                break
            time.sleep(0.1)
        assert st['built_head'] == st['vault_head'] != before

    def test_approve_while_a_rebuild_runs_does_not_claim_a_rebuild(self, server, ro):
        srv, base = server
        p = srv.store.propose_note(ro, title='During a build', body='b', rationale='r')
        with srv.build_lock:
            code, body = _req(base + f'/api/proposals/{p["id"]}/approve', 'POST', {'X-Schist-Agent': '1'})
        out = json.loads(body)
        assert code == 200 and out['status'] == 'approved' and out['rebuilding'] is False


class TestToken:
    def test_created_private_and_reused(self, tmp_path):
        token = serve_mod.load_or_create_token(tmp_path)
        path = tmp_path / '.schist' / serve_mod.TOKEN_FILE
        assert len(token) >= 32 and path.stat().st_mode & 0o777 == 0o600
        assert serve_mod.load_or_create_token(tmp_path) == token

    def test_readable_by_others_is_replaced(self, tmp_path):
        token = serve_mod.load_or_create_token(tmp_path)
        path = tmp_path / '.schist' / serve_mod.TOKEN_FILE
        os.chmod(path, 0o644)
        fresh = serve_mod.load_or_create_token(tmp_path)
        assert fresh != token and path.stat().st_mode & 0o777 == 0o600


def test_binds_ipv6_loopback(vault, db_path):
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(('::1', 0))
    except OSError:
        pytest.skip('no IPv6 loopback on this host')
    store = proposals_mod.ProposalStore(vault)
    srv = serve_mod.ViewerServer(vault, db_path, vault / '.schist' / 'data', '::1', 0,
                                 None, store, TOKEN)
    try:
        assert srv.address_family == socket.AF_INET6
        assert f'[::1]:{srv.server_address[1]}' in srv.allowed_hosts
    finally:
        srv.server_close()


# ---------------------------------------------------------------------------
# Pull before rebuild (a spoke)
# ---------------------------------------------------------------------------

class FakePull:
    """Stands in for `schist sync pull`; records each command."""

    def __init__(self, returncode=0, stderr='', raises=None, effect=None):
        self.returncode, self.stderr, self.raises, self.effect = returncode, stderr, raises, effect
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.raises:
            raise self.raises
        if self.effect:
            self.effect()
        return subprocess.CompletedProcess(cmd, self.returncode, '', self.stderr)


def _spoke_server(vault, db_path, runner, pull=True):
    (vault / '.schist' / 'spoke.yaml').write_text('hub: x\nidentity: t\nscope: notes\n')
    store = proposals_mod.ProposalStore(vault)
    srv = serve_mod.ViewerServer(vault, db_path, vault / '.schist' / 'data', '127.0.0.1', 0,
                                 None, store, TOKEN, pull=pull, runner=runner)
    with srv.build_lock:
        assert srv.rebuild()[0]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f'http://127.0.0.1:{srv.server_address[1]}'


class TestPullBeforeRebuild:
    H = {'X-Schist-Rebuild': '1'}

    def _rebuild(self, base, timer=False):
        code, body = _req(base + '/api/rebuild' + ('?if_changed=1' if timer else ''), 'POST', self.H)
        return code, json.loads(body)

    def test_runs_sync_pull_with_a_lock_timeout(self, vault, db_path):
        fake = FakePull()
        srv, base = _spoke_server(vault, db_path, fake)
        try:
            code, body = self._rebuild(base)
            assert code == 200 and body['ok'] and body['pull']['ok']
            cmd, kw = fake.calls[0]
            assert cmd[-3:] == ['sync', 'pull', f'--lock-timeout={serve_mod.PULL_LOCK_TIMEOUT_S}']
            assert kw['timeout'] == serve_mod.PULL_TIMEOUT_S and kw['stdin'] == subprocess.DEVNULL
        finally:
            srv.shutdown(); srv.server_close()

    @pytest.mark.parametrize('fake,expect', [
        (FakePull(returncode=75), 'pull skipped: the vault write lock is busy'),
        (FakePull(returncode=1, stderr="Error: pull failed — fatal: 'origin' does not appear to be a git "
                  "repository\nfatal: Could not read from remote repository.\n\nPlease make sure you "
                  "have the correct access rights\nand the repository exists.\n"),
         "pull failed: fatal: 'origin' does not appear to be a git repository"),
        (FakePull(raises=subprocess.TimeoutExpired('pull', 60)), 'pull timed out after 60 s'),
    ])
    def test_a_failed_pull_still_rebuilds_and_is_reported(self, vault, db_path, fake, expect):
        srv, base = _spoke_server(vault, db_path, fake)
        try:
            code, body = self._rebuild(base)
            assert code == 200 and body['ok']
            assert body['pull']['ok'] is False and body['pull']['message'].startswith(expect)
            st = json.loads(_req(base + '/api/status')[1])
            assert st['pulls'] is True and st['last_pull']['message'].startswith(expect)
        finally:
            srv.shutdown(); srv.server_close()

    def test_timer_rebuilds_when_the_pull_brings_commits(self, vault, db_path):
        # The unchanged check runs after the pull, so the hub's commits count.
        def land_commit():
            (vault / 'notes' / 'from-hub.md').write_text('---\ntitle: From hub\n---\n\nx\n')
            _git(vault, 'add', '-A'); _git(vault, 'commit', '-qm', 'from hub')
        srv, base = _spoke_server(vault, db_path, FakePull(effect=land_commit))
        try:
            code, body = self._rebuild(base, timer=True)
            assert code == 200 and 'skipped' not in body
            assert body['built_head'] == body['vault_head'] == _git(vault, 'rev-parse', 'HEAD')
        finally:
            srv.shutdown(); srv.server_close()

    def test_no_pull_and_non_spokes_do_not_pull(self, vault, db_path, server):
        fake = FakePull()
        srv, base = _spoke_server(vault, db_path, fake, pull=False)
        try:
            code, body = self._rebuild(base)
            assert code == 200 and body['pull'] is None and fake.calls == []
            assert json.loads(_req(base + '/api/status')[1])['pulls'] is False
        finally:
            srv.shutdown(); srv.server_close()
        (vault / '.schist' / 'spoke.yaml').unlink()        # no longer a spoke
        _, plain = server
        code, body = self._rebuild(plain)
        assert code == 200 and body['pull'] is None


@pytest.mark.integration
def test_real_pull_from_a_hub(tmp_path, monkeypatch):
    """`schist sync pull` for real, against a local bare hub."""
    import sys
    from types import SimpleNamespace
    from schist.sync import init_hub

    def cli(vault, *args, identity):
        env = {**os.environ, 'SCHIST_IDENTITY': identity}
        subprocess.run([sys.executable, '-m', 'schist', '--vault', str(vault), *args],
                       check=True, capture_output=True, text=True, env=env)

    hub = tmp_path / 'hub.git'
    init_hub(SimpleNamespace(name='t', participant=['alpha', 'beta']), str(hub))
    alpha, beta = tmp_path / 'alpha', tmp_path / 'beta'
    for path, who in ((alpha, 'alpha'), (beta, 'beta')):
        cli(path, 'init', '--spoke', '--hub', f'file://{hub}', '--scope', f'research/{who}',
            '--identity', who, identity=who)
        _git(path, 'config', 'user.email', f'{who}@t'); _git(path, 'config', 'user.name', who)
    note = alpha / 'research' / 'alpha' / '2026-10-16-from-alpha.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text('---\ntitle: from alpha\ndate: 2026-10-16\nstatus: draft\n---\n\nhi\n')
    _git(alpha, 'add', '-A'); _git(alpha, 'commit', '-qm', 'alpha: add note')
    cli(alpha, 'sync', 'push', identity='alpha')

    monkeypatch.setenv('SCHIST_IDENTITY', 'beta')
    db = beta / '.schist' / 'schist.db'
    sqlite_query.get_db(str(beta), str(db)).close()
    srv = serve_mod.ViewerServer(beta, db, beta / '.schist' / 'data', '127.0.0.1', 0, None,
                                 proposals_mod.ProposalStore(beta), TOKEN)
    try:
        before = _git(beta, 'rev-parse', 'HEAD')
        with srv.build_lock:
            result = srv.pull()
        assert result['ok'], result
        assert result['message'].startswith('pulled ') and result['message'].endswith(('commit', 'commits'))
        assert _git(beta, 'rev-parse', 'HEAD') != before
        assert 'alpha: add note' in _git(beta, 'log', '--oneline')
        with srv.build_lock:
            assert srv.pull()['message'] == 'already up to date'
    finally:
        srv.server_close()


# ---------------------------------------------------------------------------
# Search tokens, reindex wait, Ollama capability and refusal messages
# ---------------------------------------------------------------------------

class TestAgentRobustness:
    def test_punctuation_tokens_do_not_sink_an_all_words_search(self, ro):
        # '-' is an empty FTS phrase: AND-ed in, it matched nothing and the
        # search fell back to any-word ranking.
        res = agent_mod.search_notes(ro, 'write - lock')
        assert res['match'] == 'all words'
        assert [r['id'] for r in res['results']] == ['notes/lock.md']
        assert agent_mod.search_notes(ro, '- _ --')['note'] == 'empty query'

    def test_a_reindex_in_flight_ends_the_answer(self, vault, db_path, monkeypatch):
        rw = sqlite3.connect(db_path, check_same_thread=False)
        version = rw.execute('PRAGMA user_version').fetchone()[0]
        assert version != 0
        # What a reindex in flight looks like: emptied tables, version cleared.
        rw.execute('DELETE FROM docs')
        rw.execute('PRAGMA user_version = 0')
        rw.commit()
        monkeypatch.setattr(agent_mod, 'INDEX_WAIT_S', 0.3)
        # A model told to try again would retry every round; the answer must
        # end at the first timeout instead, saying why.
        fake = FakeOllama([_call('search_notes', query='lock')] * agent_mod.MAX_ROUNDS)
        agent = agent_mod.Agent(db_path, proposals_mod.ProposalStore(vault), opener=fake)
        events = list(agent.run([{'role': 'user', 'content': 'q'}]))
        assert len(fake.requests) == 1
        assert events[-1]['type'] == 'error' and 'index' in events[-1]['message']
        assert 'tool rounds' not in events[-1]['message']
        # Once the version is stamped back, a waiting tool proceeds.
        ro = agent_mod.connect_ro(db_path)
        threading.Timer(0.2, lambda: (rw.execute(f'PRAGMA user_version = {version}'), rw.commit())).start()
        try:
            assert agent_mod.wait_for_index(ro, 5) is True
        finally:
            ro.close()
            time.sleep(0.05)
            rw.close()

    def test_a_populated_index_without_a_version_is_complete(self, vault, db_path, monkeypatch):
        """An index built before ingest stamped user_version keeps 0 with its
        rows; get_db and the MCP reader accept it, so the agent must too."""
        rw = sqlite3.connect(db_path)
        rw.execute('PRAGMA user_version = 0')
        rw.commit()
        rw.close()
        monkeypatch.setattr(agent_mod, 'INDEX_WAIT_S', 0.3)
        fake = FakeOllama([_call('search_notes', query='lock'), _say('Answer.')])
        agent = agent_mod.Agent(db_path, proposals_mod.ProposalStore(vault), opener=fake)
        events = list(agent.run([{'role': 'user', 'content': 'q'}]))
        assert events[-1] == {'type': 'done'}
        tool = [m for m in fake.requests[-1]['messages'] if m['role'] == 'tool']
        assert 'Vault write lock' in tool[0]['content']

    def test_a_model_without_tool_calling_is_unavailable(self, vault, db_path):
        store = proposals_mod.ProposalStore(vault)
        no_tools = FakeOllama([], capabilities=('completion',))
        st = agent_mod.Agent(db_path, store, opener=no_tools).status()
        assert st['available'] is False and 'cannot call tools' in st['reason']
        # An Ollama that reports no capabilities is left to the chat call.
        legacy = FakeOllama([], capabilities=None)
        assert agent_mod.Agent(db_path, store, opener=legacy).status()['available'] is True

    def test_ollama_refusal_reports_its_reason(self, vault, db_path):
        fake = FakeOllama([])

        def refusing(req, timeout=None):
            url = req if isinstance(req, str) else req.full_url
            if url.endswith('/api/chat'):
                raise urllib.error.HTTPError(url, 400, 'Bad Request', {},
                                             io.BytesIO(b'{"error": "model does not support tools"}'))
            return fake(req, timeout)
        agent = agent_mod.Agent(db_path, proposals_mod.ProposalStore(vault), opener=refusing)
        events = list(agent.run([{'role': 'user', 'content': 'q'}]))
        assert events[-1] == {'type': 'error',
                              'message': 'Ollama refused the request: model does not support tools'}


# ---------------------------------------------------------------------------
# Proposals: hub ACL, body on stdin, a runner that cannot start
# ---------------------------------------------------------------------------

def _acl(vault: Path, write, identity='alice'):
    (vault / '.schist').mkdir(exist_ok=True)
    (vault / '.schist' / 'spoke.yaml').write_text(
        f'hub: file:///nonexistent\nidentity: {identity}\nscope: notes\n', encoding='utf-8')
    (vault / 'vault.yaml').write_text(
        'vault_version: 1\nname: t\nscope_convention: subdirectory\n'
        'participants:\n  - name: alice\n    type: spoke\n    default_scope: global\n'
        f'access:\n  alice:\n    read: ["*"]\n    write: {json.dumps(write)}\n', encoding='utf-8')


def _never(*a, **k):
    raise AssertionError('schist must not run')


class TestProposalACL:
    def test_out_of_scope_note_and_link_are_refused(self, vault, ro):
        _acl(vault, ['papers'])
        store = proposals_mod.ProposalStore(vault)
        with pytest.raises(proposals_mod.ProposalError, match='directory must be one of: papers$'):
            store.propose_note(ro, title='T', body='b', rationale='r')
        _acl(vault, ['elsewhere'])
        with pytest.raises(proposals_mod.ProposalError, match='none this spoke can write'):
            store.propose_note(ro, title='T', body='b', rationale='r')
        with pytest.raises(proposals_mod.ProposalError, match="may not write 'notes'"):
            store.propose_link(ro, source='notes/sync.md', target='locking', type='supports', rationale='r')
        assert store.list() == []

    def test_in_scope_proposals_are_filed(self, vault, ro):
        _acl(vault, ['notes'])
        store = proposals_mod.ProposalStore(vault)
        store.propose_note(ro, title='T', body='b', rationale='r')
        store.propose_link(ro, source='notes/sync.md', target='locking', type='supports', rationale='r')
        assert len(store.list()) == 2

    def test_approve_rechecks_a_changed_vault_yaml(self, vault, ro):
        _acl(vault, ['notes'])
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='T', body='b', rationale='r')
        _acl(vault, ['papers'])
        with pytest.raises(proposals_mod.ProposalError, match='hub would refuse'):
            store.approve(p['id'], runner=_never)
        assert store.get(p['id'])['status'] == proposals_mod.PENDING

    def test_root_files_need_a_wildcard_grant(self, vault):
        store = proposals_mod.ProposalStore(vault)
        _acl(vault, ['notes'])
        with pytest.raises(proposals_mod.ProposalError, match='the vault root'):
            store.check_writable('README.md')
        _acl(vault, ['*'])
        store.check_writable('README.md')

    def test_no_spoke_identity_or_a_broken_vault_yaml_falls_open(self, vault, ro):
        _acl(vault, ['papers'])
        (vault / '.schist' / 'spoke.yaml').unlink()
        store = proposals_mod.ProposalStore(vault)
        store.propose_note(ro, title='Standalone', body='b', rationale='r')
        _acl(vault, ['papers'])
        (vault / 'vault.yaml').write_text('access: [unbalanced\n', encoding='utf-8')
        store.propose_note(ro, title='Broken', body='b', rationale='r')
        assert len(store.list()) == 2


class TestApproveCommand:
    def test_note_body_goes_on_stdin_not_argv(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        # 150 KB of UTF-8: more than Linux allows in one argument (128 KiB).
        body = '€' * proposals_mod.MAX_BODY
        p = store.propose_note(ro, title='Big', body=body, rationale='r')
        seen = {}

        def record(cmd, **kw):
            seen['cmd'], seen['kw'] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, '', '')
        assert store.approve(p['id'], runner=record)['status'] == proposals_mod.APPROVED
        assert not any(body in arg for arg in seen['cmd'])
        assert not any(arg.startswith('--body') for arg in seen['cmd'])
        assert seen['kw']['input'] == body and 'stdin' not in seen['kw']

    def test_link_gets_empty_stdin(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_link(ro, source='notes/sync.md', target='locking', type='supports', rationale='r')
        seen = {}

        def record(cmd, **kw):
            seen['kw'] = kw
            return subprocess.CompletedProcess(cmd, 0, '', '')
        store.approve(p['id'], runner=record)
        assert seen['kw']['input'] == ''

    def test_a_runner_that_cannot_start_is_recorded(self, vault, ro):
        store = proposals_mod.ProposalStore(vault)
        p = store.propose_note(ro, title='T', body='b', rationale='r')

        def missing(cmd, **kw):
            raise FileNotFoundError(2, 'No such file or directory', cmd[0])
        done = store.approve(p['id'], runner=missing)
        assert done['status'] == proposals_mod.FAILED and 'could not run schist' in done['output']
        assert [x['id'] for x in store.list()] == [p['id']]


# ---------------------------------------------------------------------------
# A site built by other viewer code is stale
# ---------------------------------------------------------------------------

class TestViewerFingerprint:
    def test_status_tracks_the_viewer_code(self, server):
        srv, base = server
        st = json.loads(_req(base + '/api/status')[1])
        assert st['viewer_current'] is True and srv.up_to_date(st)
        # The head stamp stays a bare commit id, as older servers read it.
        assert srv.stamp.read_text().strip() == st['vault_head']
        srv.viewer_stamp.write_text('0' * 16 + '\n')
        st = json.loads(_req(base + '/api/status')[1])
        assert st['viewer_current'] is False and not srv.up_to_date(st)
        srv.viewer_stamp.unlink()
        assert srv.status()['viewer_current'] is False

    def test_timer_rebuilds_a_site_from_other_viewer_code(self, server):
        srv, base = server
        h = {'X-Schist-Rebuild': '1'}
        srv.viewer_stamp.unlink()
        code, body = _req(base + '/api/rebuild?if_changed=1', 'POST', h)
        out = json.loads(body)
        assert code == 200 and 'skipped' not in out and out['viewer_current'] is True
        code, body = _req(base + '/api/rebuild?if_changed=1', 'POST', h)
        assert json.loads(body)['skipped'] == 'unchanged'

    def test_fingerprint_covers_the_builder_and_the_page(self, monkeypatch, tmp_path):
        here = Path(serve_mod.__file__).parent
        for name in ('build.py', 'index.html'):
            (tmp_path / name).write_bytes((here / name).read_bytes())
        (tmp_path / 'serve.py').write_text('')
        monkeypatch.setattr(serve_mod, '__file__', str(tmp_path / 'serve.py'))
        base = serve_mod.viewer_fingerprint()
        for name in ('build.py', 'index.html'):
            original = (tmp_path / name).read_bytes()
            (tmp_path / name).write_bytes(original + b'\n')
            assert serve_mod.viewer_fingerprint() != base, name
            (tmp_path / name).write_bytes(original)


def _startup(vault, db_path, monkeypatch, capsys, rebuild):
    monkeypatch.setattr(serve_mod.ViewerServer, 'serve_forever', lambda self: None)
    monkeypatch.setattr(serve_mod.ViewerServer, 'rebuild', rebuild)
    serve_mod.serve(str(vault), str(db_path), None, '127.0.0.1', 0, 'm', 'http://127.0.0.1:1', True)
    return capsys.readouterr()


class TestStartupBuild:
    def test_rebuilds_a_site_from_other_viewer_code(self, server, monkeypatch, capsys):
        srv, _ = server
        calls = []
        _startup(srv.vault, srv.db, monkeypatch, capsys, lambda self: calls.append(1) or (True, ''))
        assert calls == []                      # current: no build
        srv.viewer_stamp.unlink()
        _startup(srv.vault, srv.db, monkeypatch, capsys, lambda self: calls.append(1) or (True, ''))
        assert calls == [1]

    def test_a_failed_upgrade_rebuild_still_serves_the_old_site(self, server, monkeypatch, capsys):
        srv, _ = server
        srv.viewer_stamp.unlink()
        out = _startup(srv.vault, srv.db, monkeypatch, capsys, lambda self: (False, 'boom'))
        assert 'serving the existing site' in out.err and 'boom' in out.err

    def test_no_site_and_a_failed_build_exits(self, vault, db_path, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            _startup(vault, db_path, monkeypatch, capsys, lambda self: (False, 'boom'))


# ---------------------------------------------------------------------------
# Who owns the Ollama port
# ---------------------------------------------------------------------------

def _proc_table(path, rows):
    """A /proc/net/tcp-shaped file: rows of (hex local address, port, state, uid)."""
    lines = ['  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode']
    for i, (addr, port, state, uid) in enumerate(rows):
        lines.append(f'{i:4d}: {addr}:{port:04X} 00000000:0000 {state} 00000000:00000000 '
                     f'00:00000000 00000000 {uid:5d}        0 {1000 + i} 1 0 100 0 0 10 0')
    path.write_text('\n'.join(lines) + '\n')
    return str(path)


class TestOllamaPortOwner:
    """On a shared host another user can listen on 127.0.0.1:11434 first and
    receive every question and the note text the tools return."""

    def _agent(self, vault, db_path, tmp_path, v4_rows=(), v6_rows=(), url=None):
        kw = {'ollama_url': url} if url else {}
        agent = agent_mod.Agent(db_path, proposals_mod.ProposalStore(vault), opener=FakeOllama([]), **kw)
        agent.proc_net = (_proc_table(tmp_path / 'tcp', v4_rows), _proc_table(tmp_path / 'tcp6', v6_rows))
        return agent

    def test_another_users_listener_is_refused(self, vault, db_path, tmp_path):
        other = os.getuid() + 1
        st = self._agent(vault, db_path, tmp_path, v4_rows=[('0100007F', 11434, '0A', other)]).status()
        assert st['available'] is False
        assert f'uid {other}' in st['reason'] and '--ollama-url' in st['reason']

    @pytest.mark.parametrize('addr', ['00000000000000000000000001000000', '00000000000000000000000000000000',
                                      '0000000000000000FFFF00000100007F'])
    def test_ipv6_loopback_and_wildcard_listeners_count(self, vault, db_path, tmp_path, addr):
        st = self._agent(vault, db_path, tmp_path, v6_rows=[(addr, 11434, '0A', os.getuid() + 1)]).status()
        assert st['available'] is False

    def test_the_owners_listener_is_accepted(self, vault, db_path, tmp_path):
        rows = [('00000000', 11434, '0A', os.getuid()), ('0100007F', 11434, '01', os.getuid() + 1)]
        assert self._agent(vault, db_path, tmp_path, v4_rows=rows).status()['available'] is True

    def test_other_ports_and_addresses_are_ignored(self, vault, db_path, tmp_path):
        rows = [('0100007F', 8080, '0A', os.getuid() + 1), ('0A00000A', 11434, '0A', os.getuid() + 1)]
        assert self._agent(vault, db_path, tmp_path, v4_rows=rows).status()['available'] is True

    def test_no_proc_table_means_no_check(self, vault, db_path, tmp_path):
        agent = agent_mod.Agent(db_path, proposals_mod.ProposalStore(vault), opener=FakeOllama([]))
        agent.proc_net = (str(tmp_path / 'absent'), str(tmp_path / 'absent6'))
        assert agent.status()['available'] is True

    def test_a_non_loopback_url_is_not_checked(self, vault, db_path, tmp_path):
        st = self._agent(vault, db_path, tmp_path, v4_rows=[('0100007F', 11434, '0A', os.getuid() + 1)],
                         url='http://gpu-node.example:11434').status()
        assert st['available'] is True
