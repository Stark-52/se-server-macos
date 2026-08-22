#!/bin/bash
# Stopping the server.
#
# A clean shutdown IS possible. It saves the world, unloads the session and
# closes Steam, in about two seconds, and nothing is lost.
#
# It takes BOTH of these, and neither alone does anything (six runs, verified):
#   - the 0x03 byte written into the console  (tmux send-keys C-c)
#   - SIGINT delivered to the game process    (kill -INT)
# Which is exactly what a Ctrl+C typed by hand into an attached terminal does:
# the line discipline hands the byte to the application AND raises the signal.
# tmux send-keys only does the first, kill -INT only the second.
#
# That this was believed impossible comes from one trap: `pgrep -f` matches the
# tmux process too, because the wine command line sits in its arguments, and
# tmux always has the lower PID. Every `| head -1` therefore picked TMUX, so
# the signal killed the terminal instead of the game, which died with the pty
# under it, unsaved. See se_pid() in common.sh.
#
# Two behaviours to know about:
#   - the game honours the request when it sees fit: 0.1 s once it has been up
#     a few minutes, up to 3 min right after "Game ready" or while loading.
#   - after the shutdown the process does NOT exit. It waits on "press any key
#     to close this window" and no key sent by a script reaches it, so it is
#     killed here, AFTER the save is confirmed in the log.

