#!/bin/zsh
# Stops the server and collector jobs. Data is kept.
for label in local.aihistory.update local.aihistory.collect local.aihistory.server; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "✓ stopped $label"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
