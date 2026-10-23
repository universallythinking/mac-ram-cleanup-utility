#!/usr/bin/env bash
# Prefer double-clicking RamCleanup.app (own dock icon, no Terminal/Chrome).
# This .command is a fallback that opens the UI in your browser.
cd "$(dirname "$0")"
APP="$(dirname "$0")/RamCleanup.app"
if [[ -d "$APP" ]]; then
  open "$APP"
  exit 0
fi
echo "Starting ramcleanup web UI in browser…"
python3 ramcleanup.py --web --open
echo
echo "Server stopped. Press Enter to close this window."
read -r _
