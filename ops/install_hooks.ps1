# Install Persona's git hooks (currently: the secret-scan pre-commit hook).
#
#   powershell -ExecutionPolicy Bypass -File ops\install_hooks.ps1
#
# Same result as ops/install_hooks.sh - provided so the hook can be installed
# from a plain PowerShell prompt without Git Bash. Git for Windows runs hook
# files through its bundled POSIX sh, so the installed shim is still /bin/sh.
#
# Uninstall:   Remove-Item .git\hooks\pre-commit
# Bypass once: $env:PERSONA_SKIP_SECRET_SCAN = '1'; git commit ...

$ErrorActionPreference = 'Stop'

$root = (git rev-parse --show-toplevel).Trim()
$hookDir = Join-Path $root '.git\hooks'
$target = Join-Path $hookDir 'pre-commit'

if (-not (Test-Path $hookDir)) { New-Item -ItemType Directory -Path $hookDir -Force | Out-Null }

if (Test-Path $target) {
    $existing = Get-Content $target -Raw
    if ($existing -notmatch 'ops/hooks/pre-commit') {
        $backup = "$target.backup.$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item $target $backup
        Write-Host "Existing pre-commit hook backed up to: $backup"
    }
}

# LF line endings and no BOM - this file is executed by sh, not PowerShell.
$shim = @'
#!/bin/sh
# Installed by ops/install_hooks.ps1 - delegates to the versioned hook.
root=$(git rev-parse --show-toplevel)
[ -f "$root/ops/hooks/pre-commit" ] && exec sh "$root/ops/hooks/pre-commit" "$@"
exit 0
'@ -replace "`r`n", "`n"

[System.IO.File]::WriteAllText($target, $shim, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Installed: $target -> ops/hooks/pre-commit"
Write-Host 'Verifying scanner runs...'
& git rev-parse --show-toplevel | Out-Null
& sh (Join-Path $root 'ops/hooks/pre-commit')
if ($LASTEXITCODE -eq 0) { Write-Host 'OK - secret-scan is active.' }
else { Write-Warning "Hook returned exit code $LASTEXITCODE" }
