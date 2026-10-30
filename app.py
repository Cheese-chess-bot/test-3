#!/usr/bin/env python3
import argparse
import os
import threading
import webbrowser
from pathlib import Path

from purpledesk.server import Server, refresh_docker


def main():
    parser = argparse.ArgumentParser(description='Purple Desk local prototype')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent / 'data')
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        args.data_dir.chmod(0o700)
    server = Server(('127.0.0.1', args.port), args.data_dir, Path(__file__).resolve().parent / 'web')
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'Purple Desk is running at {url}', flush=True)
    print('Open Providers to enter API keys, or select Offline demo. Ctrl+C stops the app.', flush=True)
    threading.Thread(target=refresh_docker, args=(server,), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        server.engine.stop.set()
        if server.engine.thread:
            server.engine.thread.join(timeout=12)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
