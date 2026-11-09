#!/bin/bash
# macOS only: records the ChatGPT / Gemini / Claude desktop apps. Called by
# scripts/setup.py (which also runs the local server this relies on).
# Undo with: desktop/uninstall.sh
#
#  1. LaunchAgent: mitmdump proxy (127.0.0.1:8080) with our recorder addon
#  2. Trusts mitmproxy's certificate in your login keychain (macOS asks for
#     your password once) so apps accept the proxy for AI hosts
#  3. Points each network service at http://127.0.0.1:8765/proxy.pac, which
#     routes only AI-chat hosts to the proxy
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LA="$HOME/Library/LaunchAgents"
DATA="${AI_HISTORY_DIR:-$HOME/AIHistory}"
MITM="$(command -v mitmdump || true)"
CERT="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
PAC_URL="http://127.0.0.1:8765/proxy.pac"

if [[ -z "$MITM" ]]; then
  echo "mitmproxy is not installed. Run:  brew install mitmproxy   then re-run this script." >&2
  exit 1
fi
mkdir -p "$LA" "$DATA/logs"
chmod 700 "$DATA"

write_agent() { # label program-args...
  local label=$1; shift
  local args=""
  for a in "$@"; do args+="    <string>$a</string>"$'\n'; done
  cat > "$LA/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>EnvironmentVariables</key>
  <dict><key>AI_HISTORY_DIR</key><string>$DATA</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/$label.log</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$LA/$label.plist"
  echo "✓ started $label"
}

echo "== 1. Proxy"
write_agent local.aihistory.proxy "$MITM" -q --listen-host 127.0.0.1 -p 8080 -s "$REPO/desktop/mitm_addon.py"

for i in {1..20}; do [[ -f "$CERT" ]] && break; sleep 0.5; done
[[ -f "$CERT" ]] || { echo "mitmproxy did not create its certificate; see $DATA/logs/local.aihistory.proxy.log" >&2; exit 1; }

echo "== 2. Trust the proxy certificate (macOS will ask for your password)"
if security verify-cert -c "$CERT" >/dev/null 2>&1; then
  echo "✓ already trusted"
elif [[ -n "${AIHISTORY_FROM_UPDATER:-}" ]]; then
  echo "! certificate not trusted; run scripts/setup.py by hand to approve it"  # no password prompts from background jobs
else
  security add-trusted-cert -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$CERT"
  echo "✓ trusted $CERT"
fi

echo "== 3. Route AI hosts through the proxy"
networksetup -listallnetworkservices | tail -n +2 | grep -v '^\*' | while IFS= read -r svc; do
  networksetup -setautoproxyurl "$svc" "$PAC_URL"
  networksetup -setautoproxystate "$svc" on
  echo "✓ $svc -> $PAC_URL"
done

echo
echo "Done. Open the ChatGPT or Gemini app and send a message, then run:"
echo "  tail -f $DATA/logs/local.aihistory.proxy.log"
echo "  python3 $REPO/server/server.py stats"
