# Persona outbound worker bootstrap for a Windows PC.
#
# Public entry point:
#   irm https://persona.getdoday.ru/api/llm/worker/bootstrap.ps1 | iex
#
# A one-use owner ticket creates pending credentials. They are first stored in
# an owner-only .env.next file. Existing workers and .env remain untouched
# while Python, Playwright, Ollama, models, and worker scripts are prepared.

[CmdletBinding()]
param(
    [string]$Server = 'https://persona.getdoday.ru',
    [string]$Proxy = '',
    [string]$ChatModel = '',
    [string]$EmbeddingModel = '',
    [string]$EnrollmentTicket = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$TaskName = 'PersonaLLMWorker'
$BrowserTaskName = 'PersonaBrowserWorker'
$InstallDir = Join-Path $env:LOCALAPPDATA 'persona-worker'
$Server = $Server.TrimEnd('/')
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

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
        $settings = Get-ItemProperty `
            -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' `
            -ErrorAction Stop
        if ([int]$settings.ProxyEnable -ne 1) { return '' }
        $proxyServer = ([string]$settings.ProxyServer).Trim()
        if ([string]::IsNullOrWhiteSpace($proxyServer)) { return '' }
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
        [string]$Method = 'GET',
        [string]$Body = '',
        [string]$ContentType = '',
        [int]$TimeoutSec = 30
    )
    $params = @{
        Uri = $Uri
        UseBasicParsing = $true
        TimeoutSec = $TimeoutSec
        Headers = $Headers
        Method = $Method
    }
    if ($OutFile) { $params.OutFile = $OutFile }
    if ($Body) { $params.Body = $Body }
    if ($ContentType) { $params.ContentType = $ContentType }
    if ($script:Proxy) { $params.Proxy = $script:Proxy }
    Invoke-WebRequest @params
}

function Exchange-EnrollmentTicket([string]$Ticket, [string]$WorkerId) {
    $payload = $null
    try {
        $payload = @{
            phase = 'exchange'
            ticket = $Ticket
            worker_id = $WorkerId
        } | ConvertTo-Json -Compress
        $response = Invoke-PersonaWebRequest `
            -Uri "$Server/api/llm/worker/enrollment" `
            -Method 'POST' `
            -Body $payload `
            -ContentType 'application/json' `
            -TimeoutSec 30
        if ($response.StatusCode -ne 200) { return $null }
        return $response.Content | ConvertFrom-Json
    } catch {
        return $null
    } finally {
        $payload = $null
    }
}

function Activate-Enrollment {
    param(
        [Parameter(Mandatory = $true)][int]$EnrollmentId,
        [Parameter(Mandatory = $true)][string]$WorkerId,
        [Parameter(Mandatory = $true)][string]$LlmToken,
        [Parameter(Mandatory = $true)][string]$BrowserToken
    )
    $payload = $null
    try {
        $payload = @{
            phase = 'activate'
            enrollment_id = $EnrollmentId
            worker_id = $WorkerId
            llm_worker_token = $LlmToken
            browser_worker_token = $BrowserToken
        } | ConvertTo-Json -Compress
        for ($attempt = 1; $attempt -le 4; $attempt++) {
            try {
                $response = Invoke-PersonaWebRequest `
                    -Uri "$Server/api/llm/worker/enrollment" `
                    -Method 'POST' `
                    -Body $payload `
                    -ContentType 'application/json' `
                    -TimeoutSec 30
                if ($response.StatusCode -eq 200) {
                    return $response.Content | ConvertFrom-Json
                }
            } catch {
                if ($attempt -lt 4) { Start-Sleep -Seconds (2 * $attempt) }
            }
        }
        return $null
    } finally {
        $payload = $null
    }
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

function Read-DotEnv([string]$Path) {
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            continue
        }
        $values[$Matches[1]] = $Matches[2]
    }
    return $values
}

