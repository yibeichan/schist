"""`schist serve`: the viewer site plus its local API, on loopback.

  GET  /api/status                  {built_at, built_head, vault_head, building}
  POST /api/rebuild[?if_changed=1]  rebuild the site (niced); 409 busy, 429 cooldown
  GET  /api/agent/status            {available, model, reason?}
  POST /api/agent/chat              SSE stream of agent events (see agent.py)
  GET  /api/proposals               pending and failed proposals
  POST /api/proposals/<id>/approve  write it through `schist add` / `schist link`
  POST /api/proposals/<id>/reject

The site holds the full text of every note, and approve writes to the vault,
so the server binds 127.0.0.1 by default and guards every request:
  * every request needs the access token, kept in <vault>/.schist/serve-token
    (mode 0600). Binding to 127.0.0.1 keeps other hosts out but not other
    users of this host, who can connect to its loopback ports. Opening the URL
    the server prints (`/?token=...`) sets an HttpOnly, SameSite=Strict cookie
    for the page; scripts send an `X-Schist-Token` header instead. Delete the
    file and restart to issue a new token;
  * the Host header must name this server on a loopback address (DNS rebinding);
  * every POST needs an `X-Schist-Rebuild: 1` or `X-Schist-Agent: 1` header. A
    custom header forces a CORS preflight, which this server never approves,
    so another site open in the browser cannot POST here.
"""

from __future__ import annotations

import hmac
import http.cookies
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import agent as agent_mod
from . import proposals as proposals_mod

COOLDOWN_S = 30
MAX_BODY_BYTES = 256 * 1024
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '[::1]')
TOKEN_FILE = 'serve-token'


def load_or_create_token(vault: Path) -> str:
    """The access token in <vault>/.schist/serve-token, created on first use.
    A token file that other users can read is replaced, since they could
    already have read it."""
    path = Path(vault) / '.schist' / TOKEN_FILE
    try:
        if path.stat().st_mode & 0o077 == 0:
            token = path.read_text(encoding='utf-8').strip()
            if len(token) >= 32:
                return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(TOKEN_FILE + '.tmp')
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(token + '\n')
    os.replace(tmp, path)
    return token


class ViewerServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, vault: Path, db: Path, out: Path, host: str, port: int,
                 agent: agent_mod.Agent | None, store: proposals_mod.ProposalStore,
                 token: str):
        self.vault, self.db, self.out = Path(vault), Path(db), Path(out)
        self.staging = self.out.with_name(self.out.name + '.staging')
        self.stamp = self.vault / '.schist' / 'viewer-built-head'
        self.agent, self.store = agent, store
        self.token = token
        self.build_lock = threading.Lock()
        self.chat_lock = threading.Lock()   # one question at a time
        self.last_manual = 0.0
        if ':' in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, port), Handler)
        port = self.server_address[1]
        named = f'[{host}]' if ':' in host else host
        self.allowed_hosts = {f'{h}:{port}' for h in LOOPBACK_HOSTS} | {f'{named}:{port}'}
        # Cookies are not scoped by port, so the name carries it.
        self.cookie_name = f'schist_token_{port}'

    # -- site ---------------------------------------------------------------

    def vault_head(self) -> str:
        r = subprocess.run(['git', '-C', str(self.vault), 'rev-parse', 'HEAD'],
                           capture_output=True, text=True)
        return r.stdout.strip()

    def status(self) -> dict:
        page = self.out / 'index.html'
        return {
            'built_at': page.stat().st_mtime if page.exists() else None,
            'built_head': self.stamp.read_text().strip() if self.stamp.exists() else None,
            'vault_head': self.vault_head(),
            'building': self.build_lock.locked(),
        }

    def rebuild(self) -> tuple[bool, str]:
        """Build into a staging dir, then swap files in, page last, so a load
        mid-rebuild never pairs a new page with half-written data. Caller
        holds build_lock."""
        head = self.vault_head()
        shutil.rmtree(self.staging, ignore_errors=True)
        cmd = [sys.executable, '-m', 'schist', '--vault', str(self.vault), '--db', str(self.db),
               'build', '--out', str(self.staging)]
        if shutil.which('nice'):
            cmd = ['nice', '-n', '10', *cmd]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout)[-2000:]
        self.out.mkdir(parents=True, exist_ok=True)
        for name in sorted((p.name for p in self.staging.iterdir()), key=lambda n: n == 'index.html'):
            os.replace(self.staging / name, self.out / name)
        self.staging.rmdir()
        self.stamp.write_text(head + '\n')
        return True, r.stdout[-2000:]


