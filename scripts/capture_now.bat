@echo off
REM Persona — one-tap capture. Bind this to a global hotkey
REM (AutoHotkey / PowerToys Run / Windows shortcut) to snapshot
REM the current screen without opening the web UI.
REM See docs\CAPTURE_HOTKEY.md for setup instructions.

cd /d %~dp0\..

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
    uv run persona-cli capture --quiet
    goto :end
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python -m app capture --quiet
    goto :end
)

echo Neither 'uv' nor 'python' was found in PATH. 1>&2
exit /b 1

:end
