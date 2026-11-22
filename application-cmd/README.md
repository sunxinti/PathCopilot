# PathCopilot local Web application source

This directory contains the complete runnable source of the local PathCopilot
WSI workspace: the Flask backend, HTML/JavaScript/CSS frontend, Agent Loop and
prompts, Single Agent and Agent Swarm paths, Runtime, MCP integrations,
patient-level orchestration and report generation. It requires no proprietary
server and does not call or wrap the desktop EXE.

The application runs locally on Windows, Linux and macOS. The `.cmd` files are
Windows convenience launchers; other platforms start the same Python entry
point directly.

## Requirements

- Windows, Linux or macOS
- Python 3.11
- Internet access during dependency installation
- An OpenAI-compatible model endpoint and the user's own API key

## Windows setup and start

Double-click `setup.cmd`. It creates `.venv`, installs the frozen Web
application requirements, installs the Windows OpenSlide native binaries, and
runs `pip check`.

## Start

Double-click `start.cmd`. The terminal remains visible while the local server
is running and the browser opens:

<http://127.0.0.1:5000/workspace>

Press `Ctrl+C` in the terminal to stop the server. Configure the endpoint,
model identifier and API key in **Settings** after the workspace opens.

Advanced users may set `PATHCOPILOT_PYTHON` to an existing compatible Python
3.11 executable instead of using `.venv`. Set `PATHCOPILOT_NO_BROWSER=1` when
running on a headless machine to suppress automatic browser opening.

## Linux

Install Python, venv support and OpenSlide with the package manager available
on your distribution. For Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv openslide-tools

cd application-cmd
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r webapp/requirements.txt
export PATHCOPILOT_LOCAL_MODE=1
export PATHCOPILOT_RUNTIME_MODE=managed
python webapp/_start.py
```

## macOS

With Homebrew:

```bash
brew install python@3.11 openslide

cd application-cmd
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r webapp/requirements.txt
export PATHCOPILOT_LOCAL_MODE=1
export PATHCOPILOT_RUNTIME_MODE=managed
python webapp/_start.py
```

After startup, open <http://127.0.0.1:5000/workspace>. Press `Ctrl+C` to stop
the local server.

## Open-source scope

The runnable application logic is visible and modifiable. Users can inspect the
multiscale prompts, change the UI, replace the model provider, extend MCP tools,
or build their own desktop shell around the same local Web application. API
keys, local databases, chat history, logs, generated reports, WSI files and
other user data are deliberately not included.

## Demo and local data

WSI files and the public TCGA demo are not included in the Git repository. To
enable the built-in Demo entry, place the documented public demo SVS under
`webapp/demo_data/`; otherwise upload a local WSI from the workspace.

API credentials, databases, uploads, cached DZI tiles, logs and generated
reports are local runtime state and are excluded by `.gitignore`.
