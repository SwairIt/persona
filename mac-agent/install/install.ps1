<#
  Persona agent — Windows installer (manual / from a cloned repo).

  Mirrors install.sh: detect Python 3.11+, create a venv under mac-agent\.venv,
  install the light deps (+audio with -Voice), write %APPDATA%\Persona\config.toml,
  register a per-user Scheduled Task "PersonaAgent" (logon trigger, pythonw → no
  console window, restart on crash), and start it.

  Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 `
        -Server https://your-server -Token <agent_token> [-DeviceToken <tok>] [-Voice]

  Re-running upgrades in place (idempotent updater).
#>
[CmdletBinding()]
param(
    [string]$Server,
    [string]$Token,
    [string]$DeviceToken,
    [switch]$Voice,
    [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'

function Info($m) { Write-Host "[persona-agent] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[persona-agent] $m" -ForegroundColor Yellow }
function Die ($m) { Write-Host "[persona-agent] $m" -ForegroundColor Red; exit 1 }

# --- paths -----------------------------------------------------------------
$AgentDir = Split-Path -Parent $PSScriptRoot          # ...\mac-agent
$VenvDir  = Join-Path $AgentDir '.venv'
$CfgDir   = Join-Path $env:APPDATA 'Persona'
$CfgPath  = Join-Path $CfgDir 'config.toml'

# --- 1. Python 3.11+ -------------------------------------------------------
function Find-Python {
    foreach ($c in @('py -3.13','py -3.12','py -3.11','python','python3')) {
        $parts = $c.Split(' ')
        $exe = $parts[0]
        try {
            $ver = & $exe $parts[1..($parts.Length-1)] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and $ver) {
            $mm = $ver.Trim().Split('.')
            if ([int]$mm[0] -eq 3 -and [int]$mm[1] -ge 11) { return ,@($exe, $parts[1..($parts.Length-1)]) }
        }
    }
    return $null
}
$py = Find-Python
if ($null -eq $py) {
    Die "Нужен Python 3.11+. Поставь: winget install Python.Python.3.12  (или python.org), потом перезапусти."
}
$pyExe = $py[0]; $pyArgs = $py[1]
Info "Python: $pyExe $pyArgs"

# --- 2. venv ---------------------------------------------------------------
if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
    Info "Создаю venv → $VenvDir"
    & $pyExe @pyArgs -m venv $VenvDir
}
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'

# --- 3. deps (light core; +audio opt-in) -----------------------------------
Info "Ставлю зависимости (это может занять минуту)…"
& $VenvPy -m pip install --upgrade --quiet pip wheel
$core = @('httpx>=0.28','mss>=10.0','pillow>=11.0','imagehash>=4.3','numpy>=1.26',
          'click>=8.1','structlog>=24.4','pydantic>=2.10','pydantic-settings>=2.7')
& $VenvPy -m pip install --quiet @core
if ($Voice) {
    Info "Голос/аудио включены → ставлю sounddevice, webrtcvad, scipy"
    & $VenvPy -m pip install --quiet 'sounddevice>=0.5' 'webrtcvad-wheels>=2.0' 'scipy>=1.13'
}

# --- 4. ffmpeg (для opus-аудио) -------------------------------------------
if ($Voice -and -not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Warn "ffmpeg не найден — пробую winget install Gyan.FFmpeg (нужен для записи звука)…"
    try { winget install --silent --accept-source-agreements --accept-package-agreements Gyan.FFmpeg } catch { Warn "не удалось — поставь ffmpeg вручную, иначе аудио не будет." }
}

# --- 5. server/token (prompt if missing) -----------------------------------
if (-not $Server -and -not $NonInteractive) { $Server = Read-Host 'URL сервера Persona (https://...)' }
if (-not $Token  -and -not $NonInteractive) { $Token  = Read-Host 'Agent token (со страницы установки)' }
if (-not $Server) { Die "Не задан -Server" }
if (-not $Token)  { Die "Не задан -Token" }
$Server = $Server.TrimEnd('/')

# --- 6. config.toml --------------------------------------------------------
New-Item -ItemType Directory -Force -Path $CfgDir | Out-Null
$audioFlag = if ($Voice) { 'true' } else { 'false' }
$lines = @(
    '# Persona agent (Windows). Не редактируй вручную без нужды.',
    '',
    '[server]',
    ('url   = "{0}"' -f $Server),
    ('token = "{0}"' -f $Token)
)
if ($DeviceToken) { $lines += ('device_token = "{0}"' -f $DeviceToken) }
$lines += @('', '[agent]', ('hostname = "{0}"' -f $env:COMPUTERNAME),
            '', '[capture]', 'screen = true', ('audio  = {0}' -f $audioFlag),
            '', '[logging]', 'level = "INFO"')
Set-Content -Path $CfgPath -Value ($lines -join "`n") -Encoding utf8
try { icacls $CfgPath /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null } catch {}
Info "Конфиг → $CfgPath"

# --- 7. Scheduled Task (logon, pythonw, restart) ---------------------------
$PythonW = Join-Path $VenvDir 'Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { $PythonW = $VenvPy }   # fallback (консоль мелькнёт)
$taskName = 'PersonaAgent'
$arg = ('-m cli run --config "{0}"' -f $CfgPath)
$action  = New-ScheduledTaskAction  -Execute $PythonW -Argument $arg -WorkingDirectory $AgentDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $set -Principal $principal -Description 'Persona capture agent' | Out-Null
Info "Задача автозапуска '$taskName' зарегистрирована."

# --- 8. start + verify -----------------------------------------------------
Start-ScheduledTask -TaskName $taskName
$log = Join-Path $env:LOCALAPPDATA 'Persona\Logs\persona-agent.log'
Info "Готово! Агент запущен."
Info "Лог:  $log"
Info "Статус: & `"$VenvPy`" -m cli status --config `"$CfgPath`""