function Merge-DotEnvLines {
    param(
        [string]$BasePath,
        [Parameter(Mandatory = $true)][hashtable]$Updates
    )
    $lines = @()
    if ($BasePath -and (Test-Path -LiteralPath $BasePath)) {
        $lines = @([IO.File]::ReadAllLines($BasePath))
    }
    $written = @{}
    $result = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $key = $Matches[1]
            if ($Updates.ContainsKey($key)) {
                if (-not $written.ContainsKey($key)) {
                    $value = [string]$Updates[$key]
                    if ($value.Contains("`r") -or $value.Contains("`n")) {
                        throw "Invalid newline in $key."
                    }
                    $result.Add("$key=$value")
                    $written[$key] = $true
                }
                continue
            }
        }
        $result.Add($line)
    }
    foreach ($key in $Updates.Keys | Sort-Object) {
        if ($written.ContainsKey($key)) { continue }
        $value = [string]$Updates[$key]
        if ($value.Contains("`r") -or $value.Contains("`n")) {
            throw "Invalid newline in $key."
        }
        $result.Add("$key=$value")
    }
    return $result.ToArray()
}

function New-OwnerOnlyAcl(
    [Security.Principal.SecurityIdentifier]$Sid
) {
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($Sid)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $Sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    return $acl
}

function Write-OwnerOnlyAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$Lines,
        [Parameter(Mandatory = $true)]
        [Security.Principal.SecurityIdentifier]$Sid
    )
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $temporary = Join-Path `
        $directory `
        (".{0}.{1}.tmp" -f (Split-Path -Leaf $Path), [Guid]::NewGuid())
    try {
        [IO.File]::WriteAllBytes($temporary, [byte[]]@())
        $acl = New-OwnerOnlyAcl $Sid
        Set-Acl -LiteralPath $temporary -AclObject $acl
        [IO.File]::WriteAllLines($temporary, $Lines, $script:Utf8NoBom)
        if (Test-Path -LiteralPath $Path) {
            Set-Acl -LiteralPath $Path -AclObject (New-OwnerOnlyAcl $Sid)
            [IO.File]::Replace($temporary, $Path, $null, $true)
        } else {
            [IO.File]::Move($temporary, $Path)
        }
        Set-Acl -LiteralPath $Path -AclObject (New-OwnerOnlyAcl $Sid)
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Promote-Atomic {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string]$Backup = ''
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Staged file is missing: $Source"
    }
    if (Test-Path -LiteralPath $Destination) {
        $backupPath = if ($Backup) { $Backup } else { $null }
        [IO.File]::Replace($Source, $Destination, $backupPath, $true)
    } else {
        [IO.File]::Move($Source, $Destination)
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
        throw 'winget is required. Install Microsoft App Installer and rerun.'
    }
    return $winget.Source
}

