#!/bin/bash
# ---------------------------------------------------------------------------
# Bringing the server back after a crash it cannot survive.
#
# Space Engineers dedicated servers die at random inside Havok, the physics
# engine. The stack is always the same shape:
#
#     Havok.HkJobQueue:HkJobQueue_ProcessAllJobs
#     Sandbox.Engine.Physics.MyPhysics:StepWorldsParallel
#     Sandbox.Engine.Physics.MyPhysics:StepWorlds
#
# A read through a dead pointer, in closed native code that cannot be patched.
#
# The signature is not specific to this setup: unmodded servers on Windows
# report the identical stack and Keen has published no fix. How OFTEN it lands
# is another question entirely, and one this setup may well make worse, since
# it runs on Wine Mono rather than the .NET Framework the game expects, and the
# crash sits exactly on the managed-to-native boundary. Frequency is what
# crashes.log is for: measure before believing any explanation, this one
# included.
#
# Either way the crash is not something to repair, only something to survive.
# That is this script's entire job.
#
# ⚠️ The one trap here: A RUNNING PROCESS IS NOT A RUNNING SERVER.
# The thread that dies holds a critical section, so every other thread then
# blocks on it forever (err:sync:RtlpWaitForCriticalSection ... retrying), and
# Wine puts up its "Program Error" dialog. The process stays in the list, it
# still burns CPU, se_pid() still finds it, and the panel still says "online".
# It simply no longer simulates or saves anything. Presence proves nothing.
#
# What proves the server is alive is that it is STILL WRITING. The game logs
# "GC Memory:" every 30 s no matter what, so a log that has not been touched
# for a couple of minutes is a dead server, whatever `ps` says.
# ---------------------------------------------------------------------------

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

SE_ROOT="${SE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=common.sh
. "$_CODE/common.sh"

# Silence that means death. The game writes every 30 s, so 150 s is five missed
# writes: long enough that a stalled disk or a long autosave cannot trip it,
# short enough that nobody sits in front of a dead server for long.
SILENCE="${SE_WATCHDOG_SILENCE:-150}"
# Give up rather than hammer: a world that crashes on load would otherwise be
# reloaded forever, and every attempt costs three minutes of mod downloads.
MAX_RESTARTS="${SE_WATCHDOG_MAX_RESTARTS:-5}"
WINDOW="${SE_WATCHDOG_WINDOW:-3600}"

RUN="$SE_ROOT/run"
LOCK="$RUN/watchdog.lock"
STOPPED="$RUN/stopped-on-purpose"
HISTORY="$RUN/restarts"
WLOG="$SE_ROOT/logs/watchdog.log"
CRASHLOG="$SE_ROOT/logs/crashes.log"
mkdir -p "$RUN" "$SE_ROOT/logs"

_say() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$WLOG"; }

# --- lock -----------------------------------------------------------------
# mkdir is atomic on every filesystem that matters, which mattering flag files
# written with > are not. A restart takes minutes and launchd fires every
# minute, so overlapping runs are the normal case, not the exception.
_lock() {
  if mkdir "$LOCK" 2>/dev/null; then
    echo $$ > "$LOCK/pid"; return 0
  fi
  local owner age
  owner=$(cat "$LOCK/pid" 2>/dev/null)
  # A live owner keeps the lock. A dead one held it because we were killed
  # mid-restart, and refusing to ever break it would disable the watchdog for
  # good, which is a worse failure than one extra restart.
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then return 1; fi
  age=$(( $(date +%s) - $(stat -f "%m" "$LOCK" 2>/dev/null || echo 0) ))
  [ "$age" -lt 1200 ] && return 1
  _say "stale lock from PID ${owner:-?} (${age}s), taking it"
  rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null && { echo $$ > "$LOCK/pid"; return 0; }
  return 1
}
_unlock() { rm -rf "$LOCK"; }

