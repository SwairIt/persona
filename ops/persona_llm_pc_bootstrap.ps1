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
    [string]$ChatModel = '',
    [string]$EmbeddingModel = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$TaskName = 'PersonaLLMWorker'
$BrowserTaskName = 'PersonaBrowserWorker'
$InstallDir = Join-Path $env:LOCALAPPDATA 'persona-worker'
$Server = $Server.TrimEnd('/')

function Write-Step([string]$Message) {
    Write-Host "[Persona] $Message" -ForegroundColor Cyan
}

function Resolve-SystemProxy {
    foreach ($name in @('HTTPS_PROXY', 'HTTP_PROXY')) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value.Trim()
        }
    }

    try {
        $internetSettings = Get-ItemProperty `
            -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' `
            -ErrorAction Stop
        if ([int]$internetSettings.ProxyEnable -ne 1) { return '' }

        $proxyServer = [string]$internetSettings.ProxyServer
        if ([string]::IsNullOrWhiteSpace($proxyServer)) { return '' }
        $proxyServer = $proxyServer.Trim()

        if ($proxyServer.Contains(';')) {
            $byScheme = @{}
            foreach ($entry in $proxyServer.Split(';')) {
                $parts = $entry.Split('=', 2)
                if ($parts.Count -eq 2) {
                    $byScheme[$parts[0].Trim().ToLowerInvariant()] = $parts[1].Trim()
                }
            }
            if ($byScheme.ContainsKey('https')) {
                $proxyServer = [string]$byScheme['https']
            } elseif ($byScheme.ContainsKey('http')) {
                $proxyServer = [string]$byScheme['http']
            } else {
                return ''
            }
        }

        if ($proxyServer -notmatch '^[a-z][a-z0-9+.-]*://') {
            $proxyServer = "http://$proxyServer"
        }
        return $proxyServer
    } catch {
        return ''
    }
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

function Get-WorkerConfig([string]$Token) {
    try {
        $response = Invoke-PersonaWebRequest `
            -Uri "$Server/api/llm/worker/probe" `
            -Headers @{ 'X-Worker-Token' = $Token } `
            -TimeoutSec 15
        if ($response.StatusCode -ne 200) { return $null }
        return $response.Content | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Get-BrowserWorkerProbe([string]$Token) {
    try {
        $response = Invoke-PersonaWebRequest `
            -Uri "$Server/api/llm/worker/browser/probe" `
            -Headers @{ 'X-Worker-Token' = $Token } `
            -TimeoutSec 15
        if ($response.StatusCode -ne 200) { return $null }
        return $response.Content | ConvertFrom-Json
    } catch {
        return $null
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

if ([string]::IsNullOrWhiteSpace($Proxy)) {
    $Proxy = Resolve-SystemProxy
    if (-not [string]::IsNullOrWhiteSpace($Proxy)) {
        Write-Step 'Using the configured Windows or environment proxy.'
    }
}
if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
}
$env:NO_PROXY = '127.0.0.1,localhost,::1'

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
$browserToken = $env:PERSONA_BROWSER_WORKER_TOKEN
if ([string]::IsNullOrWhiteSpace($browserToken)) {
    $secureBrowserToken = Read-Host `
        'Paste only the PERSONA_BROWSER_WORKER_TOKEN value from the server .env' `
        -AsSecureString
    $browserTokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $secureBrowserToken
    )
    try {
        $browserToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            $browserTokenPointer
        )
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($browserTokenPointer)
    }
}
if ([string]::IsNullOrWhiteSpace($browserToken)) {
    throw 'PERSONA_BROWSER_WORKER_TOKEN cannot be empty.'
}