function Wait-Ollama([int]$Seconds = 45) {
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

function Wait-WorkerHeartbeats {
    param(
        [Parameter(Mandatory = $true)][string]$LlmHeartbeat,
        [Parameter(Mandatory = $true)][string]$BrowserHeartbeat,
        [Parameter(Mandatory = $true)][datetime]$NotBefore,
        [int]$Seconds = 90
    )
    for ($attempt = 0; $attempt -lt $Seconds; $attempt++) {
        $llmReady = (
            (Test-Path -LiteralPath $LlmHeartbeat) -and
            (Get-Item -LiteralPath $LlmHeartbeat).LastWriteTimeUtc -ge $NotBefore
        )
        $browserReady = (
            (Test-Path -LiteralPath $BrowserHeartbeat) -and
            (Get-Item -LiteralPath $BrowserHeartbeat).LastWriteTimeUtc -ge $NotBefore
        )
        if ($llmReady -and $browserReady) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Register-PersonaTask {
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
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force |
        Out-Null
}

function Install-PersonaTasksAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$LlmLauncher,
        [Parameter(Mandatory = $true)][string]$BrowserLauncher,
        [Parameter(Mandatory = $true)][string]$LlmHeartbeat,
        [Parameter(Mandatory = $true)][string]$BrowserHeartbeat
    )
    $names = @($TaskName, $BrowserTaskName)
    $previousXml = @{}
    $previousRunning = @{}
    foreach ($name in $names) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            $previousXml[$name] = Export-ScheduledTask -TaskName $name
            $previousRunning[$name] = ($task.State -eq 'Running')
        } else {
            $previousRunning[$name] = $false
        }
    }
    try {
        foreach ($name in $names) {
            if (-not $previousRunning[$name]) { continue }
            Stop-ScheduledTask -TaskName $name
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                Start-Sleep -Milliseconds 500
                $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                if ($null -eq $task -or $task.State -ne 'Running') { break }
            }
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            if ($null -ne $task -and $task.State -eq 'Running') {
                throw "Could not stop the existing $name task safely."
            }
        }
        Register-PersonaTask `
            -Name $TaskName `
            -Launcher $LlmLauncher `
            -Description 'Persona local LLM worker (outbound-only)'
        Register-PersonaTask `
            -Name $BrowserTaskName `
            -Launcher $BrowserLauncher `
            -Description 'Persona local browser worker (outbound-only)'
        foreach ($heartbeat in @($LlmHeartbeat, $BrowserHeartbeat)) {
            if (Test-Path -LiteralPath $heartbeat) {
                Remove-Item -LiteralPath $heartbeat -Force
            }
        }
        $startedAt = [DateTime]::UtcNow.AddSeconds(-1)
        Start-ScheduledTask -TaskName $TaskName
        Start-ScheduledTask -TaskName $BrowserTaskName
        if (-not (Wait-WorkerHeartbeats `
            -LlmHeartbeat $LlmHeartbeat `
            -BrowserHeartbeat $BrowserHeartbeat `
            -NotBefore $startedAt
        )) {
            throw 'Workers started but did not complete authenticated polls.'
        }
    } catch {
        $installError = $_
        $rollbackErrors = New-Object System.Collections.Generic.List[string]
        foreach ($name in $names) {
            try {
                $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                if ($null -ne $task -and $task.State -eq 'Running') {
                    Stop-ScheduledTask -TaskName $name
                    for ($attempt = 0; $attempt -lt 30; $attempt++) {
                        Start-Sleep -Milliseconds 500
                        $task = Get-ScheduledTask `
                            -TaskName $name `
                            -ErrorAction SilentlyContinue
                        if ($null -eq $task -or $task.State -ne 'Running') {
                            break
                        }
                    }
                }
            } catch {
                $rollbackErrors.Add("stop $name`: $($_.Exception.Message)")
            }
        }
        foreach ($name in $names) {
            try {
                if ($previousXml.ContainsKey($name)) {
                    Register-ScheduledTask `
                        -TaskName $name `
                        -Xml $previousXml[$name] `
                        -Force |
                        Out-Null
                } else {
                    $task = Get-ScheduledTask `
                        -TaskName $name `
                        -ErrorAction SilentlyContinue
                    if ($null -ne $task) {
                        Unregister-ScheduledTask `
                            -TaskName $name `
                            -Confirm:$false
                    }
                }
            } catch {
                $rollbackErrors.Add("restore $name`: $($_.Exception.Message)")
            }
        }
        foreach ($name in $names) {
            try {
                if ($previousRunning[$name]) {
                    Start-ScheduledTask -TaskName $name
                }
            } catch {
                $rollbackErrors.Add("restart $name`: $($_.Exception.Message)")
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            Write-Warning (
                'Task rollback had errors: ' + ($rollbackErrors -join '; ')
            )
        }
        throw $installError
    }
}

Write-Host ''
Write-Host 'Persona local agents: safe automatic setup' -ForegroundColor Green
Write-Host 'The private repository is not required on this PC.'
Write-Host ''

if ([string]::IsNullOrWhiteSpace($Proxy)) {
    $Proxy = Resolve-SystemProxy
    if ($Proxy) { Write-Step 'Using the configured system proxy.' }
}
if ($Proxy) {
    $env:HTTPS_PROXY = $Proxy
    $env:HTTP_PROXY = $Proxy
}
$env:NO_PROXY = '127.0.0.1,localhost,::1'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $identity.Name
$currentSid = $identity.User
if ($null -eq $currentSid) {
    throw 'Could not resolve the current Windows user SID.'
}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$envPath = Join-Path $InstallDir '.env'
$nextEnvPath = Join-Path $InstallDir '.env.next'
$llmHeartbeatPath = Join-Path $InstallDir 'llm-poll.heartbeat'
$browserHeartbeatPath = Join-Path $InstallDir 'browser-poll.heartbeat'

