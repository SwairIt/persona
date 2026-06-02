@echo off
REM Persona — manual launcher (double-clickable from File Explorer).
REM Sits in the project root, picks up .env automatically.
REM
REM Tip: to grab a single snapshot from a hotkey/script (no web UI needed)
REM run `persona-cli capture` (or `uv run persona-cli capture --quiet` for
REM just the new screenshot id). See scripts\capture_now.bat for a tiny
REM wrapper you can bind to AutoHotkey / PowerToys Run — instructions in
REM docs\CAPTURE_HOTKEY.md.

cd /d %~dp0\..

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
    uv run uvicorn app.web.main:app --host 127.0.0.1 --port 8765
    goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python -m uvicorn app.web.main:app --host 127.0.0.1 --port 8765
    goto :end
)

echo Neither 'uv' nor 'python' was found in PATH.
echo Install Python 3.12+ from https://python.org and try again.
pause

:end
