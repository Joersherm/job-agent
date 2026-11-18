#!/bin/bash
# One scan-and-apply cycle. Scheduled hourly by launchd (see README.md).
# A lock keeps two cycles from running at once (they would fight over the same Chrome and database).
cd "$(dirname "$0")" || exit 1
mkdir -p logs
LOCK=state/cycle.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  # stale lock: the recorded pid is gone
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
    echo "$(date -u +%FT%T+00:00) Another cycle is still running; skipping this one" >> logs/cycle.log
    exit 0
  fi
  rm -rf "$LOCK"; mkdir "$LOCK" || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT
# guard.py stops the whole cycle if it is still running after an hour
.venv/bin/python bridge/guard.py 3600 -- .venv/bin/python bridge/pipeline.py "$@" >> logs/cycle.log 2>&1
