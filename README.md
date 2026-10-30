# test 3 — Purple Desk · Prototype 01

A local AI workspace for the future everyday/purple-team OS. This version is a desktop-served web app, not a bootable OS image. It works on Windows or WSL/Linux with Python 3.10+. No Python packages, Node installation, subscriptions, or dedicated GPU are required to start the app.

## Start in under a minute

1. Clone this repository and enter its folder:

   ```bash
   git clone https://github.com/Cheese-chess-bot/test-3.git
   cd test-3
   ```

   You can also extract a downloaded copy and open its folder.
2. On Windows, double-click **start-windows.bat** (requires Python with the `py` launcher). Alternatively, in PowerShell in this folder:

   ```powershell
   py -3 app.py
   ```

   On Ubuntu/Debian/Kali in WSL, open a terminal in this folder:

   ```bash
   python3 app.py
   ```

3. Open **http://127.0.0.1:8765**. The launcher normally opens it automatically.
4. Click **Try the no-key demo**. It creates real files and checks Python syntax. The demo is scripted, clearly labeled, and makes no model requests.
5. Open **Providers**, enter your NVIDIA key and optionally your OpenRouter key, then click **Save providers**. Use **Test connection** to check each configured provider. A test makes a real API call and consumes quota.
6. Start a live task with the Coding task or Purple-team lab preset.

Keep the terminal running. Press Ctrl+C to stop the app. If the port is occupied, use `python3 app.py --port 8766` (or `py -3 app.py --port 8766` on Windows).

## Enable actual command execution

The app can create/read files and check Python syntax without Docker. It executes generated commands only in Docker; it does not fall back to executing them on your Windows/WSL host.

1. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/) and start it. If you run Purple Desk inside WSL, enable integration for that WSL distribution in Docker Desktop's settings.
2. In the same terminal environment as Purple Desk, confirm `docker info` succeeds.
3. Click **Prepare sandbox**, or run:

   ```bash
   docker pull python:3.12-slim
   ```

4. Restart Purple Desk if you prepared Docker manually. The sandbox indicator should say **Sandbox ready**.
5. Run the **Purple-team lab** preset. A successful live run should produce a fix, regression tests, actual Docker test output, and a final report. Inspect the output rather than assuming the model's report is correct.

The sandbox uses Python's standard library and Debian shell basics. It has 512 MiB memory, one CPU, a 64-process limit, a read-only root filesystem, no networking, no elevated capabilities, and a writable mount of only that task's workspace. It runs as a non-root user. A command lasts at most 60 seconds. The app never passes API keys into the container.

Use a normal desktop user, not root. In WSL, keeping the project under `~/purple-desk` usually avoids Windows mount permission issues. On Linux, your Docker setup must allow your normal account to run `docker info`. Do not solve that by running this whole app as root. Container isolation is not a virtual machine security guarantee; this prototype is designed for your own local development/lab tasks.

## What the worker actually does

- NVIDIA hosted chat completions first, with a bounded retry for transient errors.
- OpenRouter fallback if the primary is unavailable. Only `openrouter/free` or an explicitly free `:free` model is accepted; specific free models have their current catalog prices checked. Maximum prompt/completion prices are set to zero.
- One validated JSON action per turn: list files, read a file, write a file, check Python syntax, run a Docker command, or finish with a report.
- Save task state, tool results, artifacts and usage after steps. Stop preserves completed work. Resume uses existing context; interrupted actions are marked as uncertain rather than automatically replayed.
- A default 20-step limit, maximum 40; a 15-minute total runtime budget; bounded file and output sizes. Stop interrupts the worker's wait for a model response; a provider request already sent may still consume quota. Container cleanup can take several seconds. A monitor stops commands whose workspace exceeds 32 MiB/1,000 entries; this polling limit is not a filesystem quota and can overshoot.
- A conservative local OpenRouter cap of 45 attempts per UTC day (configurable 1–50). It includes retries and connection tests. It is not a complete account usage meter: other apps' requests and multiple app processes are not counted here.
- Export workspace text files as a ZIP. Provider settings and conversation state are outside the exported workspace.

