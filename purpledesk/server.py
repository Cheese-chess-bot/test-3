import io
import json
import secrets
import subprocess
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Engine
from .providers import ProviderError, Router
from .workspace import IMAGE, Workspace, docker_status


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root, web):
        super().__init__(address, Handler)
        self.engine = Engine(root)
        self.web = Path(web)
        self.token = secrets.token_urlsafe(32)
        self.allowed_hosts = {f'127.0.0.1:{self.server_port}', f'localhost:{self.server_port}'}
        self.preparing = False
        self.sandbox = {'ready': False, 'message': 'Checking Docker…'}
        self.admin_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Avoid writing request bodies or query parameters into logs.

    def send(self, status, body, content_type='application/json', attachment=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self, api):
        if self.headers.get('Host') not in self.server.allowed_hosts:
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {'http://' + h for h in self.server.allowed_hosts}:
            return False
        if api and not secrets.compare_digest(self.headers.get('X-Purple-Session', ''), self.server.token):
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        api = path.startswith('/api/')
        if not self.authorized(api):
            return self.send(403, {'error': 'Local session required'})
        try:
            engine = self.server.engine
            args = urllib.parse.parse_qs(parsed.query)
            if path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                content = (self.server.web / name).read_text(encoding='utf-8')
                content = content.replace('__SESSION_TOKEN__', self.server.token)
                types = {'/': 'text/html; charset=utf-8', '/app.js': 'application/javascript', '/style.css': 'text/css'}
                return self.send(200, content, types[path])
            if path == '/api/status':
                return self.send(200, {'settings': engine.settings.public(), 'quota': engine.quota.state(),
                                      'active': engine.active, 'sandbox': self.server.sandbox,
                                      'preparing': self.server.preparing})
            if path == '/api/jobs':
                return self.send(200, engine.list_jobs())
            if path == '/api/job':
                job = engine.load(args['id'][0])
                result = engine.public(job)
                result['files'] = Workspace(engine.directory(job['id']) / 'workspace').files()
                return self.send(200, result)
            if path == '/api/file':
                workspace = Workspace(engine.directory(args['id'][0]) / 'workspace')
                return self.send(200, {'content': workspace.read(args['path'][0])})
            if path == '/api/export':
                workspace = Workspace(engine.directory(args['id'][0]) / 'workspace')
                buffer = io.BytesIO()
                # Export text artifacts only, with the same traversal and size guards as reads.
                with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for item in workspace.files():
                        try:
                            archive.writestr(item['path'], workspace.read(item['path']))
                        except (ValueError, OSError, UnicodeError):
                            continue
                return self.send(200, buffer.getvalue(), 'application/zip', 'purple-desk-workspace.zip')
            return self.send(404, {'error': 'Not found'})
        except (ValueError, KeyError, IndexError, FileNotFoundError, UnicodeError) as exc:
            return self.send(400, {'error': str(exc)[:200]})

    def do_POST(self):
        if not self.authorized(True):
            return self.send(403, {'error': 'Local session required'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send(415, {'error': 'JSON required'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 100000:
                raise ValueError('Invalid request length')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            engine = self.server.engine
            if self.path == '/api/settings':
                with self.server.admin_lock:
                    if engine.active:
                        raise ValueError('Stop the task before changing providers')
                    return self.send(200, engine.settings.update(body))
            if self.path == '/api/task':
                return self.send(200, engine.create(body.get('task', ''), body.get('mode', 'live'),
                                                    bool(body.get('lab')), body.get('max_steps', 20)))
            if self.path == '/api/stop':
                return self.send(200, engine.pause(body['id']))
            if self.path == '/api/resume':
                return self.send(200, engine.resume(body['id']))
            if self.path == '/api/provider-test':
                name = body.get('provider')
                if name not in ('nvidia', 'openrouter'):
                    raise ValueError('Choose a provider')
                if not engine.settings.snapshot()[name + '_key']:
                    raise ValueError('Save a key for this provider first')
                if not self.server.admin_lock.acquire(blocking=False):
                    raise ValueError('Another provider check is in progress')
                try:
                    _, provider, model, usage = Router(engine.settings.snapshot(), engine.quota).complete(
                        [{'role': 'user', 'content': 'Reply with READY only.'}], threading.Event(),
                        lambda *args: None, only=name)
                    return self.send(200, {'message': f'Connected to {provider}: {model}', 'usage': usage})
                finally:
                    self.server.admin_lock.release()
            if self.path == '/api/sandbox':
                with self.server.admin_lock:
                    if self.server.preparing:
                        raise ValueError('Sandbox preparation is already running')
                    self.server.preparing = True
                threading.Thread(target=self.prepare, daemon=True).start()
                return self.send(200, {'message': 'Downloading the official Python container image…'})
            return self.send(404, {'error': 'Not found'})
        except (ValueError, KeyError, TypeError, ProviderError, OSError) as exc:
            return self.send(400, {'error': self.server.engine.settings.redact(str(exc))[:500]})

    def prepare(self):
        try:
            result = subprocess.run(['docker', 'pull', IMAGE], capture_output=True, timeout=240)
            self.server.sandbox = docker_status()
            if result.returncode:
                self.server.sandbox = {'ready': False, 'message': 'Image download failed. Start Docker and run: docker pull python:3.12-slim'}
        except (OSError, subprocess.TimeoutExpired):
            self.server.sandbox = {'ready': False, 'message': 'Could not prepare Docker. Install/start Docker Desktop, then retry.'}
        finally:
            self.server.preparing = False


def refresh_docker(server):
    server.sandbox = docker_status()
