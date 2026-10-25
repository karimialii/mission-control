#!/usr/bin/env bash
# Installs Mission Control into Claude Code: the skill, the /mission-control-... slash commands (symlinks, so
# updates apply everywhere) and a short `mc` command, so that from any project folder you can run:   mc open
#
#   bash install.sh              install (safe to run again, also after every update)
#   MC_BIN_DIR=~/bin bash install.sh     put the `mc` command in this folder instead
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/skills/mission-control"
mkdir -p "$HOME/.claude/skills" "$HOME/.mission-control"
if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then
  echo "$DEST exists and is not a symlink; move it away first." >&2; exit 1
fi
ln -sfn "$SRC" "$DEST"
chmod +x "$SRC/scripts/mc.py" "$SRC/scripts/dashboard.py"
echo "✓ Skill installed: $DEST -> $SRC"

# ── slash commands: type /mission-control in Claude Code to see them ──
CMDS="$HOME/.claude/commands"
mkdir -p "$CMDS"
n=0
for f in "$SRC"/commands/*.md; do
  t="$CMDS/$(basename "$f")"
  if [ -e "$t" ] && [ ! -L "$t" ]; then echo "! $t already exists and isn't ours, so I left it alone."; continue; fi
  ln -sfn "$f" "$t"; n=$((n + 1))
done
echo "✓ $n slash commands installed (type /mission-control in Claude Code to see them)"

# ── the `mc` command ──
# A symlink in a folder that is already on your PATH, so no shell files are edited.
on_path() { case ":$PATH:" in *":$1:"*) return 0;; *) return 1;; esac; }
BIN=""
if [ -n "${MC_BIN_DIR:-}" ]; then
  BIN="${MC_BIN_DIR/#\~/$HOME}"; mkdir -p "$BIN"
else
  for d in "$HOME/.local/bin" "$HOME/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
    if [ -d "$d" ] && [ -w "$d" ] && on_path "$d"; then BIN="$d"; break; fi
  done
fi
if [ -z "$BIN" ]; then
  echo "! No writable folder on your PATH was found for the \`mc\` command."
  echo "  Create ~/.local/bin, add it to your PATH, and run this again:"
  echo "    mkdir -p ~/.local/bin && echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
  echo "  Until then, use:  python3 \"$SRC/scripts/mc.py\" open"
  exit 0
fi
LINK="$BIN/mc"
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
  if [ "$(readlink "$LINK" 2>/dev/null || true)" != "$SRC/scripts/mc.py" ] && ! grep -q "Mission Control" "$LINK" 2>/dev/null; then
    echo "! $LINK already exists and is something else (Midnight Commander?), so I left it alone."
    LINK="$BIN/mission"
    echo "  Installing the command as \`mission\` instead."
  fi
fi
ln -sfn "$SRC/scripts/mc.py" "$LINK"
NAME="$(basename "$LINK")"
echo "✓ Command installed: $LINK"
if ! on_path "$BIN"; then
  echo "! $BIN is not on your PATH yet. Add it:  echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc  (then open a new terminal)"
fi
echo
echo "Open the dashboard:"
echo "    in a terminal, from any project folder:    cd path/to/your/project && $NAME open"
echo "    inside Claude Code:                        type /mission-control and pick  /mission-control-open"
