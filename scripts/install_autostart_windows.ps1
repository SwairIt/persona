# Persona — Windows autostart installer.
#
# Creates a Scheduled Task that launches Persona at user login, hidden, and
# restarts it if it crashes. Run this script once in PowerShell as the user
# (no admin required for "Logon" trigger on the current user).
#
# Usage:
#   cd C:\www-Yaroslav\Persona
#   pwsh -ExecutionPolicy Bypass -File .\scripts\install_autostart_windows.ps1

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Get-Item -Path $PSScriptRoot).Parent.FullName
$TaskName    = 'Persona'

Write-Host "Installing autostart task '$TaskName' for project at $ProjectRoot"

# Prefer `uv` if available, fall back to `python -m`
$UvPath = (Get-Command uv -ErrorAction SilentlyContinue)
if ($UvPath) {
    $Cmd  = $UvPath.Source
    $Args = "run uvicorn app.web.main:app --host 127.0.0.1 --port 8765"
} else {
    $PythonPath = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $PythonPath) {
        Write-Error "Neither 'uv' nor 'python' found in PATH. Install Python 3.12+ first."
        exit 1
    }
    $Cmd  = $PythonPath.Source
    $Args = "-m uvicorn app.web.main:app --host 127.0.0.1 --port 8765"
}

$action    = New-ScheduledTaskAction -Execute $Cmd -Argument $Args -WorkingDirectory $ProjectRoot
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings  = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Persona — personal AI memory (local web UI on 127.0.0.1:8765)' | Out-Null

Write-Host ""
Write-Host "Installed. Persona will start automatically at next logon."
Write-Host "  - Open:    http://127.0.0.1:8765"
Write-Host "  - Disable: Unregister-ScheduledTask -TaskName Persona -Confirm:`$false"
Write-Host "  - Run now: Start-ScheduledTask -TaskName Persona"
