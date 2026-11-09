#!/bin/bash
# Reverses desktop/install.sh (proxy, certificate trust, proxy setting).
# The server and collector are removed by: python3 scripts/setup.py --uninstall
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

launchctl bootout "gui/$(id -u)/local.aihistory.proxy" 2>/dev/null && echo "✓ stopped proxy"
rm -f "$LA/local.aihistory.proxy.plist"

if [[ -f "$CERT" ]]; then
  security remove-trusted-cert "$CERT" 2>/dev/null && echo "✓ certificate no longer trusted"
  security delete-certificate -c mitmproxy "$HOME/Library/Keychains/login.keychain-db" 2>/dev/null
fi
echo "Desktop proxy removed."
