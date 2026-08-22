#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Shared resolution for every script in this repository.
#
# Sourced, never executed. It answers three questions once, so start.sh and
# stop.sh can never disagree about them:
#   - where is the repository root
#   - which configuration file applies
#   - where does the server actually keep its saves and logs
#
# That last point is not cosmetic: a helper script pointing at a different
# world than stop.sh is a save-guard that silently protects the wrong file.
# ---------------------------------------------------------------------------

# Repository root, derived from THIS file's location (scripts/..), so the
# checkout can live anywhere. An exported SE_ROOT still wins.
SE_ROOT="${SE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Local configuration first, shipped example as fallback.
if [ -f "$SE_ROOT/config.sh" ]; then
  # shellcheck source=/dev/null
  . "$SE_ROOT/config.sh"
elif [ -f "$SE_ROOT/config.example.sh" ]; then
  # shellcheck source=/dev/null
  . "$SE_ROOT/config.example.sh"
fi

# Defaults, applied again here so the scripts still run with no config file at
# all (a deleted config.example.sh, a partial copy, a stripped-down install).
SE_WORLD="${SE_WORLD:-MyWorld}"
SE_PREFIX="${SE_PREFIX:-$SE_ROOT/prefix}"
SE_GAME="${SE_GAME:-$SE_ROOT/game/DedicatedServer64}"
SE_TMUX_SESSION="${SE_TMUX_SESSION:-se}"
SE_START_TIMEOUT="${SE_START_TIMEOUT:-900}"
SE_SAVE_MAX_AGE="${SE_SAVE_MAX_AGE:-180}"
SE_STOP_TIMEOUT="${SE_STOP_TIMEOUT:-300}"
SE_START_ATTEMPTS="${SE_START_ATTEMPTS:-3}"

# Homebrew lives in /opt/homebrew on Apple Silicon and /usr/local on Intel.
# Ask brew, and only guess if brew itself is not on the PATH yet.
export PATH="$(brew --prefix 2>/dev/null || echo /opt/homebrew)/bin:$PATH"

# Wine account name inside the prefix.
# $USER is empty under cron and launchd, hence id -un. And the folder Wine
# created does not always match the macOS account, so if the expected folder is
# missing we take the single real user folder present (Public is Wine's own).
se_resolve_wine_user() {
  local candidate
  candidate="${SE_WINE_USER:-${USER:-$(id -un)}}"
  if [ -d "$SE_PREFIX/drive_c/users/$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  local d
  for d in "$SE_PREFIX"/drive_c/users/*/; do
    [ -d "$d" ] || continue
    d="$(basename "$d")"
    case "$d" in
      Public|crossover) continue ;;
    esac
    printf '%s\n' "$d"
    return 0
  done
  # Nothing on disk yet (prefix not bootstrapped): keep the expected name so
  # the caller reports a path instead of an empty one.
  printf '%s\n' "$candidate"
}

SE_WINE_USER="$(se_resolve_wine_user)"

# Server data root inside the prefix: logs sit here, saves under Saves/.
SE_APPDATA="$SE_PREFIX/drive_c/users/$SE_WINE_USER/AppData/Roaming/SpaceEngineersDedicated"
SE_SAVE_DIR="$SE_APPDATA/Saves/$SE_WORLD"

# PID du jeu, et de lui seul.
#
# `pgrep -f` cherche dans la ligne de commande ENTIERE. Le processus tmux porte
# la commande wine dans ses arguments, donc il correspond lui aussi, et son PID
# est toujours le plus petit des deux puisque c'est lui qui lance le jeu : un
# `pgrep -f ... | head -1` designait donc SYSTEMATIQUEMENT tmux. Un signal
# envoye a ce PID coupait le pty, et le jeu mourait sans jamais rien recevoir.
# On filtre donc sur le NOM du processus, qui lui ne ment pas.
se_pid() {
  local p
  for p in $(pgrep -f "SpaceEngineersDedicated\.exe" 2>/dev/null); do
    case "$(ps -o comm= -p "$p" 2>/dev/null)" in
      *SpaceEngineersDedicated.exe) printf '%s\n' "$p"; return 0 ;;
    esac
  done
  return 1
}

# Vrai si le jeu tourne. A preferer a `pgrep -f`, pour la meme raison.
se_running() { se_pid >/dev/null; }
