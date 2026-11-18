#!/bin/bash
# Fill in the install path and home directory in launchd/*.plist and load them. macOS only.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="$HOME/Library/LaunchAgents"; mkdir -p "$DEST" logs
for p in launchd/*.plist; do
  out="$DEST/$(basename "$p")"
  sed -e "s#__INSTALL_DIR__#$PWD#g" -e "s#__HOME__#$HOME#g" "$p" > "$out"
  launchctl bootstrap "gui/$(id -u)" "$out" && echo "loaded $out"
done
