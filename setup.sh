#!/bin/sh
# macOS / Linux:  ./setup.sh [data folder]   (Windows: setup.cmd)   Options: --no-proxy --uninstall --dry-run
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  case "$(uname)" in
    Darwin) echo "Python 3 is needed: run  xcode-select --install  then ./setup.sh again" ;;
    *)      echo "Python 3 is needed: e.g.  sudo apt install python3  then ./setup.sh again" ;;
  esac
  exit 1
fi
exec python3 scripts/setup.py "$@"
