#!/bin/bash
# The always-on agent: rescans the job boards every scan_gap_minutes while applying to what it finds.
# Started at login by launchd (com.jobagent.daemon) and restarted if it crashes.
# Shares state/cycle.lock with the old hourly cycle, so the two can never run together.
cd "$(dirname "$0")" || exit 1
mkdir -p logs state
LOCK=state/cycle.lock
until mkdir "$LOCK" 2>/dev/null; do
  if [ -f "$LOCK/pid" ] && ! kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then rm -rf "$LOCK"; continue; fi
  sleep 30   # an old cycle is still finishing
done
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
.venv/bin/python bridge/pipeline.py --daemon >> logs/cycle.log 2>&1
