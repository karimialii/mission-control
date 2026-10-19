#!/usr/bin/env bash
# Installs Mission Control as a global Claude Code skill (symlink, so edits here apply everywhere).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/skills/mission-control"
mkdir -p "$HOME/.claude/skills" "$HOME/.mission-control"
if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
  echo "$DEST exists and is not a symlink; move it away first." >&2; exit 1
fi
ln -sfn "$SRC" "$DEST"
chmod +x "$SRC/scripts/mc.py" "$SRC/scripts/dashboard.py"
echo "Installed: $DEST -> $SRC"
echo "Dashboard: python3 \"$SRC/scripts/mc.py\" dashboard --open"