# --- newest logs ----------------------------------------------------------
_game_log()    { ls -t "$SE_APPDATA"/*.log 2>/dev/null | head -1; }
_console_log() { ls -t "$SE_ROOT"/logs/console-*.log 2>/dev/null | head -1; }

# Seconds since the game last wrote anything.
_silence() {
  local l; l=$(_game_log); [ -n "$l" ] || return 1
  echo $(( $(date +%s) - $(stat -f "%m" "$l") ))
}

# The Havok signature, if the console capture caught it. Absence proves
# nothing: the game writes none of this to its own log, it goes to the console,
# and Wine's crash dialog swallows the backtrace entirely until ShowCrashDialog
# is turned off. See the README for that registry key.
# Did the game shut ITSELF down, whoever asked it to?
#
# The stopped-on-purpose marker only covers stops that went through stop.sh.
# Ctrl+C typed into the console is the documented manual way to stop this
# server and writes no marker; neither does anything else that asks the game to
# exit. Restarting over those makes the server impossible to stop by hand,
# which is exactly what happened before this check existed.
#
# The game's own log settles it. "Exiting.." is written ONLY when a shutdown
# request is accepted, and is followed by the shutdown save and "Log Closed".
# A crash can never produce it: it stops the log mid-frame, which is precisely
# what makes a crash recognisable. So this is authoritative in both directions,
# and it does not care how the stop was asked for.
_exited_cleanly() {
  local l; l=$(_game_log); [ -n "$l" ] || return 1
  grep -q "Exiting\.\." "$l" 2>/dev/null
}

_crash_signature() {
  local c; c=$(_console_log); [ -n "$c" ] || return 1
  grep -oE "at Havok\.[A-Za-z]+:[A-Za-z_]+|Unhandled page fault on [a-z]+ access|Got a [A-Z]+ while executing native code" "$c" \
    | tail -3 | tr '\n' ' '
}

# --- restart budget -------------------------------------------------------
_recent_restarts() {
  [ -f "$HISTORY" ] || { echo 0; return; }
  local cut; cut=$(( $(date +%s) - WINDOW ))
  awk -v c="$cut" '$1 > c' "$HISTORY" | wc -l | tr -d ' '
}

# --- the crash record -----------------------------------------------------
# An unwatched crash leaves nothing behind but a game log that stops
# mid-sentence, which says neither when it died nor how long it had been up.
# One line per crash makes the frequency measurable, which is the only way to
# tell whether a change to the world or the settings actually helped.
_record_crash() {
  local reason="$1" log up sig
  log=$(_game_log)
  # One crashed session is one line, however many attempts it takes to come
  # back. A restart that fails leaves the same dead log in place, so without
  # this the retry records the crash again and the history overstates the very
  # frequency it exists to measure. The dead log's name is the session's
  # identity: the server opens a new one each time it starts.
  if [ -n "$log" ] && [ -f "$CRASHLOG" ] \
     && tail -1 "$CRASHLOG" | grep -qF "log=$(basename "$log")"; then
    _say "same session as the last recorded crash, not recording twice"
    return 0
  fi
  if [ -n "$log" ]; then
    up=$(( ( $(stat -f "%m" "$log") - $(stat -f "%B" "$log") ) / 60 ))
  else up="?"; fi
  sig=$(_crash_signature)
  printf '%s\tuptime=%smin\treason=%s\tworld=%s\tsig=%s\tlog=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$up" "$reason" "$SE_WORLD" \
    "${sig:-none captured}" "$(basename "${log:-none}")" >> "$CRASHLOG"
  _say "CRASH  uptime ${up}min  ${sig:-no signature in console capture}"
}

# --- killing a corpse -----------------------------------------------------
# kill -9 is normally forbidden here, because one landing during an autosave
# can corrupt the world. It is safe on a crashed server, and only there,
# because the game writes a save into Saves/<world>/.new/ and swaps it into
# place at the end. A process killed mid-save therefore leaves a stray .new
# directory and an untouched world. Verified in the logs:
#   Saving Sandbox world configuration file ...\<world>\.new\Sandbox_config.sbc
#
# And nothing gentler works: the thread that would answer SIGINT is the dead
# one. stop.sh's clean shutdown needs a live game, which is exactly what we
# no longer have.
_kill_corpse() {
  local pid; pid=$(se_pid) || return 0
  _say "killing the crashed process (PID $pid)"
  kill -9 "$pid" 2>/dev/null
  local i; for i in $(seq 1 10); do sleep 1; se_running || break; done
  se_running && _say "WARNING: PID $(se_pid) survived kill -9"
  tmux kill-session -t "$SE_TMUX_SESSION" 2>/dev/null
  # Wine leaves the debugger holding the prefix after a crash; it would fight
  # the next start for the same prefix lock.
  pkill -f "winedbg" 2>/dev/null
  return 0
}

_restart() {
  local reason="$1"
  local n; n=$(_recent_restarts)
  if [ "$n" -ge "$MAX_RESTARTS" ]; then
    _say "REFUSING TO RESTART: $n restarts in the last $((WINDOW/60))min."
    _say "  The world itself is probably the problem, not luck. Look at $CRASHLOG"
    _say "  before restarting by hand, or raise SE_WATCHDOG_MAX_RESTARTS."
    return 1
  fi
  _record_crash "$reason"
  _kill_corpse
  date +%s >> "$HISTORY"
  _say "restarting (attempt $((n+1))/$MAX_RESTARTS in this window)"
  # Call start.sh through the INSTALLATION, never through $_CODE.
  #
  # start.sh reads SE_ROOT from the path it was CALLED as, precisely so that a
  # symlink into the repository still designates this installation. Calling it
  # by its resolved path therefore points it at the repository, where there is
  # no game and no prefix, and it exits with "SpaceEngineersDedicated.exe not
  # found". Exporting SE_ROOT as well makes it right even for an installation
  # that has no scripts/ symlinks of its own.
  local starter="$SE_ROOT/scripts/start.sh"
  [ -x "$starter" ] || [ -f "$starter" ] || starter="$_CODE/start.sh"
  if SE_ROOT="$SE_ROOT" bash "$starter" >> "$WLOG" 2>&1; then
    _say "server back up"
  else
    _say "RESTART FAILED, see $WLOG"
    return 1
  fi
}

# --- the check ------------------------------------------------------------
_check() {
  # Someone typed stop.sh. Not our business, and restarting over it would make
  # stopping the server impossible.
  if [ -f "$STOPPED" ]; then return 0; fi

  # start.sh is already working, and it has its own retry loop. Two restart
  # policies running at once is how a server ends up loading twice.
  # Match on "scripts/start.sh", not on $_CODE: start.sh is normally reached
  # through the installation's symlink, so its command line carries the
  # installation path, not the resolved one.
  if pgrep -f "scripts/start\.sh" >/dev/null 2>&1 || [ -f "$RUN/starting" ]; then
    return 0
  fi

  # Checked before anything else about the process, because both states can
  # follow a deliberate stop: the game lingers on "press any key to close this
  # window" after saving, so a Ctrl+C nobody finished off looks frozen, and
  # once it is gone it looks crashed. Neither is ours to undo.
  # The marker is written here too, so this costs one grep rather than one per
  # minute, and `status` can say why it is standing down.
  if _exited_cleanly; then
    _say "the game shut itself down cleanly (Exiting..), standing down"
    : > "$STOPPED"
    return 0
  fi

  local log; log=$(_game_log)

  if se_running; then
    [ -n "$log" ] || return 0
    # Still loading: start.sh owns that phase, and mods take three minutes
    # during which the log goes quiet for long stretches.
    grep -q "Game ready" "$log" 2>/dev/null || return 0
    local q; q=$(_silence) || return 0
    if [ "$q" -gt "$SILENCE" ]; then
      _say "process alive but silent for ${q}s: crashed, not running"
      _restart "frozen"
    fi
    return 0
  fi

  # No process, and nobody asked for that. Either the crash dialog was
  # dismissed, or ShowCrashDialog is off and Wine let it die on its own.
  _say "server is down and stop.sh was not used"
  _restart "gone"
}

# --- the daemon -----------------------------------------------------------
# Why a plain background loop and not a launchd agent.
#
# A launchd agent gets no access to the user's protected folders, and on macOS
# that includes ~/Documents, ~/Desktop and ~/Downloads. An installation living
# in one of them is unreachable: the agent dies before it starts with
#
#     /bin/bash: .../watchdog.sh: Operation not permitted   (exit 126)
#
# and even if the script were elsewhere, it still could not read the game logs
# it exists to watch. Granting Full Disk Access to /bin/bash would fix it, and
# would also hand every script on the machine the same access. Not worth it.
#
# Started from a terminal or from start.sh, the loop inherits the session's own
# access and simply works. The cost is honest and stated in the README: it does
# not come back by itself after a reboot, start.sh brings it back.
INTERVAL="${SE_WATCHDOG_INTERVAL:-60}"
PIDFILE="$RUN/watchdog.pid"

_daemon_pid() {
  local p; p=$(cat "$PIDFILE" 2>/dev/null) || return 1
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null && printf '%s\n' "$p"
}

_daemon() {
  echo $$ > "$PIDFILE"
  trap 'rm -f "$PIDFILE"' EXIT
  _say "watchdog running (PID $$), checking every ${INTERVAL}s"
  while :; do
    if _lock; then _check; _unlock; fi
    sleep "$INTERVAL"
  done
}

_start() {
  local p
  if p=$(_daemon_pid); then echo "Watchdog already running (PID $p)."; return 0; fi
  rm -f "$PIDFILE"
  # Hand SE_ROOT down explicitly. The daemon is another run of this script, and
  # this script derives SE_ROOT from where it was called from: spawned by its
  # resolved path it would adopt the REPOSITORY as the installation, write its
  # pidfile into the repository's run/, and be invisible to every later call
  # made through the installation. It would then sit there watching a prefix
  # that does not exist. Same two-roots trap as start.sh and stop.sh.
  local self="$SE_ROOT/scripts/watchdog.sh"
  [ -f "$self" ] || self="$_CODE/watchdog.sh"
  # setsid does not exist on macOS; nohup plus a subshell detaches well enough
  # that closing the terminal does not take the watchdog with it.
  ( SE_ROOT="$SE_ROOT" nohup bash "$self" daemon >/dev/null 2>&1 & )
  local i; for i in $(seq 1 10); do
    sleep 1; p=$(_daemon_pid) && { echo "Watchdog started (PID $p)."; return 0; }
  done
  echo "Watchdog FAILED to start, see $WLOG"; return 1
}

_stop_daemon() {
  local p; p=$(_daemon_pid) || { echo "Watchdog not running."; return 0; }
  kill "$p" 2>/dev/null; rm -f "$PIDFILE"; _say "watchdog stopped (was PID $p)"
}

# --- launchd (optional, needs Full Disk Access) ----------------------------
PLIST="$HOME/Library/LaunchAgents/com.se-server-macos.watchdog.plist"

_install() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.se-server-macos.watchdog</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SE_ROOT/scripts/watchdog.sh</string>
    <string>check</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>SE_ROOT</key><string>$SE_ROOT</string></dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$SE_ROOT/logs/watchdog-launchd.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null
  launchctl load "$PLIST" || { echo "launchctl refused the agent."; return 1; }

  # Never report this installed without seeing it run. launchctl loads an agent
  # that cannot execute at all just as happily as one that can, and the failure
  # this catches is the common one: an installation under ~/Documents, which a
  # launchd agent may not read. Announcing success there would leave a watchdog
  # that watches nothing, which is worse than none at all.
  sleep 3
  local st
  st=$(launchctl list 2>/dev/null | awk '$3 == "com.se-server-macos.watchdog" {print $2}')
  if [ -n "$st" ] && [ "$st" != "0" ]; then
    launchctl unload "$PLIST" 2>/dev/null; rm -f "$PLIST"
    echo "The launchd agent cannot run this installation (exit $st)."
    [ "$st" = "126" ] && cat <<'TXT'

  Exit 126 is macOS protecting a folder: launchd agents get no access to
  ~/Documents, ~/Desktop or ~/Downloads, and this installation lives in one.
  Reading the game logs from there would fail for the same reason.

  Use the background watchdog instead, which inherits your session's access:

      ./scripts/watchdog.sh start

  start.sh also starts it for you. To keep launchd anyway, grant Full Disk
  Access to /bin/bash in System Settings > Privacy & Security, which gives
  every script on the machine that access too.
TXT
    return 1
  fi
  _say "watchdog installed as a launchd agent, checking every 60s"
}

_uninstall() {
  launchctl unload "$PLIST" 2>/dev/null
  rm -f "$PLIST"; _say "watchdog removed"
}

_status() {
  local p st
  if p=$(_daemon_pid); then
    echo "Watchdog            : running (PID $p), every ${INTERVAL}s"
  elif st=$(launchctl list 2>/dev/null | awk '$3 == "com.se-server-macos.watchdog" {print $2}') \
       && [ -n "$st" ]; then
    # A loaded agent is not a working one: report the exit status, since the
    # interesting case is exactly the one that loads and never runs.
    if [ "$st" = "0" ]; then
      echo "Watchdog            : launchd agent, every 60s"
    else
      echo "Watchdog            : launchd agent LOADED BUT FAILING (exit $st)"
      echo "                      run: watchdog.sh uninstall && watchdog.sh start"
    fi
  else
    echo "Watchdog            : NOT running  (watchdog.sh start)"
  fi
  if [ -f "$STOPPED" ]; then
    echo "Server              : stopped on purpose, watchdog standing down"
  elif se_running; then
    q=$(_silence)
    if [ -n "$q" ] && [ "$q" -gt "$SILENCE" ]; then
      echo "Server              : PROCESS ALIVE BUT SILENT ${q}s -> crashed"
    else
      echo "Server              : running, last wrote ${q:-?}s ago"
    fi
  else
    echo "Server              : down"
  fi
  echo "Restarts (last $((WINDOW/60))min): $(_recent_restarts)/$MAX_RESTARTS"
  if [ -f "$CRASHLOG" ]; then
    echo "Crashes recorded    : $(wc -l < "$CRASHLOG" | tr -d ' ')  ($CRASHLOG)"
    tail -3 "$CRASHLOG" | sed 's/^/                      /'
  fi
}

case "${1:-check}" in
  start)     _start ;;
  stop)      _stop_daemon ;;
  daemon)    _daemon ;;
  check)     _lock || exit 0; trap _unlock EXIT; _check ;;
  install)   _install ;;
  uninstall) _uninstall ;;
  status)    _status ;;
  *) cat <<'TXT'
usage: watchdog.sh <command>

  start      run the watchdog in the background (what start.sh does)
  stop       stop it
  status     what it thinks, and the crash history
  check      run one check now, in the foreground
  daemon     run the loop in the foreground (start uses this)
  install    install a launchd agent instead; needs Full Disk Access, and
             refuses when the installation sits in a protected folder
  uninstall  remove that agent
TXT
  exit 1 ;;
esac