$seed = "$currentUser|$env:COMPUTERNAME"
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($seed))
    $workerHash = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
} finally {
    $sha256.Dispose()
}
$workerId = "persona-pc-$($workerHash.Substring(0, 24))"

$ticket = $EnrollmentTicket
$EnrollmentTicket = $null
$token = ''
$browserToken = ''
$enrollmentId = 0
$activationExpiresAt = ''
$resumeMap = @{}
$resumeSource = ''

if (-not [string]::IsNullOrWhiteSpace($ticket)) {
    Write-Step 'Exchanging the supplied one-use enrollment ticket...'
    try {
        $enrollment = Exchange-EnrollmentTicket $ticket $workerId
    } finally {
        $ticket = $null
    }
    if ($null -eq $enrollment) {
        throw 'Enrollment exchange failed. Check server access and ticket age.'
    }
    $token = [string]$enrollment.llm_worker_token
    $browserToken = [string]$enrollment.browser_worker_token
    $enrollmentId = [int]$enrollment.enrollment_id
    $activationExpiresAt = [string]$enrollment.activation_expires_at
    if ([string]::IsNullOrWhiteSpace($ChatModel)) {
        $ChatModel = [string]$enrollment.chat_model
    }
    if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
        $EmbeddingModel = [string]$enrollment.embedding_model
    }
    $enrollment = $null
} else {
    foreach ($candidate in @($nextEnvPath, $envPath)) {
        $candidateMap = Read-DotEnv $candidate
        $candidateId = 0
        [void][int]::TryParse(
            [string]$candidateMap['PERSONA_ENROLLMENT_ID'],
            [ref]$candidateId
        )
        if (
            $candidateId -gt 0 -and
            -not [string]::IsNullOrWhiteSpace(
                [string]$candidateMap['PERSONA_WORKER_TOKEN']
            ) -and
            -not [string]::IsNullOrWhiteSpace(
                [string]$candidateMap['PERSONA_BROWSER_WORKER_TOKEN']
            )
        ) {
            $resumeMap = $candidateMap
            $resumeSource = $candidate
            $enrollmentId = $candidateId
            $token = [string]$candidateMap['PERSONA_WORKER_TOKEN']
            $browserToken = [string]$candidateMap['PERSONA_BROWSER_WORKER_TOKEN']
            $workerId = [string]$candidateMap['PERSONA_ENROLLMENT_WORKER_ID']
            $activationExpiresAt = [string]$candidateMap[
                'PERSONA_ACTIVATION_EXPIRES_AT'
            ]
            Write-Step 'Resuming a durably saved pending enrollment.'
            break
        }
    }
}

