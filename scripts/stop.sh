#!/bin/bash
# Stopping the server.
#
# WARNING: there is NO clean programmatic shutdown (SIGINT, SIGTERM, SIGHUP,
# taskkill, tmux send-keys: all tested, none of them saves). So the process is
# cut off, and EVERYTHING done since the last autosave is LOST.
#
# A shutdown issued while a player was replacing a planet in game once wiped
# that work. This script therefore REFUSES to cut when the save is older than
# SE_SAVE_MAX_AGE (default 3 min), unless --force is passed.
#
# The only shutdown that saves: "tmux attach -t <session>" and press Ctrl+C by
# hand in the live console.

SE_ROOT="${SE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=common.sh
. "$SE_ROOT/scripts/common.sh"

export WINEPREFIX="$SE_PREFIX"
BASE="$SE_APPDATA"
SAVE="$SE_SAVE_DIR/SANDBOX_0_0_0_.sbs"
SESSION="$SE_TMUX_SESSION"

PID=$(pgrep -f "SpaceEngineersDedicated.exe" | head -1)
if [ -z "$PID" ]; then
  tmux kill-session -t "$SESSION" 2>/dev/null
  echo "Server already stopped."; exit 0
fi

AGE=0
if [ -f "$SAVE" ]; then
  AGE=$(( $(date +%s) - $(stat -f "%m" "$SAVE") ))
  echo "Last save           : $((AGE/60))m$((AGE%60))s ago."
else
  # No save file where it is expected: either SE_WORLD is wrong or the world
  # has never been saved. Say so instead of pretending the save is fresh,
  # because AGE=0 would silently disarm the guard below.
  echo "Save file not found : $SAVE"
  echo "                      (check SE_WORLD, currently \"$SE_WORLD\")"
  if [ "${1:-}" != "--force" ]; then
    echo
    echo "REFUSING TO STOP: the save guard cannot verify anything."
    echo "  - fix SE_WORLD in config.sh"
    echo "  - or force with: stop.sh --force"
    exit 1
  fi
fi

# Any players connected?
L=$(ls -t "$BASE"/*.log 2>/dev/null | head -1)
PLAYERS="?"
if [ -n "$L" ]; then
  LEGEND=$(grep "STATISTICS LEGEND" "$L" | tail -1 | sed 's/.*LEGEND,//')
  VALUES=$(grep "STATISTICS," "$L" | tail -1 | sed 's/.*STATISTICS,//')
  if [ -n "$VALUES" ]; then
    PLAYERS=$(paste -d'|' <(echo "$LEGEND" | tr ',' '\n') <(echo "$VALUES" | tr ',' '\n') \
              | awk -F'|' '$1=="GetOnlinePlayerCount"{print $2}')
  fi
fi
echo "Players online      : ${PLAYERS:-?}"

if [ "${1:-}" != "--force" ] && [ "$AGE" -gt "$SE_SAVE_MAX_AGE" ]; then
  echo
  echo "REFUSING TO STOP: the save is older than $((SE_SAVE_MAX_AGE/60))m$((SE_SAVE_MAX_AGE%60))s."
  echo "Everything built or changed since would be LOST."
  echo "  - wait for the autosave (every 10 min by default) and run this again"
  echo "  - or force with: stop.sh --force"
  exit 1
fi

kill -TERM "$PID" 2>/dev/null
for i in $(seq 1 8); do sleep 2; kill -0 "$PID" 2>/dev/null || break; done
kill -0 "$PID" 2>/dev/null && { kill -9 "$PID" 2>/dev/null; sleep 2; }
tmux kill-session -t "$SESSION" 2>/dev/null
echo "Server stopped (world is at the state of the save reported above)."