# Dossier de CE script, symlinks resolus. Le code et l'installation sont deux
# racines differentes : SE_ROOT designe l'INSTALLATION (jeu, prefixe, mondes,
# config.sh), pas le depot. Sourcer common.sh depuis SE_ROOT confondait les
# deux et cassait tout usage ou le depot vit ailleurs que l'installation.
# readlink -f n'existe pas partout sur macOS, d'ou la boucle.
_lien="${BASH_SOURCE[0]}"
while [ -L "$_lien" ]; do
  _cible=$(readlink "$_lien")
  case "$_cible" in
    /*) _lien="$_cible" ;;
    *)  _lien="$(dirname "$_lien")/$_cible" ;;
  esac
done
_CODE="$(cd "$(dirname "$_lien")" && pwd)"

# SE_ROOT se deduit du chemin APPELE, pas du chemin resolu : appeler
# <installation>/scripts/stop.sh doit designer cette installation, meme quand
# le fichier est un symlink vers le depot. Prendre le chemin resolu ferait
# pointer SE_ROOT sur le depot, ou il n'y a ni jeu ni prefixe.
SE_ROOT="${SE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=common.sh
. "$_CODE/common.sh"

export WINEPREFIX="$SE_PREFIX"
BASE="$SE_APPDATA"
SAVE="$SE_SAVE_DIR/SANDBOX_0_0_0_.sbs"
SESSION="$SE_TMUX_SESSION"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

PID=$(se_pid)
if [ -z "$PID" ]; then
  tmux kill-session -t "$SESSION" 2>/dev/null
  echo "Server already stopped."; exit 0
fi

# Any players connected? Read before the shutdown, the log stops updating after.
LOG=$(ls -t "$BASE"/*.log 2>/dev/null | head -1)
PLAYERS="?"
if [ -n "$LOG" ]; then
  LEGEND=$(grep "STATISTICS LEGEND" "$LOG" | tail -1 | sed 's/.*LEGEND,//')
  VALUES=$(grep "STATISTICS," "$LOG" | tail -1 | sed 's/.*STATISTICS,//')
  if [ -n "$VALUES" ]; then
    PLAYERS=$(paste -d'|' <(echo "$LEGEND" | tr ',' '\n') <(echo "$VALUES" | tr ',' '\n') \
              | awk -F'|' '$1=="GetOnlinePlayerCount"{print $2}')
  fi
fi
echo "Players online      : ${PLAYERS:-?}"

# Only the bytes written AFTER the signal count. The log holds the save lines
# of every previous shutdown, and matching one of those would report a save
# that never happened.
MARK=0
[ -n "$LOG" ] && MARK=$(wc -c < "$LOG")

# The two halves of a hand-typed Ctrl+C. Order does not matter, both must land.
_ask() {
  tmux send-keys -t "$SESSION" C-c 2>/dev/null
  kill -INT "$PID" 2>/dev/null
}

echo "Clean shutdown      : asking the game (PID $PID) to save and exit."
_ask

# What proves a saving shutdown is the ORDER of two lines: "Exiting.." says the
# request was accepted, and the save that follows it is the shutdown save.
# Matching "Session snapshot save - END" alone is wrong: the autosave fires
# every SE_AUTOSAVE minutes and writes the very same line, so a stop that the
# game quietly ignored would be reported as a clean one.
_shutdown_done() {
  [ -n "$LOG" ] || return 1
  tail -c "+$((MARK + 1))" "$LOG" 2>/dev/null \
    | awk '/Exiting\.\./ {seen = 1}
           seen && /Session snapshot save - END/ {ok = 1}
           END {exit !ok}'
}

# Second acceptable outcome: an autosave completes while we wait. The game did
# not shut down, but the world on disk is then a few seconds old, so cutting
# costs nothing. This is what makes the wait worth its length.
_autosave_done() {
  [ -n "$LOG" ] || return 1
  tail -c "+$((MARK + 1))" "$LOG" 2>/dev/null | grep -q "Session snapshot save - END"
}

SAVED=0
FRESH=0
ANNOUNCED=0
# A short grace before the autosave route is accepted, so a clean shutdown that
# is about to land still wins: it unloads the session properly, the autosave
# only writes the world.
GRACE=20
for i in $(seq 1 "$SE_STOP_TIMEOUT"); do
  sleep 1
  kill -0 "$PID" 2>/dev/null || { SAVED=1; break; }
  _shutdown_done && { SAVED=1; break; }
  if [ "$i" -gt "$GRACE" ] && _autosave_done; then FRESH=1; break; fi
  if [ "$ANNOUNCED" = 0 ] && [ -n "$LOG" ] \
     && tail -c "+$((MARK + 1))" "$LOG" 2>/dev/null | grep -q "Exiting\.\."; then
    ANNOUNCED=1
    echo "                      request accepted, saving."
  fi
  # The game does not always honour the request at once, and one attempt is not
  # enough: measured between 0.1 s and never, the "never" seen with players
  # connected. So ask again, and keep waiting for the autosave meanwhile.
  if [ $((i % 30)) = 0 ]; then
    echo "                      no answer after ${i}s, asking again."
    _ask
  fi
done

if [ "$SAVED" = 1 ]; then
  echo "World saved         : by the shutdown itself, $(date '+%H:%M:%S')."
elif [ "$FRESH" = 1 ]; then
  echo "No shutdown, but     : an autosave just completed, $(date '+%H:%M:%S')."
  echo "                      cutting now costs seconds, not minutes."
else
  # Neither route worked. Fall back to the old guard: refuse to cut when the
  # save on disk is too old, because cutting now loses everything since.
  echo "NO RESPONSE after ${SE_STOP_TIMEOUT}s, and no autosave in that window."
  if [ -f "$SAVE" ]; then
    AGE=$(( $(date +%s) - $(stat -f "%m" "$SAVE") ))
    echo "Last save on disk   : $((AGE/60))m$((AGE%60))s ago."
  else
    # No save file where it is expected: either SE_WORLD is wrong or the world
    # has never been saved. Say so instead of pretending the save is fresh,
    # because AGE=0 would silently disarm the guard below.
    AGE=$(( SE_SAVE_MAX_AGE + 1 ))
    echo "Save file not found : $SAVE"
    echo "                      (check SE_WORLD, currently \"$SE_WORLD\")"
  fi
  if [ "$FORCE" != 1 ] && [ "$AGE" -gt "$SE_SAVE_MAX_AGE" ]; then
    echo
    echo "REFUSING TO STOP: the save is older than $((SE_SAVE_MAX_AGE/60))m$((SE_SAVE_MAX_AGE%60))s."
    echo "Everything built or changed since would be LOST."
    echo "  - the server may still be loading, wait and run this again"
    echo "  - or force with: stop.sh --force"
    exit 1
  fi
fi

# The process never exits by itself, saved or not: it sits on "press any key",
# and no key sent from a script reaches it. Cutting here costs nothing, the
# world is already on disk.
kill -TERM "$PID" 2>/dev/null
for i in $(seq 1 8); do sleep 2; kill -0 "$PID" 2>/dev/null || break; done
kill -0 "$PID" 2>/dev/null && { kill -9 "$PID" 2>/dev/null; sleep 2; }
tmux kill-session -t "$SESSION" 2>/dev/null

if [ "$SAVED" = 1 ]; then
  echo "Server stopped, world saved by the game itself."
elif [ "$FRESH" = 1 ]; then
  echo "Server stopped, world at the autosave taken seconds ago."
else
  echo "Server stopped (world is at the state of the save reported above)."
fi
