#!/bin/bash
# Dedicated Space Engineers server (EOS crossplay) on macOS, through Wine.
#
# The server runs inside a detached tmux session. Two reasons:
#  - the session survives closing the terminal and the agent that launched it
#  - it provides a REAL terminal, so stop.sh can deliver a real Ctrl+C, the
#    only method that triggers the shutdown save (SIGINT, SIGTERM, SIGHUP and
#    taskkill were all tested: none of them works).
#
# Wine must be running with Wine Mono, not the .NET Framework redistributable:
# the 32-bit .NET installer deadlocks under Wine on Apple Silicon and never
# completes. Bootstrap the prefix with Mono and leave it alone.

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
# <installation>/scripts/start.sh doit designer cette installation, meme quand
# le fichier est un symlink vers le depot. Prendre le chemin resolu ferait
# pointer SE_ROOT sur le depot, ou il n'y a ni jeu ni prefixe.
SE_ROOT="${SE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# shellcheck source=common.sh
. "$_CODE/common.sh"

export WINEPREFIX="$SE_PREFIX"
export WINEDEBUG=fixme-all
GAME="$SE_GAME"
SELOG="$SE_APPDATA"
SESSION="$SE_TMUX_SESSION"

# Poll every 5 s; SE_START_TIMEOUT is expressed in seconds.
POLL=5
STEPS=$(( SE_START_TIMEOUT / POLL ))
[ "$STEPS" -lt 1 ] && STEPS=1

if [ ! -x "$GAME/SpaceEngineersDedicated.exe" ] && [ ! -f "$GAME/SpaceEngineersDedicated.exe" ]; then
  echo "SpaceEngineersDedicated.exe not found in: $GAME"
  echo "Install the server files there (steamcmd +app_update 298740) or set SE_GAME."
  exit 1
fi

if pgrep -f "SpaceEngineersDedicated.exe" > /dev/null; then
  echo "Server already running (PID $(pgrep -f SpaceEngineersDedicated.exe | head -1))."
  exit 0
fi
tmux kill-session -t "$SESSION" 2>/dev/null

for try in $(seq 1 "$SE_START_ATTEMPTS"); do
  echo "--- Attempt $try/$SE_START_ATTEMPTS (up to $((SE_START_TIMEOUT / 60)) min; mods make loading much longer) ---"
  MARK=$(mktemp); sleep 1
  # wine runs DIRECTLY in the pane, with no caffeinate in the chain: that is
  # the only way a Ctrl+C typed by hand after "tmux attach -t se" has any
  # chance of reaching the server. caffeinate is started alongside instead,
  # watching the PID, so the Mac stays awake without polluting the chain.
  #
  # WINEPREFIX is re-injected into the command string because a tmux server
  # that is already running does not inherit the caller's environment.
  # printf %q keeps paths with spaces or quotes intact.
  tmux new-session -d -s "$SESSION" -c "$GAME" \
    "WINEPREFIX=$(printf '%q' "$WINEPREFIX") WINEDEBUG=fixme-all wine SpaceEngineersDedicated.exe -console"

  ok=0; fail=0
  for i in $(seq 1 "$STEPS"); do
    sleep "$POLL"
    L=$(find "$SELOG" -maxdepth 1 -name "*.log" -newer "$MARK" 2>/dev/null | head -1)
    [ -z "$L" ] && continue
    if grep -q "Game ready" "$L" 2>/dev/null; then ok=1; break; fi
    if grep -qE "CRASH INFO|FATAL UNHANDLED|Session can not start" "$L" 2>/dev/null; then fail=1; break; fi
  done

  if [ "$ok" = 1 ]; then
    SPID=$(pgrep -f "SpaceEngineersDedicated.exe" | head -1)
    # -i veille inactive, -m veille des disques, -s veille systeme sur secteur.
    # caffeinate seul n'assure que -i, ce qui laisse le Mac s'endormir sur
    # d'autres chemins. -w arrime l'assertion au PID du serveur : elle tombe
    # d'elle-meme a l'arret, sans processus orphelin.
    # Limite honnete : rabattre l'ecran endort la machine malgre tout.
    [ -n "$SPID" ] && (caffeinate -i -m -s -w "$SPID" >/dev/null 2>&1 &)
    echo "SERVER READY."
    grep -E "Networking service|Console compatibility|World Name" "$L" | sed 's/.*-> *//'
    echo "Live console       : tmux attach -t $SESSION   (detach: Ctrl+B then D)"
    echo "Only safe shutdown : attach and press Ctrl+C yourself."
    rm -f "$MARK"; exit 0
  fi

  tmux kill-session -t "$SESSION" 2>/dev/null
  pkill -f "SpaceEngineersDedicated.exe" 2>/dev/null; sleep 3
  rm -f "$MARK"
  [ "$fail" = 1 ] && echo "  crashed while starting, retrying." || echo "  timed out, retrying."
done

echo "FAILED after $SE_START_ATTEMPTS attempts. Logs: $SELOG"
exit 1
