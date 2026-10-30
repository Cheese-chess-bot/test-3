import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from purpledesk.engine import Engine, parse_action
from purpledesk.providers import HTTPFailure, ProviderError, Quota, Router, Stopped
from purpledesk.server import Server
from purpledesk.storage import Settings, atomic_json
from purpledesk.workspace import Workspace, docker_status

ROOT = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()


class WorkspaceTests(Base):
    def test_paths_cannot_leave_workspace(self):
        workspace = Workspace(self.root / 'work')
        for name in ('../secret', '/etc/passwd', 'C:/secret', 'a/../../secret', '.env', 'a\\secret', '.'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                workspace.write(name, 'bad')
        workspace.write('src/main.py', 'print(1)')
        self.assertEqual(workspace.read('src/main.py'), 'print(1)')

    def test_symlink_cannot_read_host(self):
        if os.name == 'nt':
            self.skipTest('Creating symlinks may require Windows privileges')
        workspace = Workspace(self.root / 'work')
        (self.root / 'secret').write_text('secret')
        (workspace.root / 'link').symlink_to(self.root / 'secret')
        with self.assertRaises(ValueError):
            workspace.read('link')
        self.assertEqual(workspace.files(), [])

    def test_size_and_syntax_checks_do_not_execute(self):
        workspace = Workspace(self.root / 'work')
        with self.assertRaises(ValueError):
            workspace.write('huge.txt', 'x' * 70000)
        workspace.write('safe.py', 'raise RuntimeError("must not execute")\n')
        result = workspace.call('check_python', {'path': 'safe.py'}, threading.Event())
        self.assertFalse(result['executed'])
        workspace.write('bad.py', 'def broken(:\n')
        with self.assertRaises(SyntaxError):
            workspace.call('check_python', {'path': 'bad.py'}, threading.Event())

    def test_docker_not_ready_never_runs_on_host(self):
        workspace = Workspace(self.root / 'work')
        with patch('purpledesk.workspace.docker_status', return_value={'ready': False, 'message': 'not ready'}), patch('subprocess.Popen') as process:
            result = workspace.run(['python', '-c', 'print(1)'], threading.Event())
        self.assertFalse(result['executed'])
        process.assert_not_called()

    def test_command_workspace_budget_detects_large_files(self):
        workspace = Workspace(self.root / 'work')
        self.assertTrue(workspace.within_command_budget())
        with (workspace.root / 'large.bin').open('wb') as stream:
            stream.truncate(40 * 1024 * 1024)
        self.assertFalse(workspace.within_command_budget())

    def test_docker_command_restrictions(self):
        workspace = Workspace(self.root / 'work')
        with patch('purpledesk.workspace.docker_status', return_value={'ready': True}), patch('subprocess.Popen') as process, patch('subprocess.run'):
            process.return_value.poll.return_value = 0
            process.return_value.returncode = 0
            workspace.run(['python', '-c', 'print(1)'], threading.Event())
            argv = process.call_args.args[0]
            self.assertEqual(argv[argv.index('--network')+1], 'none')
            self.assertEqual(argv[argv.index('--pull')+1], 'never')
            self.assertIn('--read-only', argv)
            self.assertIn('no-new-privileges', argv)
            self.assertIn('--memory', argv)
            self.assertNotIn('--privileged', argv)
            self.assertNotIn('shell', process.call_args.kwargs)
            self.assertTrue(all('API_KEY' not in a for a in argv))


class SettingsTests(Base):
    def test_session_keys_not_written_or_returned(self):
        settings = Settings(self.root)
        public = settings.update({'nvidia_key': 'secret-nv', 'openrouter_key': 'secret-or'})
        self.assertNotIn('secret-nv', json.dumps(public))
        self.assertTrue(public['nvidia_key_set'])
        self.assertNotIn('secret-nv', settings.path.read_text())
        self.assertEqual(settings.snapshot()['nvidia_key'], 'secret-nv')
        settings.update({'remember_keys': True})
        self.assertIn('secret-nv', settings.path.read_text())
        settings.update({'remember_keys': False})
        self.assertNotIn('secret-nv', settings.path.read_text())

    def test_paid_model_rejected_atomically(self):
        settings = Settings(self.root)
        before = settings.snapshot()
        with self.assertRaises(ValueError):
            settings.update({'nvidia_key': 'key', 'openrouter_model': 'paid/model'})
        self.assertEqual(settings.snapshot(), before)

    def test_quota_survives_restart(self):
        quota = Quota(self.root / 'quota.json')
        quota.reserve(1)
        again = Quota(self.root / 'quota.json')
        with self.assertRaises(ProviderError):
            again.reserve(1)
        self.assertEqual(again.state()['attempts'], 1)


class ProviderTests(Base):
    def config(self):
        settings = Settings(self.root)
        settings.update({'nvidia_key': 'nv', 'openrouter_key': 'or'})
        return settings.snapshot()

    def test_failover_on_provider_failure(self):
        calls = []
        def transport(url, payload=None, key=None):
            calls.append((url, payload))
            if 'nvidia.com' in url:
                raise HTTPFailure(401)
            return {'choices': [{'message': {'content': 'READY'}}], 'model': 'served-free-model'}
        router = Router(self.config(), Quota(self.root / 'quota.json'), transport)
        text, provider, model, usage = router.complete([{'role': 'user', 'content': 'test'}], threading.Event(), lambda *a: None)
        self.assertEqual((text, provider), ('READY', 'openrouter'))
        self.assertEqual(calls[-1][1]['provider']['max_price']['prompt'], 0)
        self.assertEqual(calls[-1][1]['model'], 'openrouter/free')
        self.assertEqual(router.quota.state()['attempts'], 1)
        router.complete([{'role': 'user', 'content': 'test'}], threading.Event(), lambda *a: None)
        self.assertEqual(len(calls), 3)  # failed NVIDIA not retried on every step

    def test_429_retry_then_fallback(self):
        calls = []
        def transport(url, payload=None, key=None):
            calls.append(url)
            if 'nvidia.com' in url:
                raise HTTPFailure(429)
            return {'choices': [{'message': {'content': 'READY'}}]}
        stop = threading.Event()
        with patch.object(stop, 'wait', return_value=False):
            Router(self.config(), Quota(self.root / 'quota.json'), transport).complete([], stop, lambda *a: None)
        self.assertEqual(sum('nvidia.com' in u for u in calls), 2)

    def test_cancelled_response_not_executed(self):
        stop = threading.Event()
        def transport(*args):
            stop.set()
            return {'choices': [{'message': {'content': 'READY'}}]}
        with self.assertRaises(Stopped):
            Router(self.config(), Quota(self.root / 'quota.json'), transport).complete([], stop, lambda *a: None)

    def test_stop_interrupts_waiting_for_provider(self):
        started, release, stop = threading.Event(), threading.Event(), threading.Event()
        outcome = []
        def transport(*args):
            started.set()
            release.wait(3)
            return {'choices': [{'message': {'content': 'READY'}}]}
        router = Router(self.config(), Quota(self.root / 'quota.json'), transport)
        def worker():
            try:
                router.complete([], stop, lambda *a: None)
            except Stopped:
                outcome.append('stopped')
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            stop.set()
            thread.join(1)
            self.assertEqual(outcome, ['stopped'])
            self.assertFalse(thread.is_alive())
        finally:
            release.set()
            thread.join(3)

    def test_nonzero_catalog_price_blocked(self):
        config = self.config()
        config['openrouter_model'] = 'x/model:free'
        def transport(*args):
            return {'data': [{'id': 'x/model:free', 'pricing': {'prompt':'0', 'completion':'1'}}]}
        router = Router(config, Quota(self.root / 'quota.json'), transport)
        with self.assertRaises(ProviderError):
            router.verify_free('x/model:free')

    def test_refusal_does_not_trigger_provider_hopping(self):
        calls = []
        def transport(*args):
            calls.append(args)
            return {'choices': [{'message': {'content': 'I cannot assist with that.'}}]}
        result = Router(self.config(), Quota(self.root / 'quota.json'), transport).complete([], threading.Event(), lambda *a: None)
        self.assertEqual(result[1], 'nvidia')
        self.assertEqual(len(calls), 1)


class EngineTests(Base):
    def wait(self, engine):
        engine.thread.join(timeout=10)
        self.assertFalse(engine.thread.is_alive())

    def test_demo_creates_actual_artifacts_and_persists(self):
        engine = Engine(self.root)
        job = engine.create('Build a demo', mode='demo')
        self.wait(engine)
        result = engine.load(job['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertIn('NOT executed', result['report'])
        files = Workspace(engine.directory(job['id']) / 'workspace').files()
        self.assertIn('hello.py', [f['path'] for f in files])
        restarted = Engine(self.root)
        self.assertEqual(restarted.load(job['id'])['status'], 'completed')

    def test_live_loop_with_fake_provider(self):
        actions = iter([
            {'action':'write_file','args':{'path':'tool.py','content':'print("ok")\n'}},
            {'action':'check_python','args':{'path':'tool.py'}},
            {'action':'finish','args':{'summary':'Syntax valid; execution not run.'}},
        ])
        requests = []
        def transport(url, payload=None, key=None):
            requests.append(json.loads(json.dumps(payload)))
            return {'choices':[{'message':{'content':json.dumps(next(actions))}}], 'usage':{'total_tokens':10}}
        engine = Engine(self.root, lambda c,q: Router(c,q,transport))
        engine.settings.update({'nvidia_key':'fake-key'})
        job = engine.create('Create a Python tool')
        self.wait(engine)
        done = engine.load(job['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['tokens'], 30)
        self.assertIn('TOOL_RESULT', requests[1]['messages'][-1]['content'])
        self.assertNotIn('fake-key', json.dumps(engine.public(done)))

    def test_restart_does_not_replay_pending_action(self):
        engine = Engine(self.root)
        job_id = 'a' * 32
        job = {'id':job_id,'status':'running','messages':[], 'pending_action':{'action':'run_command','args':{'argv':['python','test.py']}}}
        atomic_json(engine.directory(job_id) / 'job.json', job)
        restarted = Engine(self.root)
        job = restarted.load(job_id)
        self.assertEqual(job['status'], 'paused')
        self.assertNotIn('pending_action', job)
        self.assertIn('interrupted', job['messages'][-1]['content'])
        self.assertIsNone(restarted.active)

    def test_invalid_response_pauses(self):
        def transport(*args):
            return {'choices':[{'message':{'content':'not JSON'}}]}
        engine = Engine(self.root, lambda c,q: Router(c,q,transport))
        engine.settings.update({'nvidia_key':'fake'})
        job = engine.create('Create a Python tool')
        self.wait(engine)
        done = engine.load(job['id'])
        self.assertEqual(done['status'], 'paused')
        self.assertEqual(done['steps'], 3)

    def test_stop_and_resume(self):
        engine = Engine(self.root)
        job = engine.create('Build a demo', mode='demo')
        engine.pause(job['id'])
        self.wait(engine)
        self.assertEqual(engine.load(job['id'])['status'], 'paused')
        engine.resume(job['id'])
        self.wait(engine)
        self.assertEqual(engine.load(job['id'])['status'], 'completed')

    def test_parser_rejects_unknown_action(self):
        for value in ('[]', '{"action":"shell","args":{}}', '{"action":"read_file","args":[]}'):
            with self.assertRaises(ValueError):
                parse_action(value)


class HTTPTests(Base):
    def setUp(self):
        super().setUp()
        self.server = Server(('127.0.0.1',0), self.root, ROOT/'web')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        if self.server.engine.thread:
            self.server.engine.thread.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()

    def request(self, path, data=None, token=True, origin=None):
        headers = {'Content-Type':'application/json'}
        if token:
            headers['X-Purple-Session'] = self.server.token
        if origin:
            headers['Origin'] = origin
        req = urllib.request.Request(self.url+path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        return urllib.request.urlopen(req, timeout=5)

    def test_api_requires_session_and_same_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api/status', token=False)
        self.assertEqual(exc.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request('/api/settings', {}, origin='https://evil.example')
        self.assertEqual(exc.exception.code,403)

    def test_browser_to_demo_to_export(self):
        with self.request('/') as response:
            self.assertIn(self.server.token, response.read().decode())
        with self.request('/api/task', {'task':'Make the local demo','mode':'demo'}) as response:
            job = json.load(response)
        self.server.engine.thread.join(5)
        with self.request('/api/job?id='+job['id']) as response:
            result = json.load(response)
        self.assertEqual(result['status'],'completed')
        self.assertNotIn('messages',result)
        with self.request('/api/export?id='+job['id']) as response:
            raw = response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertIn('hello.py', archive.namelist())
            self.assertNotIn('settings.json', archive.namelist())


class DockerIntegration(Base):
    def test_actual_container_runs_and_network_is_disabled(self):
        if not docker_status()['ready'] or (hasattr(os,'getuid') and os.getuid()==0):
            self.skipTest('Requires Docker/image and a non-root desktop user')
        workspace = Workspace(self.root/'work')
        workspace.write('check.py', 'import socket\nfrom pathlib import Path\nassert len(socket.if_nameindex()) == 1\nPath("proof.txt").write_text("ran in sandbox")\nprint("sandbox-pass")\n')
        result = workspace.run(['python','check.py'], threading.Event())
        self.assertEqual(result['exit_code'],0, result)
        self.assertIn('sandbox-pass', result['output'])
        self.assertEqual(workspace.read('proof.txt'),'ran in sandbox')


if __name__ == '__main__':
    unittest.main()
