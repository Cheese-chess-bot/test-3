import datetime as dt
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation

from .storage import atomic_json


class ProviderError(Exception):
    pass


class Stopped(Exception):
    pass


class HTTPFailure(Exception):
    def __init__(self, status):
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, payload=None, key=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'PurpleDesk/0.1'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ProviderError('Provider response was too large')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        # Do not log remote error bodies, request headers, or credentials.
        raise HTTPFailure(exc.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        raise ProviderError('Provider connection failed or timed out') from None
    except (ValueError, UnicodeError):
        raise ProviderError('Provider returned invalid JSON') from None


class Quota:
    """A conservative local counter; provider/account usage elsewhere is additional."""
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def state(self):
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding='utf-8'))
            if state.get('date') == today:
                return state
        return {'date': today, 'attempts': 0}

    def reserve(self, cap):
        with self.lock:
            state = self.state()
            if state['attempts'] >= cap:
                raise ProviderError('Local OpenRouter daily cap reached; resume after reset')
            state['attempts'] += 1
            atomic_json(self.path, state)


class Router:
    def __init__(self, config, quota, transport=http_json):
        self.config = config
        self.quota = quota
        self.transport = transport
        self.failed = set()
        self.verified = set()

    def request(self, url, payload, key, stop):
        """Stop waiting promptly; an already-sent request may still consume quota."""
        result = queue.Queue(maxsize=1)
        def work():
            try:
                result.put((True, self.transport(url, payload, key)))
            except Exception as exc:
                result.put((False, exc))
        threading.Thread(target=work, daemon=True).start()
        deadline = time.monotonic() + 45
        while True:
            if stop.is_set():
                raise Stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError('Provider response deadline reached')
            try:
                ok, value = result.get(timeout=min(0.2, remaining))
                if ok:
                    return value
                raise value
            except queue.Empty:
                continue

    def verify_free(self, model):
        if model == 'openrouter/free':
            return  # Provider's dedicated zero-price router; no arbitrary model fallback.
        if not model.endswith(':free'):
            raise ProviderError('Paid OpenRouter model blocked')
        if model in self.verified:
            return
        catalog = self.transport('https://openrouter.ai/api/v1/models')
        match = next((m for m in catalog.get('data', []) if m.get('id') == model), None)
        if not match or not isinstance(match.get('pricing'), dict):
            raise ProviderError('Could not verify this free model in the current catalog')
        prices = match['pricing']
        if 'prompt' not in prices or 'completion' not in prices:
            raise ProviderError('Missing price information; request blocked')
        try:
            if any(Decimal(str(v)) != 0 for v in prices.values()):
                raise ProviderError('Model has a nonzero price; request blocked')
        except InvalidOperation:
            raise ProviderError('Unrecognized price information; request blocked') from None
        self.verified.add(model)

    def complete(self, messages, stop, event, only=None):
        names = [only] if only else ['nvidia', 'openrouter']
        failures = []
        for name in names:
            if stop.is_set():
                raise Stopped()
            key = self.config[name + '_key']
            if not key or name in self.failed:
                continue
            model = self.config[name + '_model']
            payload = {'model': model, 'messages': messages, 'max_tokens': 4096,
                       'temperature': 0.2, 'stream': False}
            url = 'https://integrate.api.nvidia.com/v1/chat/completions'
            if name == 'openrouter':
                url = 'https://openrouter.ai/api/v1/chat/completions'
                payload['provider'] = {'max_price': {'prompt': 0, 'completion': 0}}
            elif model == 'nvidia/nemotron-3-super-120b-a12b':
                payload['chat_template_kwargs'] = {'enable_thinking': False}
            for attempt in range(2):
                try:
                    if stop.is_set():
                        raise Stopped()
                    if name == 'openrouter':
                        self.verify_free(model)
                        self.quota.reserve(self.config['daily_cap'])
                    event('provider', f'{name} · {model}', {'provider': name, 'model': model})
                    response = self.request(url, payload, key, stop)
                    if stop.is_set():
                        raise Stopped()
                    choices = response.get('choices', [])
                    if not choices:
                        raise ProviderError('Provider response contained no completion')
                    content = choices[0].get('message', {}).get('content')
                    if not isinstance(content, str) or not content.strip():
                        raise ProviderError('Provider returned no usable text')
                    return content, name, response.get('model', model), response.get('usage', {})
                except HTTPFailure as exc:
                    message = f'{name}: HTTP {exc.status}'
                    retry = exc.status in (408, 429, 500, 502, 503, 504)
                except ProviderError as exc:
                    message = str(exc)
                    retry = False
                event('warning', message)
                if retry and attempt == 0:
                    if stop.wait(3):
                        raise Stopped()
                    continue
                failures.append(message)
                self.failed.add(name)
                break
        raise ProviderError('; '.join(failures) or 'No usable provider key configured')
