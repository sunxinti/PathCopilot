# PathCopilot Windows desktop client

`PathCopilot-Setup-2.0.5-x64.exe` is the optional Windows companion client for
PathCopilot. The paper evaluation and reproducible command-line source remain
under `evaluation/`; the installer is provided only for convenient interactive
use of the WSI workspace.

## Distribution

Upload the installer as a **GitHub Release asset**. Do not commit it to the Git
repository: the file is larger than GitHub's normal per-file limit and is
therefore ignored by this directory's `.gitignore`.

## Installation

- Windows 10/11, x64
- Approximately 2 GB of free disk space
- Double-click the installer, choose an installation directory, and launch the
  desktop shortcut.

The package contains an independent portable Python runtime, OpenSlide native
libraries, the PathCopilot Web/Electron application, and one public TCGA demo
WSI. Users do not need to install Python or Node.js.

## API configuration and privacy

No API key, local account database, upload history, log, cached DZI, or analysis
output is included. On first launch, open **Settings** and enter an
OpenAI-compatible endpoint, model identifier, and the user's own API key.

The bundled demo is a public TCGA slide and carries only the trusted specimen
context: `这是一个子宫器官取样的切片。` Do not upload identifiable clinical
material to an external model provider without appropriate authorization and
data-processing safeguards.

## Integrity

Verify the installer against `SHA256SUMS.txt` before distribution. The current
artifact is unsigned, so Windows SmartScreen may display an unrecognized
publisher warning until a trusted code-signing certificate is applied.

