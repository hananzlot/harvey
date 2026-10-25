#!/bin/bash
# Installs the Harvey dashboard + bridge as launchd agents for the current user.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/Library/LaunchAgents"
mkdir -p "$dest" /Users/chananzlotnitsky/harvey/data/logs
for name in dashboard bridge; do
  label="ai.ibuildpro.harvey.$name"
  cp "$here/$label.plist" "$dest/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest/$label.plist"
  echo "loaded $label"
done
