# Persona LLM Worker — лаунчер для Windows (одна команда для ПК).
#
# Что делает:
#   1. Находит Python (.venv репозитория, иначе python из PATH).
#   2. Проверяет/ставит httpx (pip install httpx) — единственная зависимость.
#   3. Читает PERSONA_WORKER_TOKEN (а также опц. PERSONA_SERVER / OLLAMA_URL):
#      сначала из env, затем из <repo>\.env (как fallback).
#   4. Запускает ops\persona_llm_worker.py в БЕСКОНЕЧНОМ цикле с авто-рестартом
#      (если .py упал/сеть моргнула — поднимаем заново через паузу).
#
# Запуск (просто, без админа):
#   powershell -ExecutionPolicy Bypass -File .\ops\persona_llm_worker.ps1
#
# Токен можно передать тремя способами (приоритет сверху вниз):
#   * переменная окружения PERSONA_WORKER_TOKEN;
#   * строка PERSONA_WORKER_TOKEN=... в <repo>\.env;
#   * параметр -Token "<плейнтекст>" этому скрипту.

[CmdletBinding()]
param(
    [string]$Token,
    [string]$Server,
    [string]$OllamaUrl,
    # Пауза (сек) между авто-рестартами .py.
    [int]$RestartDelay = 3
)

$ErrorActionPreference = 'Stop'

# --- Пути -----------------------------------------------------------------
$ScriptDir   = $PSScriptRoot                       # ...\ops
$RepoRoot    = (Get-Item -Path $ScriptDir).Parent.FullName
$WorkerPy    = Join-Path $ScriptDir 'persona_llm_worker.py'

if (-not (Test-Path $WorkerPy)) {
    Write-Error "Не найден $WorkerPy — запускай скрипт из репозитория Persona."
    exit 1
}

# --- Python ---------------------------------------------------------------
# Предпочитаем .venv репозитория (там уже стоит httpx), иначе python из PATH.
$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (Test-Path $VenvPy) {
    $Python = $VenvPy
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $cmd = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $cmd) {
        Write-Error "Не найден Python. Установи Python 3.10+ или создай .venv в репозитории."
        exit 1
    }
    $Python = $cmd.Source
}
Write-Host "[launcher] Python: $Python"

# --- Зависимость httpx ----------------------------------------------------
# Проверяем импорт; если нет — ставим. exit-код python = есть/нет модуля.
& $Python -c "import httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[launcher] httpx не найден — ставлю (pip install httpx)..."
    & $Python -m pip install --quiet httpx
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Не удалось установить httpx. Поставь вручную: $Python -m pip install httpx"
        exit 1
    }
}

# --- .env как fallback для конфига ---------------------------------------
# Простой парсер KEY=VALUE; реальный env имеет приоритет (его не перетираем).
function Get-DotEnvValue([string]$Name) {
    $envPath = Join-Path $RepoRoot '.env'
    if (-not (Test-Path $envPath)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envPath -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        if ($key -eq $Name) {
            $val = $trimmed.Substring($idx + 1).Trim().Trim('"').Trim("'")
            return $val
        }
    }
    return $null
}

# Параметр -Token > env > .env. Аналогично для server/ollama (но они опц.).
if ($Token)     { $env:PERSONA_WORKER_TOKEN = $Token }
if ($Server)    { $env:PERSONA_SERVER       = $Server }
if ($OllamaUrl) { $env:OLLAMA_URL           = $OllamaUrl }

if (-not $env:PERSONA_WORKER_TOKEN) {
    $fromFile = Get-DotEnvValue 'PERSONA_WORKER_TOKEN'
    if ($fromFile) { $env:PERSONA_WORKER_TOKEN = $fromFile }
}

if (-not $env:PERSONA_SERVER) {
    $fromFile = Get-DotEnvValue 'PERSONA_SERVER'
    if ($fromFile) { $env:PERSONA_SERVER = $fromFile }
}

