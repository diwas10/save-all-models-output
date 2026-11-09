#!/bin/zsh
# Removes all background jobs, the proxy setting and certificate trust.
# Your saved data (~/AIHistory and ~/AI/local-data) is kept.
REPO="$(cd "$(dirname "$0")" && pwd)"
"$REPO/desktop/uninstall.sh"
"$REPO/collectors/uninstall.sh"