if ($enrollmentId -eq 0) {
    $activeMap = Read-DotEnv $envPath
    $token = [string]$env:PERSONA_WORKER_TOKEN
    $browserToken = [string]$env:PERSONA_BROWSER_WORKER_TOKEN
    if (
        [string]::IsNullOrWhiteSpace($token) -or
        [string]::IsNullOrWhiteSpace($browserToken)
    ) {
        $token = [string]$activeMap['PERSONA_WORKER_TOKEN']
        $browserToken = [string]$activeMap['PERSONA_BROWSER_WORKER_TOKEN']
    }
    if (
        [string]::IsNullOrWhiteSpace($token) -or
        [string]::IsNullOrWhiteSpace($browserToken)
    ) {
        Write-Step 'A short-lived owner enrollment ticket is required.'
        $secureTicket = Read-Host `
            'Paste the one-use ticket from Settings > Automation' `
            -AsSecureString
        $ticketPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
            $secureTicket
        )
        try {
            $ticket = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
                $ticketPointer
            )
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ticketPointer)
            $secureTicket = $null
        }
        try {
            if ([string]::IsNullOrWhiteSpace($ticket)) {
                throw 'Enrollment ticket cannot be empty.'
            }
            $enrollment = Exchange-EnrollmentTicket $ticket $workerId
        } finally {
            $ticket = $null
        }
        if ($null -eq $enrollment) {
            throw 'Enrollment exchange failed. Check server access and ticket age.'
        }
        $token = [string]$enrollment.llm_worker_token
        $browserToken = [string]$enrollment.browser_worker_token
        $enrollmentId = [int]$enrollment.enrollment_id
        $activationExpiresAt = [string]$enrollment.activation_expires_at
        if ([string]::IsNullOrWhiteSpace($ChatModel)) {
            $ChatModel = [string]$enrollment.chat_model
        }
        if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
            $EmbeddingModel = [string]$enrollment.embedding_model
        }
        $enrollment = $null
    }
}
$ticket = $null

if (
    [string]::IsNullOrWhiteSpace($token) -or
    [string]::IsNullOrWhiteSpace($browserToken)
) {
    throw 'Both scoped worker credentials are required.'
}
if ([string]::IsNullOrWhiteSpace($workerId)) {
    throw 'Pending enrollment is missing its bound worker id.'
}

if ($enrollmentId -eq 0) {
    Write-Step 'Validating the existing active credentials...'
    $workerConfig = Get-WorkerConfig $token
    $browserProbe = Get-BrowserWorkerProbe $browserToken
    if ($null -eq $workerConfig -or $null -eq $browserProbe) {
        throw 'Existing credentials are invalid or the server is unreachable.'
    }
    if ([string]::IsNullOrWhiteSpace($ChatModel)) {
        $ChatModel = [string]$workerConfig.chat_model
    }
    if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
        $EmbeddingModel = [string]$workerConfig.embedding_model
    }
}
if ([string]::IsNullOrWhiteSpace($ChatModel)) {
    if ($resumeMap.ContainsKey('PERSONA_WORKER_MODEL')) {
        $ChatModel = [string]$resumeMap['PERSONA_WORKER_MODEL']
    }
}
if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
    if ($resumeMap.ContainsKey('PERSONA_EMBEDDING_MODEL')) {
        $EmbeddingModel = [string]$resumeMap['PERSONA_EMBEDDING_MODEL']
    }
}
if ([string]::IsNullOrWhiteSpace($ChatModel)) { $ChatModel = 'qwen2.5:3b' }
if ([string]::IsNullOrWhiteSpace($EmbeddingModel)) {
    $EmbeddingModel = 'nomic-embed-text'
}

$updates = @{
    PERSONA_SERVER = $Server
    PERSONA_WORKER_TOKEN = $token
    PERSONA_BROWSER_WORKER_TOKEN = $browserToken
    PERSONA_WORKER_MODEL = $ChatModel
    PERSONA_EMBEDDING_MODEL = $EmbeddingModel
    PERSONA_WORKER_ID = $workerId
    PERSONA_BROWSER_WORKER_ID = $workerId
    PERSONA_WORKER_HEARTBEAT_FILE = $llmHeartbeatPath
    PERSONA_BROWSER_HEARTBEAT_FILE = $browserHeartbeatPath
    NO_PROXY = '127.0.0.1,localhost,::1'
}
$baseMap = Read-DotEnv $envPath
if (-not $baseMap.ContainsKey('OLLAMA_URL')) {
    $updates['OLLAMA_URL'] = 'http://127.0.0.1:11434'
}
if (-not $baseMap.ContainsKey('PERSONA_BROWSER_HEADLESS')) {
    $updates['PERSONA_BROWSER_HEADLESS'] = 'false'
}
if ($Proxy) {
    $updates['HTTPS_PROXY'] = $Proxy
    $updates['HTTP_PROXY'] = $Proxy
}
if ($enrollmentId -gt 0) {
    $updates['PERSONA_ENROLLMENT_ID'] = [string]$enrollmentId
    $updates['PERSONA_ENROLLMENT_WORKER_ID'] = $workerId
    $updates['PERSONA_ACTIVATION_EXPIRES_AT'] = $activationExpiresAt
}
$basePath = if ($resumeSource) { $resumeSource } else { $envPath }
$nextLines = Merge-DotEnvLines -BasePath $basePath -Updates $updates
Write-OwnerOnlyAtomic -Path $nextEnvPath -Lines $nextLines -Sid $currentSid
Write-Step 'Pending configuration is durably saved with owner-only ACL.'

