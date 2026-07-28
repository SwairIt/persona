# Persona Telegram Worker — owner-only long-poll supervisor.
#
# Run this on the same machine/container and with the same .env + database as
# persona.getdoday.ru.  The Python worker reconnects on network errors; this
# wrapper additionally restarts it after an unexpected process exit.
#
# First secure binding (prints a one-time /claim command):
#   powershell -ExecutionPolicy Bypass -File .\ops\persona_telegram_worker.ps1 -Pair
#
# Normal start:
#   powershell -ExecutionPolicy Bypass -File .\ops\persona_telegram_worker.ps1

[CmdletBinding()]
param(
    [switch]$Pair,
    [int]$RestartDelay = 3
)

$ErrorActionPreference = 'Stop'
$ScriptDir = $PSScriptRoot
$RepoRoot = (Get-Item -LiteralPath $ScriptDir).Parent.FullName
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (Test-Path -LiteralPath $VenvPython) {
    $Python = $VenvPython
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        Write-Error 'Python was not found. Install Python 3.12+ or create .venv.'
        exit 1
    }
    $Python = $PythonCommand.Source
}

Push-Location -LiteralPath $RepoRoot
try {
    if ($Pair) {
        & $Python -m app.integrations.telegram --pairing-code-only
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'Could not create the Telegram pairing code.'
            exit $LASTEXITCODE
        }
    }

    Write-Host '[Persona] Telegram worker is running. Ctrl+C stops it.'
    while ($true) {
        & $Python -m app.integrations.telegram
        $Code = $LASTEXITCODE
        if ($Code -eq 0) {
            Write-Host '[Persona] Telegram worker stopped normally.'
            break
        }
        Write-Warning "[Persona] Telegram worker exited with code $Code. Restart in $RestartDelay sec."
        Start-Sleep -Seconds $RestartDelay
    }
} finally {
    Pop-Location
}