Write-Step 'Checking server access and worker token...'
$workerConfig = Get-WorkerConfig $token
if ($null -eq $workerConfig) {
    if ([string]::IsNullOrWhiteSpace($Proxy)) {
        $Proxy = Read-Host `
            'Connection failed. Enter proxy URL (for example http://127.0.0.1:8080), or press Enter to stop'
    }
    if (-not [string]::IsNullOrWhiteSpace($Proxy)) {
        $workerConfig = Get-WorkerConfig $token
    }
    if ($null -eq $workerConfig) {
        throw 'Cannot validate the server/token. Check PERSONA_WORKER_TOKEN, Internet access, and proxy settings.'
    }
}

Write-Step 'Checking scoped browser worker token...'
$browserProbe = Get-BrowserWorkerProbe $browserToken
if ($null -eq $browserProbe) {
    throw 'Cannot validate PERSONA_BROWSER_WORKER_TOKEN. Run the server token provisioner and retry.'
}

if ([string]::IsNullOrWhiteSpace($ChatModel)) {
    $ChatModel = [string]$workerConfig.chat_model
}
if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
    $EmbeddingModel = [string]$workerConfig.embedding_model
}
if ([string]::IsNullOrWhiteSpace($ChatModel)) { $ChatModel = 'qwen2.5:7b' }
if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
    $EmbeddingModel = 'nomic-embed-text'
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

Write-Step 'Installing the worker dependencies...'
& $python -m pip install `
    --disable-pip-version-check `
    --quiet `
    'httpx>=0.27,<1' `
    'playwright>=1.52,<2'
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed ($LASTEXITCODE)."
}

Write-Step 'Ensuring Playwright Chromium is installed...'
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed ($LASTEXITCODE)."
}

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

$browserWorkerPy = Join-Path $InstallDir 'persona_remote_browser_worker.py'
Write-Step 'Downloading the current Persona browser worker...'
Invoke-PersonaWebRequest `
    -Uri "$Server/api/llm/worker/browser/agent.py" `
    -OutFile $browserWorkerPy `
    -TimeoutSec 60 |
    Out-Null
if ((Get-Item -LiteralPath $browserWorkerPy).Length -lt 1000) {
    throw 'Downloaded browser worker file is unexpectedly small.'
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $currentIdentity.Name
$currentSid = $currentIdentity.User
if ($null -eq $currentSid) {
    throw 'Could not resolve the current Windows user SID.'
}
$identitySeed = "$currentUser|$env:COMPUTERNAME"
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $seedBytes = [Text.Encoding]::UTF8.GetBytes($identitySeed)
    $workerHash = [BitConverter]::ToString(
        $sha256.ComputeHash($seedBytes)
    ).Replace('-', '').ToLowerInvariant()
} finally {
    $sha256.Dispose()
}
$browserWorkerId = "persona-pc-$($workerHash.Substring(0, 24))"

$envLines = @(
    "PERSONA_SERVER=$Server",
    "PERSONA_WORKER_TOKEN=$token",
    "PERSONA_BROWSER_WORKER_TOKEN=$browserToken",
    'OLLAMA_URL=http://127.0.0.1:11434',
    "PERSONA_WORKER_MODEL=$ChatModel",
    "PERSONA_BROWSER_WORKER_ID=$browserWorkerId",
    'PERSONA_BROWSER_HEADLESS=false',
    'NO_PROXY=127.0.0.1,localhost,::1'
)
if ($Proxy) {
    $envLines += "HTTPS_PROXY=$Proxy"
    $envLines += "HTTP_PROXY=$Proxy"
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$envPath = Join-Path $InstallDir '.env'
[IO.File]::WriteAllLines($envPath, $envLines, $utf8NoBom)
$envAcl = New-Object Security.AccessControl.FileSecurity
$envAcl.SetOwner($currentSid)
$envAcl.SetAccessRuleProtection($true, $false)
$envRule = New-Object Security.AccessControl.FileSystemAccessRule(
    $currentSid,
    [Security.AccessControl.FileSystemRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
)
$envAcl.AddAccessRule($envRule)
Set-Acl -LiteralPath $envPath -AclObject $envAcl
$token = $null
$browserToken = $null
$env:PERSONA_WORKER_TOKEN = $null
$env:PERSONA_BROWSER_WORKER_TOKEN = $null

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

$escapedBrowserWorker = $browserWorkerPy.Replace("'", "''")
$browserLauncherPath = Join-Path $InstallDir 'persona_browser_worker_launcher.ps1'
$browserLauncher = @"
`$ErrorActionPreference = 'Continue'
`$python = '$escapedPython'
`$worker = '$escapedBrowserWorker'
`$log = Join-Path `$PSScriptRoot 'browser-worker.log'
while (`$true) {
    if ((Test-Path -LiteralPath `$log) -and (Get-Item -LiteralPath `$log).Length -gt 5242880) {
        Move-Item -LiteralPath `$log -Destination "`$log.previous" -Force
    }
    & `$python `$worker *>> `$log
    if (`$LASTEXITCODE -in @(2, 3)) { break }
    Start-Sleep -Seconds 3
}
"@
[IO.File]::WriteAllText($browserLauncherPath, $browserLauncher, $utf8NoBom)

function Install-PersonaTask {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Launcher,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $actionArgs = @(
        '-NoProfile',
        '-NonInteractive',
        '-WindowStyle Hidden',
        '-ExecutionPolicy Bypass',
        "-File `"$Launcher`""
    ) -join ' '
    $action = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument $actionArgs `
        -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
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
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $previousTask = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    $previousTaskXml = $null
    if ($null -ne $previousTask) {
        $previousTaskXml = Export-ScheduledTask -TaskName $Name
        if ($previousTask.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $Name
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                Start-Sleep -Milliseconds 500
                $state = (Get-ScheduledTask -TaskName $Name).State
                if ($state -ne 'Running') { break }
            }
            if ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') {
                throw "Could not stop the existing $Name task safely."
            }
        }
    }
    try {
        Register-ScheduledTask `
            -TaskName $Name `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Principal $principal `
            -Description $Description `
            -Force |
            Out-Null
    } catch {
        if (-not [string]::IsNullOrWhiteSpace($previousTaskXml)) {
            Register-ScheduledTask `
                -TaskName $Name `
                -Xml $previousTaskXml `
                -Force |
                Out-Null
        }
        throw
    }
    Start-ScheduledTask -TaskName $Name
}

Write-Step 'Installing per-user automatic startup...'
Install-PersonaTask `
    -Name $TaskName `
    -Launcher $launcherPath `
    -Description 'Persona local LLM worker (outbound-only)'
Install-PersonaTask `
    -Name $BrowserTaskName `
    -Launcher $browserLauncherPath `
    -Description 'Persona local browser worker (outbound-only)'
Start-Sleep -Seconds 5

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$browserTask = Get-ScheduledTask -TaskName $BrowserTaskName -ErrorAction Stop
$browserTaskInfo = Get-ScheduledTaskInfo -TaskName $BrowserTaskName -ErrorAction Stop
$ollamaReady = Wait-Ollama -Seconds 2

Write-Host ''
Write-Host 'Persona LLM setup completed.' -ForegroundColor Green
Write-Host "LLM task: $($task.State); last result: $($taskInfo.LastTaskResult)"
Write-Host "Browser task: $($browserTask.State); last result: $($browserTaskInfo.LastTaskResult)"
Write-Host "Ollama: $ollamaReady"
Write-Host "Runtime: $InstallDir"
Write-Host "Log: $(Join-Path $InstallDir 'worker.log')"
Write-Host "Browser log: $(Join-Path $InstallDir 'browser-worker.log')"
if ($task.State -ne 'Running' -or $browserTask.State -ne 'Running') {
    Write-Warning 'A task is installed but not running. Check its log; an invalid token exits with code 3.'
}
