"""Tests for `schist serve`: the request guards, rebuild, the agent's tools and
loop (against a fake Ollama), and the proposal queue end to end."""

from __future__ import annotations

import io
import json
import subprocess
import threading
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

    def __init__(self, rounds, models=('qwen3.5:9b',)):
        self.rounds = list(rounds)
        self.models = models
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith('/api/tags'):
            return io.BytesIO(json.dumps({'models': [{'name': m} for m in self.models]}).encode())
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
                                 agent, store)
    port = srv.server_address[1]
    srv.allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
    with srv.build_lock:
        assert srv.rebuild()[0]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, f'http://127.0.0.1:{port}'
    srv.shutdown()
    srv.server_close()


def _req(url, method='GET', headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
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

    def test_posts_need_custom_headers(self, server):
        _, base = server
        assert _req(base + '/api/rebuild', 'POST')[0] == 403
        assert _req(base + '/api/agent/chat', 'POST',
                    body={'messages': [{'role': 'user', 'content': 'x'}]})[0] == 403
        # The preflight a cross-origin page would need is never approved.
        assert _req(base + '/api/rebuild', 'OPTIONS',
                    headers={'Origin': 'https://evil.example'})[0] >= 400

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

    def test_proposal_approve_over_http(self, server, ro):
        srv, base = server
        p = srv.store.propose_note(ro, title='Via http', body='b', rationale='r')
        code, body = _req(base + '/api/proposals')
        assert [x['id'] for x in json.loads(body)] == [p['id']]
        assert _req(base + f'/api/proposals/{p["id"]}/approve', 'POST')[0] == 403
        code, body = _req(base + f'/api/proposals/{p["id"]}/approve', 'POST', {'X-Schist-Agent': '1'})
        assert code == 200 and json.loads(body)['status'] == 'approved'
