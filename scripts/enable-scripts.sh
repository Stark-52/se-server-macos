#!/bin/bash
# Enable (or disable) the programmable block on the world.
#
# Console players cannot run custom scripts in single player or in a
# console-hosted session: Microsoft and Sony forbid it. On a dedicated server
# the programmable block works, and console players get it by joining.
#
# EnableIngameScripts has to be written in the THREE files that describe the
# world, because the <SessionSettings> block of the .cfg is ignored when an
# existing world is loaded (see "The three-files rule" in the README):
#
#   Saves/<world>/Sandbox_config.sbc     the one that actually applies
#   Saves/<world>/Sandbox.sbc            the world itself
#   SpaceEngineers-Dedicated.cfg         the server config
#
# Editing a world file while the server runs is pointless: the next autosave
# rewrites it from memory. So this stops first, edits, then starts again.
#
# Usage:
#   ./scripts/enable-scripts.sh            enable, honouring the save guard
#   ./scripts/enable-scripts.sh --force    enable, stopping even on a stale save
#   ./scripts/enable-scripts.sh --off      disable again

set -u

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

VALUE="true"
FORCE=""
for arg in "$@"; do
  case "$arg" in
    --off|--disable) VALUE="false" ;;
    --force)         FORCE="--force" ;;
    # Print the header comment block and stop at the first line that is not a
    # comment. A fixed line range drifts as soon as the block is edited: it
    # used to print "set -u" as if it were help text.
    -h|--help)       awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)               echo "Unknown option: $arg"; exit 64 ;;
  esac
done

FILES=(
  "$SE_SAVE_DIR/Sandbox_config.sbc"
  "$SE_SAVE_DIR/Sandbox.sbc"
  "$SE_APPDATA/SpaceEngineers-Dedicated.cfg"
)

# Refuse to guess. A wrong SE_WORLD would otherwise "succeed" on zero files.
if [ ! -d "$SE_SAVE_DIR" ]; then
  echo "World folder not found : $SE_SAVE_DIR"
  echo "                         (check SE_WORLD, currently \"$SE_WORLD\")"
  exit 1
fi

# Stop first. stop.sh keeps its save-age guard, so a refusal here is a refusal
# to lose work, not a failure of this script.
if se_running; then
  echo "Stopping the server before editing the world..."
  if ! "$SE_ROOT/scripts/stop.sh" $FORCE; then
    echo
    echo "Not editing anything: the server is still running."
    exit 1
  fi
  RESTART=1
else
  echo "Server is not running: editing the files directly."
  RESTART=0
fi

TAG="EnableIngameScripts"
touched=0
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    echo "  skipped (absent) : $f"
    continue
  fi
  if ! grep -q "<$TAG>" "$f"; then
    # The tag is written by the game itself. Its absence means this file is not
    # what we think it is, and inserting XML blind is how worlds get corrupted.
    echo "  skipped (no <$TAG>) : $f"
    continue
  fi
  # BSD sed (macOS): -i takes a mandatory backup suffix, empty means in place.
  sed -i '' "s|<$TAG>[^<]*</$TAG>|<$TAG>$VALUE</$TAG>|g" "$f"
  echo "  $TAG=$VALUE : $f"
  touched=$((touched + 1))
done

if [ "$touched" = 0 ]; then
  echo
  echo "Nothing was changed. Start the server once so it writes the world files."
  exit 1
fi

# Space Engineers reads this stale compressed copy in preference to the file we
# just edited, and the edit is silently ignored.
rm -f "$SE_SAVE_DIR/SANDBOX_0_0_0_.sbsB5"

echo
if [ "$VALUE" = "true" ]; then
  echo "Programmable block enabled in $touched file(s)."
  echo "Note: EnableIngameScripts forces the world into Experimental mode."
else
  echo "Programmable block disabled in $touched file(s)."
fi

if [ "$RESTART" = 1 ]; then
  echo
  exec "$SE_ROOT/scripts/start.sh"
fi
echo "Server was already stopped: start it with ./scripts/start.sh"
