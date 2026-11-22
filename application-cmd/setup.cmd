@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

if defined PATHCOPILOT_PYTHON goto :configured_python

where py.exe >nul 2>&1
if not errorlevel 1 (
    py.exe -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
    if not errorlevel 1 (
        echo Creating Python 3.11 environment...
        py.exe -3.11 -m venv "%~dp0.venv"
        if errorlevel 1 goto :venv_failed
        goto :install
    )
)

where python.exe >nul 2>&1
if errorlevel 1 goto :python_missing
python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :python_version
echo Creating Python 3.11 environment...
python.exe -m venv "%~dp0.venv"
if errorlevel 1 goto :venv_failed
goto :install

:configured_python
if not exist "%PATHCOPILOT_PYTHON%" goto :configured_python_missing
"%PATHCOPILOT_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if errorlevel 1 goto :python_version
echo Creating Python 3.11 environment...
"%PATHCOPILOT_PYTHON%" -m venv "%~dp0.venv"
if errorlevel 1 goto :venv_failed

:install
echo Installing PathCopilot CMD dependencies...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :install_failed
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-windows.txt"
if errorlevel 1 goto :install_failed
"%~dp0.venv\Scripts\python.exe" -m pip check
if errorlevel 1 goto :install_failed

echo.
echo Setup complete. Double-click start.cmd to launch PathCopilot.
pause
exit /b 0

:python_missing
echo ERROR: Python 3.11 was not found.
echo Install Python 3.11 x64 from https://www.python.org/downloads/
pause
exit /b 1

:python_version
echo ERROR: PathCopilot CMD requires Python 3.11 x64 for the frozen dependencies.
pause
exit /b 1

:configured_python_missing
echo ERROR: PATHCOPILOT_PYTHON does not exist:
echo   %PATHCOPILOT_PYTHON%
pause
exit /b 1

:venv_failed
echo ERROR: Could not create .venv.
pause
exit /b 1

:install_failed
echo ERROR: Dependency installation failed. Review the output above.
pause
exit /b 1

