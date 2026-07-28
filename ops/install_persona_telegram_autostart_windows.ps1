# Install the Persona Telegram worker as an owner-scoped Windows Scheduled Task.
#
# The task starts at user logon.  The launcher itself reconnects/restarts the
# Python worker, while Scheduled Task also restarts the launcher if it crashes.
#
# First install + secure Telegram binding:
#   powershell -ExecutionPolicy Bypass -File .\ops\install_persona_telegram_autostart_windows.ps1 -Pair
#
# Reinstall without rotating the pairing code:
#   powershell -ExecutionPolicy Bypass -File .\ops\install_persona_telegram_autostart_windows.ps1

[CmdletBinding()]
param(
    [switch]$Pair
)

$ErrorActionPreference = 'Stop'
$TaskName = 'PersonaTelegramWorker'
$ScriptDir = $PSScriptRoot
$RepoRoot = (Get-Item -LiteralPath $ScriptDir).Parent.FullName
$Launcher = Join-Path $ScriptDir 'persona_telegram_worker.ps1'
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Error "Telegram launcher was not found: $Launcher"
    exit 1
}

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
        # Explicit operator action: print a fresh one-time /claim code, then
        # install/start the hidden worker.  The bot token is never printed.
        & $Python -m app.integrations.telegram --pairing-code-only
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'Could not create the Telegram pairing code.'
            exit $LASTEXITCODE
        }
    }

    $PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $ActionArgs = (
        '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass ' +
        "-File `"$Launcher`""
    )
    $Action = New-ScheduledTaskAction `
        -Execute $PowerShell `
        -Argument $ActionArgs `
        -WorkingDirectory $RepoRoot
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 10 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -Hidden
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $env:USERNAME `
        -LogonType Interactive `
        -RunLevel Limited

    # Exact, narrow task name; reinstalling is an explicit action and never
    # touches Persona's web task or any unrelated scheduled task.
    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction SilentlyContinue
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description 'Owner-only Persona Telegram agent' | Out-Null
    Start-ScheduledTask -TaskName $TaskName

    Write-Host "Installed and started '$TaskName'."
    Write-Host 'It will start automatically at every user logon.'
} finally {
    Pop-Location
}