if (-not $env:OLLAMA_URL) {
    $fromFile = Get-DotEnvValue 'OLLAMA_URL'
    if ($fromFile) { $env:OLLAMA_URL = $fromFile }
}

if (-not $env:PERSONA_WORKER_TOKEN) {
    Write-Warning "PERSONA_WORKER_TOKEN не задан (ни -Token, ни env, ни .env)."
    Write-Warning "Получи токен в owner-кабинете Persona и запусти, напр.:"
    Write-Warning "  .\ops\persona_llm_worker.ps1 -Token '<твой-токен>'"
    # Всё равно запускаем — .py сам красиво сообщит и выйдет с кодом 2,
    # а цикл ниже не будет долбить вечно (см. обработку фатального кода).
}

# --- Local Ollama lifecycle -----------------------------------------------
# A hidden Scheduled Task must recover after Ollama was manually closed.
# Only manage loopback Ollama; a remote OLLAMA_URL belongs to another host.
$EffectiveOllamaUrl = if ($env:OLLAMA_URL) {
    $env:OLLAMA_URL.TrimEnd('/')
} else {
    'http://127.0.0.1:11434'
}
$OllamaUri = $null
try { $OllamaUri = [Uri]$EffectiveOllamaUrl } catch {}
$ManageLocalOllama = (
    $OllamaUri -and
    $OllamaUri.Scheme -in @('http', 'https') -and
    $OllamaUri.Host -in @('127.0.0.1', 'localhost', '::1')
)

if ($ManageLocalOllama) {
    $OllamaReady = $false
    try {
        $probe = Invoke-WebRequest `
            -Uri "$EffectiveOllamaUrl/api/tags" `
            -UseBasicParsing `
            -TimeoutSec 2
        $OllamaReady = $probe.StatusCode -eq 200
    } catch {}

    if (-not $OllamaReady) {
        $OllamaExe = $null
        $OllamaCommand = Get-Command ollama.exe -ErrorAction SilentlyContinue
        if ($OllamaCommand) {
            $OllamaExe = $OllamaCommand.Source
        } else {
            $Candidates = @(
                (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
                (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
                'C:\Program Files\Ollama\ollama.exe'
            )
            $OllamaExe = $Candidates |
                Where-Object { Test-Path -LiteralPath $_ } |
                Select-Object -First 1
        }

        if ($OllamaExe) {
            Write-Host "[launcher] Ollama не отвечает — запускаю ollama serve..."
            Start-Process `
                -FilePath $OllamaExe `
                -ArgumentList 'serve' `
                -WindowStyle Hidden
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                Start-Sleep -Seconds 1
                try {
                    $probe = Invoke-WebRequest `
                        -Uri "$EffectiveOllamaUrl/api/tags" `
                        -UseBasicParsing `
                        -TimeoutSec 2
                    if ($probe.StatusCode -eq 200) {
                        $OllamaReady = $true
                        break
                    }
                } catch {}
            }
        }
    }

    if (-not $OllamaReady) {
        Write-Warning (
            "Локальный Ollama недоступен на $EffectiveOllamaUrl. " +
            "Worker продолжит переподключаться; установи/запусти Ollama."
        )
    }
}

# --- Цикл авто-рестарта ---------------------------------------------------
Write-Host "[launcher] Запускаю Persona LLM Worker. Ctrl+C — стоп."
while ($true) {
    & $Python $WorkerPy
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        # Чистый выход (Ctrl+C в .py) — не рестартим.
        Write-Host "[launcher] Воркер завершился штатно (код 0). Выход."
        break
    }
    if ($code -eq 2 -or $code -eq 3) {
        # 2 = нет токена; 3 = 401 (неверный токен). Это конфиг-ошибки —
        # бесконечный рестарт бессмысленен, выходим, чтобы человек поправил.
        Write-Host "[launcher] Воркер вышел с кодом $code (конфиг/токен). Исправь и перезапусти."
        break
    }
    Write-Host "[launcher] Воркер упал (код $code). Рестарт через $RestartDelay с..."
    Start-Sleep -Seconds $RestartDelay
}
