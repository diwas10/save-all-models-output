#!/bin/zsh
# Run daily by the local.aihistory.update LaunchAgent: pulls the latest code
# from GitHub and, if anything changed, re-runs setup so all laptops stay in
# sync. Only fast-forwards; never overwrites local edits.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
before=$(git rev-parse HEAD)
git pull --ff-only -q || { echo "$(date): pull failed (local changes or offline)"; exit 0; }
after=$(git rev-parse HEAD)
if [[ "$before" != "$after" ]]; then
  echo "$(date): updated ${before:0:7} -> ${after:0:7}, re-running setup"
  "$REPO/setup.sh" --from-updater
else
  echo "$(date): up to date"
fi