OpenRouter switches providers/models only for availability/transport errors, not to retry a refused request with another model. If a model cannot follow the action format after three responses, the job pauses. Completion means the model finished its workflow; tests/evidence remain the measure of whether it succeeded.

## Provider setup

Get NVIDIA developer access at <https://build.nvidia.com/>. The initial model ID is `nvidia/nemotron-3-super-120b-a12b`; it is editable if your account uses a different available model. The app uses `https://integrate.api.nvidia.com/v1/chat/completions`. NVIDIA's hosted developer service is for prototyping/evaluation, with account/model-dependent limits.

Get an OpenRouter key at <https://openrouter.ai/keys>. The default `openrouter/free` selects a free model, so quality and availability may change between calls. You can choose an available explicit `:free` model for more consistent behavior. Free variants currently have a 50/day, 20/min cap without a qualifying credit purchase. The app's local cap can pause sooner. There is no automatic paid model fallback, and neither provider/model selection is advertised as abliterated.

Keys are session-only by default. Optional **Remember keys** stores plain text in `data/settings.json` (mode 0600 on Linux); the data folder uses mode 0700. This is not OS-keychain encryption. Uncheck Remember keys and save to remove the stored copies. Environment variables `NVIDIA_API_KEY` and `OPENROUTER_API_KEY` are also supported. Keys never appear in API status responses, are never mounted into the command container, and are redacted from event messages. Never put the `data/` folder in Git or share it publicly: it also contains your task history and code.

Live mode sends the task prompt and tool results (including files the worker reads) to the selected model provider. Local demo mode sends none of this. The dashboard binds only to 127.0.0.1, checks Host/Origin, and protects API routes with a per-launch session token. Keep it local; this is not a multi-user internet service.

## First live tasks

### Coding / hackathon foundation

Use the Coding task preset. It requests a standard-library Python log summarizer, edge-case tests, and a report. You can also request a self-contained HTML/CSS/JS prototype; Python is the only preinstalled execution runtime in this initial sandbox.

### Purple-team lab

Use the Purple-team lab preset. Each task receives an intentionally unsafe, in-memory SQLite login fixture. The worker should reproduce the issue inside the local container, parameterize the query, verify valid/invalid logins, and test failed-login event logging. The fixture is not a network service and does not contain real credentials.

### What is not included yet

No external target scanning, browser automation, bug-bounty platform integration, autonomous submissions, deployment, package installation inside the sandbox, GPU inference, or bootable OS installer. This first milestone validates the provider → worker → tools → evidence loop. Remote authorized targets, additional language images, and OS packaging are later stages.

## Tests

From this folder:

```bash
python3 -m unittest discover -s tests -v
```

On Windows use `py -3 -m unittest discover -s tests -v`. Tests cover workspace boundaries, secret persistence, free-model checks, rate-limit fallback, cancellation, restart recovery, the live loop using a simulated provider, and browser/API/export flow. A real Docker integration test runs only when Docker/image and a non-root user are available. Provider network calls are simulated in automated tests; use the connection buttons for a live credential check.

## Source layout

| File | Role |
|---|---|
| `app.py` | Launch local server |
| `purpledesk/engine.py` | Agent loop, checkpoints, demo and lab |
| `purpledesk/providers.py` | NVIDIA/OpenRouter routing and quota |
| `purpledesk/workspace.py` | File guards and Docker execution |
| `purpledesk/storage.py` | Local configuration and secret handling |
| `purpledesk/server.py` | Dashboard API and exports |
| `web/` | Dashboard interface |
| `tests/` | Unit and integration checks |
| `data/` | Created at runtime; never redistribute |

## References checked for this prototype

- NVIDIA model/API: <https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-super-120b-a12b-infer>
- NVIDIA developer access: <https://docs.api.nvidia.com/nim/docs/product>
- OpenRouter free models: <https://openrouter.ai/collections/free-models>
- OpenRouter routing: <https://openrouter.ai/docs/guides/routing/provider-selection>

Version: 0.1.0. Provider availability and quotas can change. Read VALIDATION.md for exactly what was exercised before delivery.
