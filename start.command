#!/usr/bin/env bash
# Double-click to launch the ramcleanup terminal UI.
cd "$(dirname "$0")"
echo "Starting ramcleanup…"
python3 ramcleanup.py
echo
echo "Stopped. Press Enter to close this window."
read -r _
