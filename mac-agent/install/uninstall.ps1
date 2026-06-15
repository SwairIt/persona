<#
  Persona agent — Windows uninstaller.
  Stops + removes the Scheduled Task and (optionally) the venv/config/logs.

  Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1 [-Purge]
#>
[CmdletBinding()]
param([switch]$Purge)
$ErrorActionPreference = 'SilentlyContinue'

function Info($m) { Write-Host "[persona-agent] $m" -ForegroundColor Cyan }

$AgentDir = Split-Path -Parent $PSScriptRoot
$VenvDir  = Join-Path $AgentDir '.venv'
$CfgDir   = Join-Path $env:APPDATA 'Persona'
$LogDir   = Join-Path $env:LOCALAPPDATA 'Persona'

Info "Останавливаю и удаляю задачу PersonaAgent…"
Stop-ScheduledTask  -TaskName 'PersonaAgent' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'PersonaAgent' -Confirm:$false -ErrorAction SilentlyContinue

if ($Purge) {
    Info "Удаляю venv, конфиг и логи…"
    Remove-Item -Recurse -Force $VenvDir -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $CfgDir  -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $LogDir  -ErrorAction SilentlyContinue
}
Info "Готово. Агент удалён$(if ($Purge) {' (вместе с данными)'} else {' (venv/конфиг оставлены — добавь -Purge чтобы снести)'})."
