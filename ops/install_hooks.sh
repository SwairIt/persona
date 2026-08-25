#!/bin/sh
# Install Persona's git hooks (currently: the secret-scan pre-commit hook).
#
#   sh ops/install_hooks.sh
#
# Installs a two-line shim into .git/hooks/pre-commit that delegates to the
# versioned ops/hooks/pre-commit. The shim means edits to the real hook take
# effect immediately without reinstalling, and it leaves .git/hooks/ free for
# the `pre-commit` framework to chain (it renames an existing hook to
# pre-commit.legacy and still runs it).
#
# Uninstall:  rm .git/hooks/pre-commit
# Bypass once: PERSONA_SKIP_SECRET_SCAN=1 git commit ...

set -e

root=$(git rev-parse --show-toplevel)
hookdir=$(git rev-parse --git-path hooks)
target="$hookdir/pre-commit"

mkdir -p "$hookdir"

if [ -f "$target" ] && ! grep -q "ops/hooks/pre-commit" "$target" 2>/dev/null; then
    backup="$target.backup.$(date +%Y%m%d%H%M%S)"
    cp "$target" "$backup"
    echo "Existing pre-commit hook backed up to: $backup"
fi

cat > "$target" <<'SHIM'
#!/bin/sh
# Installed by ops/install_hooks.sh - delegates to the versioned hook.
root=$(git rev-parse --show-toplevel)
[ -f "$root/ops/hooks/pre-commit" ] && exec sh "$root/ops/hooks/pre-commit" "$@"
exit 0
SHIM

chmod +x "$target" 2>/dev/null || true

echo "Installed: $target -> ops/hooks/pre-commit"
echo "Verifying scanner runs..."
sh "$root/ops/hooks/pre-commit" && echo "OK - secret-scan is active."
