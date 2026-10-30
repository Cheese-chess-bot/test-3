import json
import os
import tempfile
import threading
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Settings:
    def __init__(self, root):
        self.path = Path(root) / 'settings.json'
        self.lock = threading.RLock()
        self.value = {
            'nvidia_key': '', 'openrouter_key': '',
            'nvidia_model': 'nvidia/nemotron-3-super-120b-a12b',
            'openrouter_model': 'openrouter/free',
            'daily_cap': 45, 'remember_keys': False,
        }
        if self.path.exists():
            self.value.update(json.loads(self.path.read_text(encoding='utf-8')))
        for key in ('nvidia_key', 'openrouter_key'):
            env_name = key.replace('_key', '_API_KEY').upper()
            self.value[key] = os.environ.get(env_name, self.value[key])

    def snapshot(self):
        with self.lock:
            return dict(self.value)

    def public(self):
        value = self.snapshot()
        for key in ('nvidia_key', 'openrouter_key'):
            value[key + '_set'] = bool(value.pop(key))
        return value

    def update(self, changes):
        with self.lock:
            value = dict(self.value)
            for key in ('nvidia_key', 'openrouter_key'):
                if changes.get('clear_' + key):
                    value[key] = ''
                elif changes.get(key):
                    secret = str(changes[key]).strip()
                    if len(secret) > 512 or any(c.isspace() for c in secret):
                        raise ValueError('Invalid API key format')
                    value[key] = secret
            for key in ('nvidia_model', 'openrouter_model'):
                if key in changes:
                    model = str(changes[key]).strip()
                    if not model or len(model) > 200 or any(c.isspace() for c in model):
                        raise ValueError('Invalid model ID')
                    value[key] = model
            model = value['openrouter_model']
            if model != 'openrouter/free' and not model.endswith(':free'):
                raise ValueError('OpenRouter must use openrouter/free or a :free model')
            cap = int(changes.get('daily_cap', value['daily_cap']))
            if not 1 <= cap <= 50:
                raise ValueError('Daily OpenRouter cap must be 1–50')
            value['daily_cap'] = cap
            value['remember_keys'] = bool(changes.get('remember_keys', value['remember_keys']))
            saved = dict(value)
            if not value['remember_keys']:
                saved['nvidia_key'] = saved['openrouter_key'] = ''
            atomic_json(self.path, saved)
            self.value = value
            return self.public()

    def redact(self, text):
        for key in ('nvidia_key', 'openrouter_key'):
            secret = self.snapshot()[key]
            if secret:
                text = str(text).replace(secret, '[REDACTED]')
        return str(text)
