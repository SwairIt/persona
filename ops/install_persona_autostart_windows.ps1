<#
.SYNOPSIS
    Install / repair the Persona always-on Windows Scheduled Tasks.

.DESCRIPTION
    Registers (or re-registers) the two tasks that keep the Persona site alive:

      PersonaWatchdog  - runs ops/persona_watchdog.py every minute. Short-lived
                         probe: hits http://127.0.0.1:<port>/landing twice; after
                         _FAIL_THRESHOLD consecutive failing runs it kills the
                         stale uvicorn and starts a fresh one. Exits in seconds.

      PersonaMemproc   - runs ops/memory_processor.py every 10 minutes. Refreshes
                         hourly memory cards, plus bounded OCR/embedding batches.
                         Self-locking (memproc.lock), so overlap is impossible.

    Both are registered with an S4U principal ("run whether the user is logged on
    or not", no stored password) and an AtStartup trigger, so the site survives
    the owner logging out, a reboot with nobody logged in, and a crash.

    THIS SCRIPT MUST RUN ELEVATED. A non-elevated shell can change task *settings*
    but Windows returns "Access is denied" (0x80070005) for an S4U LogonType, a
    RunLevel of Highest, and a BootTrigger. See docs/ALWAYS_ON_WINDOWS.md.

.PARAMETER User
    Account the tasks run as. Defaults to the current user. Needs the "Log on as
    a batch job" right (secpol.msc -> Local Policies -> User Rights Assignment),
    which S4U requires; Administrators normally have it via a group.

.EXAMPLE
    # from an ELEVATED PowerShell:
    powershell -ExecutionPolicy Bypass -File ops\install_persona_autostart_windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File ops\install_persona_autostart_windows.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File ops\install_persona_autostart_windows.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string] $Repo,
    [string] $PyExe,
    [string] $User,
    [int]    $WatchdogIntervalMinutes = 1,
    [int]    $MemprocIntervalMinutes  = 10,
    [switch] $Uninstall,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

$WATCHDOG_TASK = 'PersonaWatchdog'
$MEMPROC_TASK  = 'PersonaMemproc'

# --- elevation ------------------------------------------------------------
# S4U + BootTrigger + RunLevel Highest are all privileged writes. Fail loudly
# and early rather than half-applying and leaving a logon-only task behind.
function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Elevated) -and -not $DryRun) {
    Write-Error @"
This script must run ELEVATED (Run as administrator).

Without elevation Windows rejects exactly the three things that make the site
survive a logout: LogonType=S4U, RunLevel=Highest, and the AtStartup trigger
(each returns 'Access is denied' / 0x80070005).

Re-run from an elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File "$PSCommandPath"
"@
    exit 1
}

# --- paths (portable: derived from this file's location) ------------------
if (-not $Repo) {
    if ($env:PERSONA_REPO) { $Repo = $env:PERSONA_REPO }
    else { $Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
}
if (-not $PyExe) {
    if ($env:PERSONA_PYEXE) {
        $PyExe = $env:PERSONA_PYEXE
    } else {
        # pythonw (GUI subsystem) allocates no console -> no black window on respawn.
        $venvPyw = Join-Path $Repo '.venv\Scripts\pythonw.exe'
        if (Test-Path $venvPyw) { $PyExe = $venvPyw } else { $PyExe = 'pythonw.exe' }
    }
}
if (-not $User) { $User = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }

$watchdogScript = Join-Path $Repo 'ops\persona_watchdog.py'
$memprocScript  = Join-Path $Repo 'ops\memory_processor.py'

# --- uninstall ------------------------------------------------------------
if ($Uninstall) {
    foreach ($n in @($WATCHDOG_TASK, $MEMPROC_TASK)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            if ($DryRun) { "DRY-RUN: Unregister-ScheduledTask -TaskName $n" }
            else { Unregister-ScheduledTask -TaskName $n -Confirm:$false; "Removed $n" }
        } else { "Not present: $n" }
    }
    exit 0
}

foreach ($p in @($watchdogScript, $memprocScript)) {
    if (-not (Test-Path $p)) { Write-Error "Missing script: $p"; exit 1 }
}

# --- shared principal -----------------------------------------------------
# S4U = "run whether user is logged on or not" WITHOUT storing a password.
# The task then runs in session 0, so nothing dies when the owner logs out.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Highest

