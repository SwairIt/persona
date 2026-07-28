# Persona LLM PC bootstrap.
#
# Public entry point:
#   irm https://persona.getdoday.ru/api/llm/worker/bootstrap.ps1 | iex
#
# This script intentionally contains no credentials. It asks for the worker
# token without echoing it, installs only the minimal local runtime, registers
# a per-user Scheduled Task, starts it, and verifies the installation.

[CmdletBinding()]
param(
    [string]$Server = 'https://persona.getdoday.ru',
    [string]$Proxy = '',
    [string]$ChatModel = 'gemma3:4b',
    [string]$EmbeddingModel = 'nomic-embed-text'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$TaskName = 'PersonaLLMWorker'
$InstallDir = Join-Path $env:LOCALAPPDATA 'persona-worker'
$Server = $Server.TrimEnd('/')

function Write-Step([string]$Message) {
    Write-Host "[Persona] $Message" -ForegroundColor Cyan
}

function Invoke-PersonaWebRequest {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [string]$OutFile = '',
        [hashtable]$Headers = @{},
        [int]$TimeoutSec = 30
    )
    $params = @{
        Uri = $Uri
        UseBasicParsing = $true
        TimeoutSec = $TimeoutSec
        Headers = $Headers
    }
    if ($OutFile) { $params.OutFile = $OutFile }
    if ($script:Proxy) { $params.Proxy = $script:Proxy }
    Invoke-WebRequest @params
}

function Test-WorkerToken([string]$Token) {
    try {
        $response = Invoke-PersonaWebRequest `
            -Uri "$Server/api/llm/worker/probe" `
            -Headers @{ 'X-Worker-Token' = $Token } `
            -TimeoutSec 15
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable(
        'Path', [EnvironmentVariableTarget]::Machine
    )
    $userPath = [Environment]::GetEnvironmentVariable(
        'Path', [EnvironmentVariableTarget]::User
    )
    $env:Path = "$machinePath;$userPath"
}

function Resolve-Python {
    $candidates = @()
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $candidates += Get-ChildItem `
        -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe') `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        ForEach-Object { $_.FullName }
    $candidates += Get-ChildItem `
        -Path (Join-Path $env:ProgramFiles 'Python*\python.exe') `
        -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending |
        ForEach-Object { $_.FullName }
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        & $candidate -c "import sys; assert sys.version_info >= (3, 10)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}

function Resolve-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
        (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
        (Join-Path $env:ProgramFiles 'Ollama\ollama.exe')
    )
    return $candidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
}

function Require-Winget {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw 'winget is required to install a missing Python or Ollama. Install Microsoft App Installer and rerun the same command.'
    }
    return $winget.Source
}

