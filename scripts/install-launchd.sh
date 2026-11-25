#!/bin/bash
# Fill in the install path and home directory in the launchd plists and load them. macOS only.
# Loads the always-on scanner, the daily report and the watchlist. launchd/com.jobagent.cycle.plist is the
# hourly alternative to the always-on scanner, so it is not loaded here.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="$HOME/Library/LaunchAgents"; mkdir -p "$DEST" logs
for p in launchd/com.jobagent.{daemon,daily,watchlist}.plist; do
  out="$DEST/$(basename "$p")"
  sed -e "s#__INSTALL_DIR__#$PWD#g" -e "s#__HOME__#$HOME#g" "$p" > "$out"
  launchctl bootstrap "gui/$(id -u)" "$out" && echo "loaded $out"
done
