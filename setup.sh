#!/bin/zsh
# One-shot setup for a Mac. Safe to run again: every step is idempotent.
#
#   git clone https://github.com/<you>/save-all-models-output ~/save-all-models-output \
#     && ~/save-all-models-output/setup.sh
#
# What it does
#   1. Background jobs: local server + collector (Claude Code, Codex, Antigravity)
#      + daily auto-update from GitHub
#   2. Builds the Chrome / Firefox extension
#   3. Desktop-app recording proxy (installs mitmproxy with Homebrew; macOS
#      asks for your password once to trust its certificate)
#      Skip with:  ./setup.sh --no-proxy
#
# Undo everything: ./uninstall.sh   (your saved data is kept)
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
FROM_UPDATER=0; NO_PROXY=0
for a in "$@"; do
  case $a in
    --no-proxy) NO_PROXY=1 ;;
    --from-updater) FROM_UPDATER=1 ;;
  esac
done
[[ -f "$REPO/.no-proxy" ]] && NO_PROXY=1   # remembered choice
(( FROM_UPDATER )) && export AIHISTORY_FROM_UPDATER=1
step() { print -P "\n%F{cyan}== $1%f"; }

[[ "$(uname)" == Darwin ]] || { echo "This setup is for macOS." >&2; exit 1; }
if ! python3 -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then
  echo "Python 3.9+ is needed. Run:  xcode-select --install   then run setup.sh again." >&2
  exit 1
fi

step "1/3 Background jobs (server, collector, auto-update)"
"$REPO/collectors/install.sh"

step "2/3 Browser extension"
python3 "$REPO/extension/build.py"

step "3/3 Desktop-app proxy"
if (( NO_PROXY )); then
  echo "skipped (--no-proxy)"; touch "$REPO/.no-proxy"
else
  if ! command -v mitmdump >/dev/null; then
    if command -v brew >/dev/null; then
      brew install mitmproxy
    else
      echo "Homebrew not found, so the desktop proxy was skipped."
      echo "Install Homebrew (https://brew.sh) and run setup.sh again to add it."
    fi
  fi
  command -v mitmdump >/dev/null && "$REPO/desktop/install.sh"
fi

(( FROM_UPDATER )) && exit 0
MACHINE="$(python3 -c "import sys; sys.path.insert(0, '$REPO/server'); import store; print(store.MACHINE)")"

cat <<EOF

────────────────────────────────────────────────────────────
✓ Done. Everything now runs in the background and starts at login.

One thing browsers don't let a script do: load the extension (30 seconds).
  Chrome:  chrome://extensions → Developer mode → Load unpacked →
           $REPO/extension/dist/chrome
  Firefox: about:debugging → This Firefox → Load Temporary Add-on →
           $REPO/extension/dist/firefox/manifest.json

Your data:  ~/AI/local-data/ai_history-$MACHINE.sqlite
            (in Google Drive's settings, choose to sync the ~/AI/local-data folder)
────────────────────────────────────────────────────────────
EOF
open "$REPO/extension/dist" 2>/dev/null || true