function Wait-Ollama {
    param([int]$Seconds = 45)
    for ($attempt = 0; $attempt -lt $Seconds; $attempt++) {
        try {
            $response = Invoke-WebRequest `
                -Uri 'http://127.0.0.1:11434/api/tags' `
                -UseBasicParsing `
                -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
        Start-Sleep -Seconds 1
    }
    return $false
}

Write-Host ''
Write-Host 'Persona local LLM: automatic setup' -ForegroundColor Green
Write-Host 'The private repository is not required on this PC.'
Write-Host ''

$token = $env:PERSONA_WORKER_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    $secureToken = Read-Host `
        'Paste only the PERSONA_WORKER_TOKEN value from the server .env' `
        -AsSecureString
    $tokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPointer)
    }
}
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'PERSONA_WORKER_TOKEN cannot be empty.'
}

Write-Step 'Checking server access and worker token...'
if (-not (Test-WorkerToken $token)) {
    if ([string]::IsNullOrWhiteSpace($Proxy)) {
        $Proxy = Read-Host `
            'Connection failed. Enter proxy URL (for example http://127.0.0.1:8080), or press Enter to stop'
    }
    if ([string]::IsNullOrWhiteSpace($Proxy) -or -not (Test-WorkerToken $token)) {
        throw 'Cannot validate the server/token. Check PERSONA_WORKER_TOKEN, Internet access, and proxy settings.'
    }
}

if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
    $env:NO_PROXY = '127.0.0.1,localhost,::1'
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

$python = Resolve-Python
if (-not $python) {
    Write-Step 'Python 3.12 is missing; installing it with winget...'
    $winget = Require-Winget
    & $winget install `
        --id Python.Python.3.12 `
        --exact `
        --scope user `
        --silent `
        --accept-package-agreements `
        --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed ($LASTEXITCODE)." }
    Refresh-ProcessPath
    $python = Resolve-Python
}
if (-not $python) { throw 'Python 3.10+ was not found after installation.' }
Write-Step "Python: $python"

$ollama = Resolve-Ollama
if (-not $ollama) {
    Write-Step 'Ollama is missing; installing it with winget...'
    $winget = Require-Winget
    & $winget install `
        --id Ollama.Ollama `
        --exact `
        --silent `
        --accept-package-agreements `
        --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Ollama installation failed ($LASTEXITCODE)." }
    Refresh-ProcessPath
    $ollama = Resolve-Ollama
}
if (-not $ollama) { throw 'Ollama was not found after installation.' }
Write-Step "Ollama: $ollama"

Write-Step 'Installing the small Python dependency...'
& $python -m pip install `
    --disable-pip-version-check `
    --quiet `
    'httpx>=0.27,<1'
if ($LASTEXITCODE -ne 0) { throw "httpx installation failed ($LASTEXITCODE)." }

$workerPy = Join-Path $InstallDir 'persona_llm_worker.py'
Write-Step 'Downloading the current Persona worker...'
Invoke-PersonaWebRequest `
    -Uri "$Server/api/llm/worker/agent.py" `
    -OutFile $workerPy `
    -TimeoutSec 60 |
    Out-Null
if ((Get-Item -LiteralPath $workerPy).Length -lt 1000) {
    throw 'Downloaded worker file is unexpectedly small.'
}

$envLines = @(
    "PERSONA_SERVER=$Server",
    "PERSONA_WORKER_TOKEN=$token",
    'OLLAMA_URL=http://127.0.0.1:11434',
    "PERSONA_WORKER_MODEL=$ChatModel",
    'NO_PROXY=127.0.0.1,localhost,::1'
)
if ($Proxy) {
    $envLines += "HTTPS_PROXY=$Proxy"
    $envLines += "HTTP_PROXY=$Proxy"
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$envPath = Join-Path $InstallDir '.env'
[IO.File]::WriteAllLines($envPath, $envLines, $utf8NoBom)
$token = $null
$env:PERSONA_WORKER_TOKEN = $null

if (-not (Wait-Ollama -Seconds 2)) {
    Write-Step 'Starting Ollama in the background...'
    Start-Process `
        -FilePath $ollama `
        -ArgumentList 'serve' `
        -WindowStyle Hidden
}
if (-not (Wait-Ollama -Seconds 45)) {
    throw 'Ollama did not become ready on http://127.0.0.1:11434.'
}

Write-Step "Ensuring chat model is available: $ChatModel (this can take a while)..."
& $ollama pull $ChatModel
if ($LASTEXITCODE -ne 0) { throw "Could not pull $ChatModel." }
Write-Step "Ensuring embedding model is available: $EmbeddingModel..."
& $ollama pull $EmbeddingModel
if ($LASTEXITCODE -ne 0) { throw "Could not pull $EmbeddingModel." }

$escapedPython = $python.Replace("'", "''")
$escapedOllama = $ollama.Replace("'", "''")
$launcherPath = Join-Path $InstallDir 'persona_llm_worker_launcher.ps1'
$launcher = @"
`$ErrorActionPreference = 'Continue'
`$env:PERSONA_REPO = `$PSScriptRoot
`$python = '$escapedPython'
`$ollama = '$escapedOllama'
`$worker = Join-Path `$PSScriptRoot 'persona_llm_worker.py'
`$log = Join-Path `$PSScriptRoot 'worker.log'
try {
    Invoke-WebRequest -Uri 'http://127.0.0.1:11434/api/tags' -UseBasicParsing -TimeoutSec 2 | Out-Null
} catch {
    Start-Process -FilePath `$ollama -ArgumentList 'serve' -WindowStyle Hidden
    Start-Sleep -Seconds 5
}
while (`$true) {
    if ((Test-Path -LiteralPath `$log) -and (Get-Item -LiteralPath `$log).Length -gt 5242880) {
        Move-Item -LiteralPath `$log -Destination "`$log.previous" -Force
    }
    & `$python `$worker *>> `$log
    if (`$LASTEXITCODE -in @(2, 3)) { break }
    Start-Sleep -Seconds 3
}
"@
[IO.File]::WriteAllText($launcherPath, $launcher, $utf8NoBom)

Write-Step 'Installing per-user automatic startup...'
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$actionArgs = @(
    '-NoProfile',
    '-NonInteractive',
    '-WindowStyle Hidden',
    '-ExecutionPolicy Bypass',
    "-File `"$launcherPath`""
) -join ' '
$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument $actionArgs `
    -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -Hidden
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited
Unregister-ScheduledTask `
    -TaskName $TaskName `
    -Confirm:$false `
    -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Persona local LLM worker (outbound-only)' |
    Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$ollamaReady = Wait-Ollama -Seconds 2

Write-Host ''
Write-Host 'Persona LLM setup completed.' -ForegroundColor Green
Write-Host "Task: $($task.State); last result: $($taskInfo.LastTaskResult)"
Write-Host "Ollama: $ollamaReady"
Write-Host "Runtime: $InstallDir"
Write-Host "Log: $(Join-Path $InstallDir 'worker.log')"
if ($task.State -ne 'Running') {
    Write-Warning 'The task is installed but not running. Check worker.log; an invalid token exits with code 3.'
}
