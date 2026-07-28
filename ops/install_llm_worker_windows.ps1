# Install Persona LLM Worker as a per-user Windows Scheduled Task.
#
# The task starts at logon, runs hidden, and is restarted by both the existing
# launcher and Task Scheduler. It is never registered or started unless a
# durable PERSONA_WORKER_TOKEN exists in <repo>\.env or User/Machine env.
#
# Install (does not start immediately):
#   powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1
# Install and start now:
#   powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -StartNow
# Provision/rotate token, install and start (explicit destructive token rotation):
#   powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -ProvisionToken -StartNow
# Diagnose:
#   powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -Status
# Remove:
#   powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -Uninstall

[CmdletBinding()]
param(
    [switch]$StartNow,
    [switch]$ProvisionToken,
    [switch]$Status,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'PersonaLLMWorker'
$RepoRoot = (Get-Item -LiteralPath $PSScriptRoot).Parent.FullName
$Launcher = Join-Path $PSScriptRoot 'persona_llm_worker.ps1'
$EnvPath = Join-Path $RepoRoot '.env'

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $EnvPath)) { return $null }
    foreach ($line in Get-Content -LiteralPath $EnvPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        if ($trimmed.Substring(0, $idx).Trim() -eq $Name) {
            return $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

function Test-DurableWorkerToken {
    $fileValue = Get-DotEnvValue 'PERSONA_WORKER_TOKEN'
    if (-not [string]::IsNullOrWhiteSpace($fileValue)) { return $true }
    $userValue = [Environment]::GetEnvironmentVariable(
        'PERSONA_WORKER_TOKEN', [EnvironmentVariableTarget]::User
    )
    if (-not [string]::IsNullOrWhiteSpace($userValue)) { return $true }
    $machineValue = [Environment]::GetEnvironmentVariable(
        'PERSONA_WORKER_TOKEN', [EnvironmentVariableTarget]::Machine
    )
    return -not [string]::IsNullOrWhiteSpace($machineValue)
}

function Get-PersonaServer {
    if (-not [string]::IsNullOrWhiteSpace($env:PERSONA_SERVER)) {
        return $env:PERSONA_SERVER.TrimEnd('/')
    }
    $fileValue = Get-DotEnvValue 'PERSONA_SERVER'
    if (-not [string]::IsNullOrWhiteSpace($fileValue)) {
        return $fileValue.TrimEnd('/')
    }
    return 'https://persona.getdoday.ru'
}

function Test-PersonaServerConnectivity {
    $Server = Get-PersonaServer
    $Curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($Curl) {
        & $Curl.Source `
            --silent `
            --show-error `
            --head `
            --max-time 8 `
            --output NUL `
            "$Server/auth/login"
        return $LASTEXITCODE -eq 0
    }
    try {
        Invoke-WebRequest `
            -Uri "$Server/auth/login" `
            -Method Head `
            -UseBasicParsing `
            -TimeoutSec 8 |
            Out-Null
        return $true
    } catch {
        # Any received HTTP response proves connectivity, even if the endpoint
        # rejects HEAD/auth. A transport failure has no response object.
        return $null -ne $_.Exception.Response
    }
}

function Invoke-TokenProvision {
    $VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
    Push-Location $RepoRoot
    try {
        if (Test-Path -LiteralPath $VenvPython) {
            & $VenvPython -m ops.provision_llm_worker_token
        } else {
            $Uv = Get-Command uv -ErrorAction SilentlyContinue
            if ($Uv) {
                & $Uv.Source run python -m ops.provision_llm_worker_token
            } else {
                $Python = Get-Command python -ErrorAction SilentlyContinue
                if (-not $Python) {
                    Write-Error 'No repo .venv, uv or python found; token was not rotated.'
                    exit 1
                }
                & $Python.Source -m ops.provision_llm_worker_token
            }
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Token provisioning failed with exit code $LASTEXITCODE."
            exit $LASTEXITCODE
        }
    } finally {
        Pop-Location
    }
}

function Show-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host "Task installed: $([bool]$task)"
    Write-Host "Durable worker token configured: $(Test-DurableWorkerToken)"
    Write-Host "Persona server reachable: $(Test-PersonaServerConnectivity)"
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "State: $($task.State)"
        Write-Host "Last run: $($info.LastRunTime)"
        Write-Host "Last result: $($info.LastTaskResult)"
        Write-Host "Next run: $($info.NextRunTime)"
    }
    try {
        $ollama = Invoke-WebRequest `
            -Uri 'http://127.0.0.1:11434/api/tags' `
            -UseBasicParsing `
            -TimeoutSec 2
        Write-Host "Ollama reachable: $($ollama.StatusCode -eq 200)"
    } catch {
        Write-Host 'Ollama reachable: False'
    }
}

if ($Status) {
    Show-Status
    exit 0
}

if ($Uninstall) {
    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction SilentlyContinue
    Write-Host "Removed Scheduled Task '$TaskName'."
    exit 0
}

if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Error "Launcher not found: $Launcher"
    exit 1
}

if ($ProvisionToken) {
    Write-Host 'Explicit token provisioning requested; rotating worker token...'
    Invoke-TokenProvision
}

if (-not (Test-DurableWorkerToken)) {
    Write-Error @"
PERSONA_WORKER_TOKEN is not configured in a durable location.
Add PERSONA_WORKER_TOKEN=... to:
  $EnvPath
Or rerun with -ProvisionToken. The token was not printed and no task was changed.
"@
    exit 2
}

if (-not (Test-PersonaServerConnectivity)) {
    Write-Warning @"
Persona server is not reachable from this PC right now.
The task will still be installed: the worker reconnects automatically.
If access is restricted in your network, configure HTTPS_PROXY/HTTP_PROXY
as described in docs/LLM_WORKER_WINDOWS.md.
"@
}

$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$ActionArgs = @(
    '-NoProfile'
    '-NonInteractive'
    '-WindowStyle Hidden'
    '-ExecutionPolicy Bypass'
    "-File `"$Launcher`""
) -join ' '

$Action = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument $ActionArgs `
    -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

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
    -Description 'Persona local LLM worker (outbound connection to Persona server)' |
    Out-Null

Write-Host "Installed Scheduled Task '$TaskName'."
Write-Host 'It will start automatically at the next user logon.'
if ($StartNow) {
    # Token was checked above; starting without it is deliberately impossible.
    Start-ScheduledTask -TaskName $TaskName
    Write-Host 'Started now.'
}
Write-Host ''
Write-Host 'Diagnostics:'
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Status"
