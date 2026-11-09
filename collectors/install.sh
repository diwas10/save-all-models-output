#!/bin/zsh
# Background jobs that need no special permissions:
#   local.aihistory.server   receives browser-extension captures (always on)
#   local.aihistory.collect  imports Claude Code / Codex / Antigravity every
#                            10 min and snapshots the DB to the synced folder
#   local.aihistory.update   daily git pull from GitHub, re-runs setup on change
# Undo: collectors/uninstall.sh   (the desktop proxy is separate: desktop/install.sh)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
DATA="${AI_HISTORY_DIR:-$HOME/AIHistory}"
EXPORT="${AI_HISTORY_EXPORT_DIR:-$HOME/AI/local-data}"
PY="$(python3 -c 'import sys; print(sys.executable)')"   # real interpreter, not a shim
mkdir -p "$LA" "$DATA/logs" "$EXPORT"; chmod 700 "$DATA"

agent() { # label interval(0=always on) args...
  local label=$1 interval=$2; shift 2
  local args="" sched
  for a in "$@"; do args+="    <string>$a</string>"$'\n'; done
  if [[ $interval == 0 ]]; then sched="<key>KeepAlive</key><true/>"
  else sched="<key>StartInterval</key><integer>$interval</integer>"; fi
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AI_HISTORY_DIR</key><string>$DATA</string>
    <key>AI_HISTORY_EXPORT_DIR</key><string>$EXPORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  $sched
  <key>LowPriorityIO</key><true/>
  <key>StandardOutPath</key><string>$DATA/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/$label.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist"
  echo "✓ $label"
}

agent local.aihistory.server 0 "$PY" "$REPO/server/server.py"
agent local.aihistory.collect 600 "$PY" "$REPO/collectors/run.py" --scheduled
# Daily `git pull` + re-setup. Not re-registered from inside the updater itself,
# which would kill the running update.
if [[ -z "${AIHISTORY_FROM_UPDATER:-}" ]]; then
  agent local.aihistory.update 86400 /bin/zsh "$REPO/scripts/update.sh"
fi
echo "Live DB:  $DATA/ai_history.sqlite"
echo "Snapshot: $EXPORT/ai_history-<machine>.sqlite (hourly when changed)"
echo "Logs:     $DATA/logs/"
