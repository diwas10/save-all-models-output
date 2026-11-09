#!/bin/zsh
# Reverses desktop/install.sh. Your saved data in ~/AIHistory is kept.
set -uo pipefail
LA="$HOME/Library/LaunchAgents"
CERT="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"

networksetup -listallnetworkservices | tail -n +2 | grep -v '^\*' | while IFS= read -r svc; do
  if networksetup -getautoproxyurl "$svc" | grep -q "127.0.0.1:8765/proxy.pac"; then
    networksetup -setautoproxystate "$svc" off
    networksetup -setautoproxyurl "$svc" " " 2>/dev/null
    echo "✓ proxy removed from $svc"
  fi
done

for label in local.aihistory.proxy local.aihistory.server; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "✓ stopped $label"
  rm -f "$LA/$label.plist"
done

if [[ -f "$CERT" ]]; then
  security remove-trusted-cert "$CERT" 2>/dev/null && echo "✓ certificate no longer trusted"
  security delete-certificate -c mitmproxy "$HOME/Library/Keychains/login.keychain-db" 2>/dev/null
fi
echo "Uninstalled. Data kept in ${AI_HISTORY_DIR:-$HOME/AIHistory}."