class Handler(http.server.SimpleHTTPRequestHandler):
    server: ViewerServer

    def __init__(self, request, client_address, server):
        super().__init__(request, client_address, server, directory=str(server.out))

    # -- plumbing -----------------------------------------------------------

    def _host_ok(self) -> bool:
        if self.headers.get('Host') not in self.server.allowed_hosts:
            self.send_error(403, 'bad Host')
            return False
        return True

    def _token_ok(self) -> bool:
        """The request carries the access token, as a header or the cookie."""
        given = self.headers.get('X-Schist-Token')
        if given is None:
            jar = http.cookies.SimpleCookie()
            try:
                jar.load(self.headers.get('Cookie') or '')
            except http.cookies.CookieError:
                pass
            morsel = jar.get(self.server.cookie_name)
            given = morsel.value if morsel else None
        if given is not None and hmac.compare_digest(given.encode(), self.server.token.encode()):
            return True
        self.send_error(401, 'missing or wrong access token',
                        'Open the URL `schist serve` printed, or send an X-Schist-Token '
                        'header with the token in <vault>/.schist/serve-token.')
        return False

    def _guard(self) -> bool:
        return self._host_ok() and self._token_ok()

    def _login(self, url) -> bool:
        """`?token=<t>` on a GET: set the cookie and redirect to the bare path.
        Returns False when the query carries no token."""
        given = parse_qs(url.query).get('token')
        if not given:
            return False
        if not hmac.compare_digest(given[0].encode(), self.server.token.encode()):
            self.send_error(401, 'wrong access token')
            return True
        self.send_response(303)
        self.send_header('Location', url.path or '/')
        self.send_header('Set-Cookie', f'{self.server.cookie_name}={self.server.token}; Path=/; '
                                       'HttpOnly; SameSite=Strict; Max-Age=31536000')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return True

    def _json(self, code: int, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n < 0:
            self._json(400, {'ok': False, 'reason': 'bad Content-Length'})
            return None
        if n > MAX_BODY_BYTES:
            self._json(413, {'ok': False, 'reason': 'request too large'})
            return None
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json(400, {'ok': False, 'reason': 'body is not a JSON object'})
            return None
        return body

    def end_headers(self):
        if not self.path.startswith('/api/'):
            self.send_header('Cache-Control', 'no-cache')  # data changes on rebuild
        super().end_headers()

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') or (len(args) > 1 and str(args[1])[:1] in '45'):
            super().log_message(fmt, *args)

    # -- GET ----------------------------------------------------------------

    def do_HEAD(self):
        if self._guard():
            super().do_HEAD()

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        if self._login(url) or not self._token_ok():
            return
        path = url.path
        if path == '/api/status':
            return self._json(200, self.server.status())
        if path == '/api/agent/status':
            if self.server.agent is None:
                return self._json(200, {'available': False, 'model': None,
                                        'reason': 'agent disabled (--no-agent)'})
            return self._json(200, self.server.agent.status())
        if path == '/api/proposals':
            return self._json(200, self.server.store.list())
        if path.startswith('/api/'):
            return self.send_error(404)
        return super().do_GET()

    # -- POST ---------------------------------------------------------------

    def do_POST(self):
        if not self._guard():
            return
        url = urlsplit(self.path)
        path = url.path
        if path == '/api/rebuild':
            if self.headers.get('X-Schist-Rebuild') != '1':
                return self.send_error(403, 'missing X-Schist-Rebuild header')
            return self._rebuild('1' in parse_qs(url.query).get('if_changed', []))
        if self.headers.get('X-Schist-Agent') != '1':
            return self.send_error(403, 'missing X-Schist-Agent header')
        if path == '/api/agent/chat':
            return self._chat()
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['api', 'proposals'] and parts[3] in ('approve', 'reject'):
            return self._decide(parts[2], parts[3])
        return self.send_error(404)

    def _rebuild(self, timer: bool):
        srv = self.server
        if not srv.build_lock.acquire(blocking=False):
            return self._json(409, {'ok': False, 'reason': 'rebuild already running'})
        try:
            st = srv.status()
            if timer and st['built_head'] == st['vault_head']:
                return self._json(200, {'ok': True, 'skipped': 'unchanged', **st})
            if not timer and time.time() - srv.last_manual < COOLDOWN_S:
                return self._json(429, {'ok': False, 'reason': f'cooldown {COOLDOWN_S}s'})
            if not timer:
                srv.last_manual = time.time()
            t0 = time.time()
            ok, log = srv.rebuild()
        finally:
            srv.build_lock.release()
        return self._json(200 if ok else 500, {'ok': ok, 'seconds': round(time.time() - t0, 2),
                                               'log': log, **srv.status()})

    def _chat(self):
        srv = self.server
        body = self._body()
        if body is None:
            return
        if srv.agent is None:
            return self._json(404, {'ok': False, 'reason': 'agent disabled (--no-agent)'})
        messages = body.get('messages')
        if (not isinstance(messages, list) or not messages
                or not all(isinstance(m, dict) and isinstance(m.get('content'), str) for m in messages)
                or messages[-1].get('role') != 'user'):
            return self._json(400, {'ok': False, 'reason': 'messages must be {role, content} objects '
                                                           'ending with a user message'})
        if not srv.chat_lock.acquire(blocking=False):
            return self._json(409, {'ok': False, 'reason': 'the agent is answering another question'})
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            gone = threading.Event()
            for event in srv.agent.run(messages, should_stop=gone.is_set):
                try:
                    self.wfile.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    gone.set()   # the page pressed Stop or closed; end the loop
        finally:
            srv.chat_lock.release()
        self.close_connection = True

    def _decide(self, pid: str, action: str):
        store = self.server.store
        try:
            p = store.approve(pid) if action == 'approve' else store.reject(pid)
        except proposals_mod.ProposalError as e:
            return self._json(400, {'ok': False, 'reason': str(e)})
        return self._json(200, {'ok': p['status'] != proposals_mod.FAILED, **p})


def serve(vault: str, db: str, out: str | None, host: str, port: int,
          model: str, ollama_url: str, no_agent: bool) -> None:
    vault_p = Path(vault).resolve()
    out_p = Path(out) if out else vault_p / '.schist' / 'data'
    store = proposals_mod.ProposalStore(vault_p, Path(db))
    agent = None if no_agent else agent_mod.Agent(Path(db), store, model=model, ollama_url=ollama_url)
    token = load_or_create_token(vault_p)
    srv = ViewerServer(vault_p, Path(db), out_p, host, port, agent, store, token)
    if not (out_p / 'index.html').exists():
        with srv.build_lock:
            ok, log = srv.rebuild()
        if not ok:
            print(f'Error: initial build failed:\n{log}', file=sys.stderr)
            sys.exit(1)
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print(f'Warning: serving on {host}: anyone who can reach this address and has the '
              'access token can read every note and approve proposals; the token travels '
              'over plain HTTP.', file=sys.stderr)
    named = f'[{host}]' if ':' in host else host
    print(f'schist viewer on http://{named}:{srv.server_address[1]}/?token={token} (agent: '
          f'{"off" if agent is None else model}; token also in {vault_p / ".schist" / TOKEN_FILE})',
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