$python = Resolve-Python
if (-not $python) {
    Write-Step 'Installing Python 3.12...'
    $winget = Require-Winget
    & $winget install `
        --id Python.Python.3.12 `
        --exact `
        --scope user `
        --silent `
        --accept-package-agreements `
        --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Python installation failed ($LASTEXITCODE)."
    }
    Refresh-ProcessPath
    $python = Resolve-Python
}
if (-not $python) { throw 'Python 3.10+ was not found after installation.' }

$ollama = Resolve-Ollama
if (-not $ollama) {
    Write-Step 'Installing Ollama...'
    $winget = Require-Winget
    & $winget install `
        --id Ollama.Ollama `
        --exact `
        --silent `
        --accept-package-agreements `
        --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "Ollama installation failed ($LASTEXITCODE)."
    }
    Refresh-ProcessPath
    $ollama = Resolve-Ollama
}
if (-not $ollama) { throw 'Ollama was not found after installation.' }

Write-Step 'Installing worker dependencies and Playwright Chromium...'
& $python -m pip install `
    --disable-pip-version-check `
    --quiet `
    'httpx>=0.27,<1' `
    'playwright>=1.52,<2'
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency installation failed ($LASTEXITCODE)."
}
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Playwright Chromium installation failed ($LASTEXITCODE)."
}

$workerPy = Join-Path $InstallDir 'persona_llm_worker.py'
$workerPyNext = "$workerPy.next"
$browserWorkerPy = Join-Path $InstallDir 'persona_remote_browser_worker.py'
$browserWorkerPyNext = "$browserWorkerPy.next"
Write-Step 'Downloading staged worker scripts...'
Invoke-PersonaWebRequest `
    -Uri "$Server/api/llm/worker/agent.py" `
    -OutFile $workerPyNext `
    -TimeoutSec 60 |
    Out-Null
Invoke-PersonaWebRequest `
    -Uri "$Server/api/llm/worker/browser/agent.py" `
    -OutFile $browserWorkerPyNext `
    -TimeoutSec 60 |
    Out-Null
if ((Get-Item -LiteralPath $workerPyNext).Length -lt 1000) {
    throw 'Downloaded LLM worker is unexpectedly small.'
}
if ((Get-Item -LiteralPath $browserWorkerPyNext).Length -lt 1000) {
    throw 'Downloaded browser worker is unexpectedly small.'
}
& $python -m py_compile $workerPyNext $browserWorkerPyNext
if ($LASTEXITCODE -ne 0) { throw 'Downloaded worker preflight failed.' }

if (-not (Wait-Ollama -Seconds 2)) {
    Write-Step 'Starting Ollama in the background...'
    Start-Process `
        -FilePath $ollama `
        -ArgumentList 'serve' `
        -WindowStyle Hidden
}
if (-not (Wait-Ollama -Seconds 45)) {
    throw 'Ollama did not become ready on 127.0.0.1:11434.'
}
Write-Step "Ensuring chat model is available: $ChatModel"
& $ollama pull $ChatModel
if ($LASTEXITCODE -ne 0) { throw "Could not pull $ChatModel." }
Write-Step "Ensuring embedding model is available: $EmbeddingModel"
& $ollama pull $EmbeddingModel
if ($LASTEXITCODE -ne 0) { throw "Could not pull $EmbeddingModel." }

