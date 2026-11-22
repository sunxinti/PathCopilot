@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "PATHCOPILOT_LOCAL_MODE=1"
set "PATHCOPILOT_RUNTIME_MODE=managed"
set "API_PROVIDER=openrouter"
if not defined PATHCOPILOT_MODEL set "PATHCOPILOT_MODEL=google/gemini-3.1-pro-preview"
set "API_MAX_CONCURRENCY=0"

if defined PATHCOPILOT_PYTHON (
    if not exist "%PATHCOPILOT_PYTHON%" goto :configured_python_missing
    set "PYTHON_EXE=%PATHCOPILOT_PYTHON%"
) else (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
)

if not exist "%PYTHON_EXE%" goto :environment_missing
"%PYTHON_EXE%" -c "import flask, flask_socketio, openslide, numpy, scipy, skimage, mcp" >nul 2>&1
if errorlevel 1 goto :dependencies_missing

if not defined PATHCOPILOT_NO_BROWSER (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\open-workspace.ps1"
)

echo Starting PathCopilot CMD desktop application...
echo URL: http://127.0.0.1:5000/workspace
echo Press Ctrl+C to stop the local server.
echo Configure the model endpoint and your own API key in Settings.
echo.
"%PYTHON_EXE%" -u "%~dp0webapp\_start.py"
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0

echo.
echo ERROR: PathCopilot exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

:environment_missing
echo ERROR: Python environment was not found.
echo Double-click setup.cmd first, or set PATHCOPILOT_PYTHON.
pause
exit /b 1

:dependencies_missing
echo ERROR: Required Python packages are missing or incompatible.
echo Run setup.cmd with Python 3.11 x64.
pause
exit /b 1

:configured_python_missing
echo ERROR: PATHCOPILOT_PYTHON does not exist:
echo   %PATHCOPILOT_PYTHON%
pause
exit /b 1