function New-PersonaSettings {
    param([string] $ExecutionTimeLimit)
    # StartWhenAvailable  - catch up a run missed while the box was off/asleep.
    # Battery flags off   - a server must never skip or abort on "battery".
    # IgnoreNew           - never stack a second probe on top of a running one.
    # RestartOnFailure    - retry a run that crashed outright (e.g. at boot,
    #                       before the network stack is ready).
    # StopOnIdleEnd off   - do not kill a run because the box stopped being idle.
    $s = New-ScheduledTaskSettingsSet `
            -MultipleInstances IgnoreNew `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([System.Xml.XmlConvert]::ToTimeSpan($ExecutionTimeLimit)) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1)
    $s.IdleSettings.StopOnIdleEnd = $false
    $s
}

function New-PersonaTriggers {
    param([int] $IntervalMinutes, [string] $BootDelay)
    # Repeating time trigger with NO repetition duration = repeat forever.
    # StartBoundary is in the past on purpose so the first repeat is immediate.
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date '2026-01-01T00:00:00') `
                -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
    # AtStartup: the site comes back after a reboot with NOBODY logged in.
    # This is the trigger that a non-elevated shell cannot create.
    $boot = New-ScheduledTaskTrigger -AtStartup
    $boot.Delay = $BootDelay
    @($boot, $repeat)
}

function Install-PersonaTask {
    param(
        [string] $Name,
        [string] $Script,
        [int]    $IntervalMinutes,
        [string] $ExecutionTimeLimit,
        [string] $BootDelay,
        [string] $Description
    )
    $action = New-ScheduledTaskAction -Execute $PyExe -Argument "`"$Script`"" -WorkingDirectory $Repo
    $trigs  = New-PersonaTriggers -IntervalMinutes $IntervalMinutes -BootDelay $BootDelay
    $sets   = New-PersonaSettings -ExecutionTimeLimit $ExecutionTimeLimit

    "Task     : $Name (every $IntervalMinutes min + AtStartup, delay $BootDelay)"
    "Run      : `"$PyExe`" `"$Script`"  (cwd $Repo)"
    "Principal: $User  LogonType=S4U  RunLevel=Highest"
    "Limit    : $ExecutionTimeLimit"

    if ($DryRun) { "DRY-RUN: Register-ScheduledTask -TaskName $Name -Force"; return }

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigs `
        -Principal $principal -Settings $sets -Description $Description -Force | Out-Null

    $a = Get-ScheduledTask -TaskName $Name
    "  -> LogonType=$($a.Principal.LogonType) RunLevel=$($a.Principal.RunLevel) " +
    "Triggers=$(($a.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ',')"
    ""
}

Install-PersonaTask -Name $WATCHDOG_TASK -Script $watchdogScript `
    -IntervalMinutes $WatchdogIntervalMinutes -ExecutionTimeLimit 'PT10M' -BootDelay 'PT30S' `
    -Description 'Persona: probe the web server; restart uvicorn if it is down. Short-lived probe, runs every minute.'

# Memproc waits 5 min after boot: the watchdog owns the boot window, and memory
# work must not compete with the web server's cold start.
Install-PersonaTask -Name $MEMPROC_TASK -Script $memprocScript `
    -IntervalMinutes $MemprocIntervalMinutes -ExecutionTimeLimit 'PT30M' -BootDelay 'PT5M' `
    -Description 'Persona: hourly memory cards + bounded OCR/embedding batches. Self-locking.'

if (-not $DryRun) {
    Write-Host @"
Installed. IMPORTANT next step - the tasks now run in session 0, but any uvicorn
started earlier from an interactive session is still holding port 8000 and will
keep serving OLD code. Do one clean cycle:

    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { `$_.CommandLine -like '*app.web.main*' } |
        ForEach-Object { taskkill /F /PID `$_.ProcessId /T }
    Start-ScheduledTask -TaskName $WATCHDOG_TASK

then verify with HTTP (a live port does not prove fresh code):

    Invoke-WebRequest http://127.0.0.1:8000/landing -UseBasicParsing

See docs/ALWAYS_ON_WINDOWS.md for verification, rollback and failure modes.
"@
}
