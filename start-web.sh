#!/usr/bin/env bash
# Launch ramcleanup web UI (opens browser on http://127.0.0.1:8765).
# Prefer double-clicking start-web.command on macOS.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 ramcleanup.py --web --open "$@"