$escapedPython = $python.Replace("'", "''")
$escapedOllama = $ollama.Replace("'", "''")
$launcherPath = Join-Path $InstallDir 'persona_llm_worker_launcher.ps1'
$launcherNext = "$launcherPath.next"
$browserLauncherPath = Join-Path $InstallDir 'persona_browser_worker_launcher.ps1'
$browserLauncherNext = "$browserLauncherPath.next"
$dotenvLoader = @'
$envFile = Join-Path $PSScriptRoot '.env'
foreach ($line in [IO.File]::ReadAllLines($envFile)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
        Set-Item -Path ("Env:" + $Matches[1]) -Value $Matches[2]
    }
}
'@
$launcher = @"
`$ErrorActionPreference = 'Continue'
$dotenvLoader
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
$browserLauncher = @"
`$ErrorActionPreference = 'Continue'
$dotenvLoader
`$python = '$escapedPython'
`$worker = Join-Path `$PSScriptRoot 'persona_remote_browser_worker.py'
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
[IO.File]::WriteAllText($launcherNext, $launcher, $Utf8NoBom)
[IO.File]::WriteAllText($browserLauncherNext, $browserLauncher, $Utf8NoBom)

# Heavy preparation is complete. Only now replace live runtime files.
Promote-Atomic -Source $workerPyNext -Destination $workerPy
Promote-Atomic -Source $browserWorkerPyNext -Destination $browserWorkerPy
Promote-Atomic -Source $launcherNext -Destination $launcherPath
Promote-Atomic -Source $browserLauncherNext -Destination $browserLauncherPath
$envBackup = ''
if (Test-Path -LiteralPath $envPath) {
    Set-Acl -LiteralPath $envPath -AclObject (New-OwnerOnlyAcl $currentSid)
    $envBackup = Join-Path `
        $InstallDir `
        ('.env.persona-worker.{0}.{1}.bak' -f `
            [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff'), `
            [Guid]::NewGuid().ToString('N'))
}
Promote-Atomic `
    -Source $nextEnvPath `
    -Destination $envPath `
    -Backup $envBackup
Set-Acl -LiteralPath $envPath -AclObject (New-OwnerOnlyAcl $currentSid)

if ($enrollmentId -gt 0) {
    Write-Step 'Activating the durably installed credentials...'
    $activation = Activate-Enrollment `
        -EnrollmentId $enrollmentId `
        -WorkerId $workerId `
        -LlmToken $token `
        -BrowserToken $browserToken
    if ($null -eq $activation) {
        throw 'Activation could not be confirmed. Rerun this command to resume safely.'
    }
    $activation = $null
}

Write-Step 'Running post-activation credential probes...'
$workerConfig = Get-WorkerConfig $token
$browserProbe = Get-BrowserWorkerProbe $browserToken
if ($null -eq $workerConfig -or $null -eq $browserProbe) {
    throw 'Post-activation probes failed. Rerun to verify the installed state.'
}

$token = $null
$browserToken = $null
$env:PERSONA_WORKER_TOKEN = $null
$env:PERSONA_BROWSER_WORKER_TOKEN = $null

Write-Step 'Installing both per-user startup tasks atomically...'
Install-PersonaTasksAtomically `
    -LlmLauncher $launcherPath `
    -BrowserLauncher $browserLauncherPath `
    -LlmHeartbeat $llmHeartbeatPath `
    -BrowserHeartbeat $browserHeartbeatPath

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
$browserTask = Get-ScheduledTask -TaskName $BrowserTaskName -ErrorAction Stop
$browserTaskInfo = Get-ScheduledTaskInfo -TaskName $BrowserTaskName -ErrorAction Stop

Write-Host ''
Write-Host 'Persona local agents are installed and polling.' -ForegroundColor Green
Write-Host "LLM task: $($task.State); last result: $($taskInfo.LastTaskResult)"
Write-Host "Browser task: $($browserTask.State); last result: $($browserTaskInfo.LastTaskResult)"
Write-Host "Runtime: $InstallDir"
Write-Host "LLM log: $(Join-Path $InstallDir 'worker.log')"
Write-Host "Browser log: $(Join-Path $InstallDir 'browser-worker.log')"
