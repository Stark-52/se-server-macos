#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Example configuration for the Space Engineers dedicated server on macOS.
#
# Copy this file to config.sh and edit it:
#
#     cp config.example.sh config.sh
#
# config.sh is git-ignored, so your own paths, world name and server name never
# reach the repository. When config.sh is absent the scripts source this file
# instead, so a fresh clone still runs with sane defaults.
#
# Every value uses the ":=" form, so anything already set in the environment
# wins over the default below, e.g.
#
#     SE_WORLD=AnotherWorld ./scripts/start.sh
#
# These are shell variables, not exported ones: this file is SOURCED by
# scripts/common.sh, and the settings panel sources it the same way. Sourcing
# it by hand in your own shell therefore does NOT pass anything to unrelated
# child processes, which is why nothing here is exported (exporting SE_ROOT in
# particular would defeat the auto-detection of the checkout root).
# ---------------------------------------------------------------------------

# Repository / installation root.
# The scripts auto-locate it from their own position, so this default only
# applies when the file is sourced on its own. Never hardcode an absolute
# path under a user home here.
: "${SE_ROOT:=$HOME/SEServer}"

# Name of the world folder under Saves/. This is the directory Space Engineers
# created for the save, not the "world name" shown in the server browser.
: "${SE_WORLD:=MyWorld}"

# Wine prefix holding the server installation and its AppData tree.
# Created by the bootstrap step, never committed (it is several GB and its
# drive_c/users/<name> folder leaks the account name).
: "${SE_PREFIX:=$SE_ROOT/prefix}"

# tmux session name the server runs in. Keep it short: you type it a lot
# ("tmux attach -t se").
: "${SE_TMUX_SESSION:=se}"

# How long to wait, in SECONDS, for a start attempt to reach "Game ready"
# before giving up and retrying. A vanilla world is ready in well under a
# minute; a dozen mods (planets especially) can take several minutes, so the
# default is deliberately generous.
: "${SE_START_TIMEOUT:=900}"

# How long to wait, in SECONDS, for stop.sh to reach a safe point.
#
# Two outcomes end the wait: the game honours the shutdown request and saves,
# or an autosave completes and cutting becomes free. The default must therefore
# comfortably exceed AutoSaveInMinutes: raise it if the world autosaves less
# often than every five minutes.
#
# A running server usually answers in about two seconds. But it queues the
# request while the world is loading, and it has been seen ignoring it outright
# for minutes with players connected: hence the wait, and hence the autosave
# route out of it.
: "${SE_STOP_TIMEOUT:=420}"

# Maximum age, in SECONDS, of the save file for stop.sh to accept cutting the
# server FOR LACK OF ANYTHING BETTER. It is a fallback: stop.sh first asks the
# game to shut down cleanly, which saves. This guard only applies when the game
# did not answer within SE_STOP_TIMEOUT, where cutting would lose everything
# built since the last autosave. Above this age stop.sh refuses and asks for
# --force.
: "${SE_SAVE_MAX_AGE:=180}"

# --- Optional overrides -----------------------------------------------------

# Directory holding SpaceEngineersDedicated.exe. Fetch it with steamcmd
# (+app_update 298740); it is not part of this repository.
: "${SE_GAME:=$SE_ROOT/game/DedicatedServer64}"

# Account name of the Windows user inside the Wine prefix. It usually matches
# the macOS account, but not always, and $USER is empty under cron/launchd.
# The scripts fall back to a glob over drive_c/users/* when this name has no
# matching folder.
: "${SE_WINE_USER:=$(id -un)}"

# Number of start attempts before start.sh gives up.
: "${SE_START_ATTEMPTS:=3}"

# Local settings panel (panel/), when you use it.
#
# The panel has NO authentication, and applying settings rewrites the world
# files and runs stop.sh then start.sh. Anyone who can reach the port can do
# that. Hence the loopback default: only this machine can talk to it.
: "${SE_PANEL_HOST:=127.0.0.1}"
: "${SE_PANEL_PORT:=8777}"

# Binding the panel to anything other than a loopback address exposes an
# unauthenticated restart button to that network. The panel refuses to do it
# unless you say so explicitly here, so a stray SE_PANEL_HOST cannot open the
# LAN by accident. Set to 1 only on a network you control, and prefer an SSH
# tunnel instead:
#
#     ssh -N -L 8777:127.0.0.1:8777 user@the-mac
: "${SE_PANEL_ALLOW_REMOTE:=0}"

# NOTE: there is deliberately no SE_SERVER_NAME and no SE_PASSWORD here.
# Nothing in this repository writes SpaceEngineers-Dedicated.cfg, so such
# variables would be read by nobody, and a real password sitting in a variable
# that configures nothing is worse than no variable at all. The server name is
# <ServerName> in SpaceEngineers-Dedicated.cfg, and the password is only ever
# the <ServerPasswordHash> / <ServerPasswordSalt> pair: plain text does
# nothing there. See "Password" in the README.
