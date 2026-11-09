#!/bin/sh
# Removes the background jobs (and the macOS proxy). Your data is kept.
exec "$(dirname "$0")/setup.sh" --uninstall
