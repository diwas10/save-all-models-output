#!/bin/zsh
# Run daily by the local.aihistory.update LaunchAgent: pulls the latest code
# from GitHub and, if anything changed, re-runs setup so all laptops stay in
# sync. Only fast-forwards; never overwrites local edits.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
before=$(git rev-parse HEAD)
branch=$(git rev-parse --abbrev-ref HEAD)
if ! git fetch -q origin "$branch" || ! git merge --ff-only -q FETCH_HEAD; then
  echo "$(date): update skipped (offline, or local changes that can't fast-forward)"
  exit 0
fi
after=$(git rev-parse HEAD)
if [[ "$before" != "$after" ]]; then
  echo "$(date): updated ${before:0:7} -> ${after:0:7}, re-running setup"
  "$REPO/setup.sh" --from-updater
else
  echo "$(date): up to date"
fi
