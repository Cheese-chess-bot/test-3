# Prototype validation

Date: 30 October 2026. Version: 0.1.0.

## Executed successfully

- Python unit/API/integration suite: **23 passed, 1 skipped** (24 total).
- Real local HTTP workflow: task creation → scripted demo → files → report → ZIP export.
- Simulated live-provider workflow: model actions → file creation → syntax check → final report.
- Simulated NVIDIA failure and HTTP 429 retry → OpenRouter fallback; zero-price routing asserted.
- Rejection of paid model IDs and a falsely labeled free model with a nonzero catalog price.
- Stop/resume, interruption of provider waits, restart checkpoint recovery, and no automatic replay of an interrupted command.
- Workspace traversal/symlink checks, size limits, session-only keys, redaction/public configuration, Host/Origin/session protections.
- Actual JavaScript + local API interaction through a JSDOM harness: initial status, missing-key error, offline demo, file preview, export, navigation, paid-model rejection, key field clearing, session-only storage, key removal. No JavaScript runtime errors in these interactions.
- Python compilation, JavaScript syntax, and Bash launcher syntax.

## Not verified in this environment

- Real NVIDIA/OpenRouter inference: no user API keys were supplied. Connection buttons are provided for local verification. Provider/action reliability and model output quality still need live evaluation.
- Actual Docker command execution: Docker was not installed here. Docker arguments and refusal to execute on the host were tested using mocks; the real Docker integration test is included and skipped automatically when unavailable.
- Visual rendering, responsive layout and native browser downloads in a real Chromium window: the browser runtime download failed. Functional DOM testing passed, but it is not visual/browser-engine verification.
- Execution on the user's Windows/WSL laptop and Windows .bat launcher. The server and tests were exercised on Linux/Python 3.12.

The offline demo is scripted, not an AI benchmark. It writes real artifacts and validates syntax, but deliberately does not claim to have executed their behavioral tests. A completed live task should likewise be assessed using its actual tool results.
