import ast
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath

from .providers import Stopped

IMAGE = 'python:3.12-slim'
MAX_FILE = 65536


def docker_status():
    if not shutil.which('docker'):
        return {'ready': False, 'message': 'Install Docker Desktop and enable its WSL integration.'}
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                                capture_output=True, timeout=8, text=True)
        if result.returncode:
            return {'ready': False, 'message': 'Docker is installed but its engine is not running.'}
        image = subprocess.run(['docker', 'image', 'inspect', IMAGE],
                               capture_output=True, timeout=8)
        return {'ready': image.returncode == 0,
                'message': 'Sandbox ready' if image.returncode == 0 else 'Click Prepare sandbox to download the Python image.'}
    except (OSError, subprocess.TimeoutExpired):
        return {'ready': False, 'message': 'Docker did not respond.'}


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name):
        if not isinstance(name, str) or not name or len(name) > 240:
            raise ValueError('A relative file path of 1–240 characters is required')
        if '\\' in name or PureWindowsPath(name).drive or Path(name).is_absolute():
            raise ValueError('Use a relative path with forward slashes')
        parts = Path(name).parts
        if '..' in parts or any(part.startswith('.') for part in parts):
            raise ValueError('Parent traversal and hidden paths are not allowed')
        cursor = self.root
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError('Symbolic links are not allowed')
        result = cursor.resolve()
        if result == self.root or not result.is_relative_to(self.root):
            raise ValueError('Path must stay inside the task workspace')
        return result

    def files(self):
        found = []
        visited = 0
        for base, dirs, files in os.walk(self.root, followlinks=False):
            visited += len(dirs) + len(files)
            if visited > 2000:
                break
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(base) / d).is_symlink())
            for name in sorted(files):
                path = Path(base) / name
                if not name.startswith('.') and path.is_file() and not path.is_symlink():
                    found.append({'path': path.relative_to(self.root).as_posix(), 'bytes': path.stat().st_size})
                    if len(found) >= 200:
                        return found
        return found

    def within_command_budget(self):
        total = count = 0
        for base, dirs, files in os.walk(self.root, followlinks=False):
            count += len(dirs) + len(files)
            if count > 1000:
                return False
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in files:
                try:
                    total += (Path(base) / name).lstat().st_size
                except FileNotFoundError:
                    continue
                if total > 32 * 1024 * 1024:
                    return False
        return True

    def read(self, name):
        path = self.path(name)
        if not path.is_file():
            raise ValueError('File does not exist')
        with path.open('rb') as stream:
            raw = stream.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError('File is larger than the 64 KiB preview limit')
        return raw.decode('utf-8')

    def write(self, name, content):
        path = self.path(name)
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise ValueError('File content must be text under 64 KiB')
        existing = self.files()
        if len(existing) >= 100 and not path.exists():
            raise ValueError('Workspace file-count limit reached')
        total = sum(item['bytes'] for item in existing)
        if total + len(content.encode()) > 2 * 1024 * 1024:
            raise ValueError('Workspace write budget exceeded')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return {'written': name, 'bytes': len(content.encode())}

    def run(self, argv, stop, timeout=30):
        if not isinstance(argv, list) or not argv or len(argv) > 32:
            raise ValueError('argv must be a nonempty list with at most 32 strings')
        if any(not isinstance(a, str) or '\x00' in a or len(a) > 4096 for a in argv):
            raise ValueError('Invalid command argument')
        timeout = max(1, min(int(timeout), 60))
        status = docker_status()
        if not status['ready']:
            return {'error': status['message'], 'executed': False}
        if ',' in str(self.root):
            raise ValueError('Place Purple Desk in a folder without commas for Docker bind mounts')
        name = 'purple-desk-' + uuid.uuid4().hex
        uid = max(1, os.getuid()) if hasattr(os, 'getuid') else 1000
        gid = max(1, os.getgid()) if hasattr(os, 'getgid') else 1000
        command = [
            'docker', 'run', '--rm', '--pull', 'never', '--name', name,
            '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges', '--pids-limit', '64',
            '--memory', '512m', '--cpus', '1', '--user', f'{uid}:{gid}',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
            '--mount', f'type=bind,src={self.root},dst=/work', '--workdir', '/work',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'HOME=/tmp',
            IMAGE, *argv,
        ]
        start = time.monotonic()
        reason = None
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
            try:
                while process.poll() is None:
                    if stop.wait(0.1):
                        reason = 'stopped'
                        break
                    if time.monotonic() - start > timeout:
                        reason = 'timeout'
                        break
                    if os.fstat(output.fileno()).st_size > 1024 * 1024:
                        reason = 'output limit'
                        break
                    if not self.within_command_budget():
                        reason = 'workspace size/file-count limit'
                        break
                if reason:
                    process.kill()
                process.wait(timeout=5)
            finally:
                # Also removes descendant/background processes inside the container.
                try:
                    subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if reason == 'stopped':
                raise Stopped()
            output.seek(0)
            text = output.read(20000).decode('utf-8', errors='replace')
            return {'executed': True, 'exit_code': process.returncode, 'output': text,
                    'limit': reason, 'seconds': round(time.monotonic() - start, 2)}

    def call(self, action, args, stop):
        if action == 'list_files':
            return self.files()
        if action == 'read_file':
            return {'path': args['path'], 'content': self.read(args['path'])}
        if action == 'write_file':
            return self.write(args['path'], args['content'])
        if action == 'check_python':
            source = self.read(args['path'])
            ast.parse(source, filename=args['path'])
            return {'syntax': 'valid', 'executed': False,
                    'note': 'Syntax check only; run tests in Docker to verify behavior.'}
        if action == 'run_command':
            return self.run(args['argv'], stop, args.get('timeout', 30))
        raise ValueError('Unknown action: ' + action)
