# Space Engineers dedicated server on macOS

Runs the Windows dedicated server of Space Engineers 1 (build `01_210_014`) on a
Mac through Wine, with EOS crossplay enabled so console players can join.

The dedicated server is a .NET Framework 4.8 Windows application. Here it runs
under **Wine 11 with Wine Mono**, translated to x86-64 by Rosetta 2 on Apple
Silicon. No game code is patched: the official binaries are used as shipped.

This repository holds the scripts, the configuration surface and the notes.
It does not hold the game itself.

## What this does

- Starts and stops the official dedicated server binaries under Wine.
- Keeps the server in a detached `tmux` session, so it survives closing the
  terminal and still offers a live console.
- Retries a start that crashes or hangs, watching the server log for readiness.
- Stops the server by asking the game to shut down, which saves the world, and
  refuses to cut on an old save if the game does not answer.
- Documents the EOS crossplay configuration (console-compatible worlds,
  mod.io mods, password hashing, admin promotion).
- Ships a local web panel that reads and rewrites the world settings, then
  restarts the server.

## What this does not do

- **It does not ship the game.** The server files come from steamcmd
  (app `298740`, anonymous login). They are several GB and not ours to
  redistribute.
- **It does not stop the server instantly.** The shutdown is the game's own, so
  it happens when the game decides: measured between 0.1 s and 3 min on the
  same machine. See [Known limitations](#known-limitations).
- **It does not support Steam Workshop mods.** Crossplay excludes them.
  mod.io works and is the path used by Xbox and PlayStation.
- **It does not convert an existing save to crossplay.** A console-compatible
  world has to be started from a premade console-compatible world.
- **It is not a control panel for remote operation.** The settings panel binds
  to `127.0.0.1` and has no authentication: applying settings rewrites the
  world files and restarts the server, so anyone who can reach the port can do
  that. It refuses requests whose `Host` or `Origin` is not its own, which
  closes browser-driven access but not direct access to the port.
  `SE_PANEL_HOST` can bind elsewhere, and the panel refuses to unless
  `SE_PANEL_ALLOW_REMOTE=1` says so explicitly. To reach it from another
  machine, tunnel it rather than open it:

      ssh -N -L 8777:127.0.0.1:8777 user@the-mac

Built and used on Apple Silicon. Intel Macs are not tested; the scripts resolve
the Homebrew prefix instead of hardcoding one, but nothing else was verified
there.

## Requirements

- macOS on Apple Silicon, with Rosetta 2 installed:

      softwareupdate --install-rosetta --agree-to-license

- Homebrew.
- Wine 11, `tmux`, `steamcmd`:

      brew install --cask wine-stable
      brew install tmux steamcmd

- Gatekeeper quarantine lifted on the Wine bundle, otherwise it refuses to run:

      sudo xattr -dr com.apple.quarantine "/Applications/Wine Stable.app"
      wine --version        # expect wine-11.0

- Python 3 for the settings panel (the system `python3` is enough).
- Disk: about 8 GB for the server files, plus a few GB for the Wine prefix.

### If the Game Porting Toolkit is installed

Both packages want the same symlinks in `$(brew --prefix)/bin`: `wine64`,
`wine64-preloader`, `wineserver`. Record where they point, then remove them so
the `wine-stable` cask can install:

    ls -l "$(brew --prefix)/bin/wine64" "$(brew --prefix)/bin/wine64-preloader" "$(brew --prefix)/bin/wineserver"

The toolkit application itself stays intact and remains usable through its full
path under `/Applications/`. Keep the recorded targets outside the repository:
they are absolute paths from your machine, and `gptk-links-backup/` is
git-ignored for that reason.

## Install

### 1. Clone and configure

    git clone <this-repo> se-server-macos
    cd se-server-macos
    cp config.example.sh config.sh

`config.sh` is git-ignored. Every script derives the repository root from its
own location, so the checkout can live anywhere; `config.sh` only carries what
is specific to your install:

| Variable | Default | Meaning |
|---|---|---|
| `SE_ROOT` | auto-detected | Repository / installation root |
| `SE_WORLD` | `MyWorld` | Folder name under `Saves/` |
| `SE_PREFIX` | `$SE_ROOT/prefix` | Wine prefix |
| `SE_GAME` | `$SE_ROOT/game/DedicatedServer64` | Folder holding `SpaceEngineersDedicated.exe` |
| `SE_WINE_USER` | `$(id -un)` | Windows account name inside the prefix |
| `SE_TMUX_SESSION` | `se` | tmux session name |
| `SE_START_TIMEOUT` | `900` | Seconds to wait for `Game ready` per attempt |
| `SE_START_ATTEMPTS` | `3` | Start attempts before giving up |
| `SE_SAVE_MAX_AGE` | `180` | Seconds; above this age `stop.sh` refuses to stop |
| `SE_WATCHDOG_SILENCE` | `150` | Seconds without a log write before the server counts as dead |
| `SE_WATCHDOG_MAX_RESTARTS` | `5` | Restarts allowed per window before the watchdog gives up |
| `SE_WATCHDOG_WINDOW` | `3600` | Seconds that window covers |
| `SE_PANEL_HOST` | `127.0.0.1` | Address the settings panel listens on |
| `SE_PANEL_PORT` | `8777` | Local settings panel port |
| `SE_PANEL_ALLOW_REMOTE` | `0` | Set to `1` to allow a non-loopback `SE_PANEL_HOST` |

`config.sh` is sourced by `scripts/common.sh`, and `panel/settings.py` sources
it the same way, so one file configures both and they cannot end up pointing at
two different worlds. Its values are shell variables, not exported ones:
sourcing it in your own shell does not, by itself, configure anything else.

There is deliberately no variable for the server name or the password: they are
not per-installation configuration, they are server state. The settings panel
edits both directly in `SpaceEngineers-Dedicated.cfg`, and the password only
ever exists there as a hash and a salt; see [Password](#password).

Any variable can also be overridden per invocation:

    SE_WORLD=AnotherWorld ./scripts/start.sh

#### If you did not clone: `git init` first

`.gitignore` only does something inside a git repository. A copy of this tree
unpacked beside a checkout, or started from scratch, has no `.git` at all: every
ignore rule is inert, and the first `git add -A` takes whatever happens to be on
the disk at that moment.

    git init
    git add -A
    git status --short        # read this list before committing

Expect the files listed in [Repository contents](#repository-contents) and
nothing else. If `prefix*/`, `game/`, a `.sbc`, a `.sbs`, a `.log` or your
`config.sh` appears there, fix the ignore rules before the commit: afterwards
the leak is in the history, and a later `.gitignore` will not remove it.

### 2. Fetch the server files

    steamcmd +force_install_dir "$PWD/game" +login anonymous +app_update 298740 validate +quit

App `298740` is the Space Engineers Dedicated Server. It downloads anonymously:
no Steam account, no game licence on the machine. `game/` is git-ignored.

### 3. Create the Wine prefix

    export WINEPREFIX="$PWD/prefix"
    wineboot --init
    wineserver -w

This installs **Wine Mono** into the prefix from the copy bundled inside the
`wine-stable` cask. That is the .NET runtime the server will use. Do not
install the Microsoft .NET Framework instead: see
[The .NET Framework trap](#the-net-framework-trap).

### 4. Install the Visual C++ 2015-2019 x64 redistributable

Required. Without it the server refuses to start. The installer ships with the
server files:

    WINEPREFIX="$PWD/prefix" wine "game/_CommonRedist/vcredist/2019/VC_redist.x64.exe" /quiet /norestart

### 5. First start

    ./scripts/start.sh
    tmux attach -t se        # live console, detach with Ctrl+B then D

The first start creates the server data tree inside the prefix (see
[Where the files live](#where-the-files-live)), including
`SpaceEngineers-Dedicated.cfg`, which is where crossplay, the world path and
the password hash are configured.

## The .NET Framework trap

This is the single point that decides whether the server runs at all.

The dedicated server targets .NET Framework 4.8. The Microsoft redistributable
shipped with the game, `game/_CommonRedist/DotNet/4.8/ndp48-x86-x64-allos-enu.exe`,
is a **32-bit binary** and deadlocks under the WoW64 layer of Wine 11. It was
tried twice, in Windows 10 mode and in Windows 7 mode, and hung at the same
place both times.

**Wine Mono bypasses the problem completely.** It is Wine's own .NET
implementation, it is 64-bit, it installs in seconds during `wineboot --init`,
and the dedicated server runs on it.

One consequence worth knowing: the `dotnet48` verb of `winetricks` removes Wine
Mono from the prefix as its first step, before it starts installing anything.
If you tried that route and it deadlocked, the prefix is left with no .NET
runtime at all. Delete the prefix and recreate it with `wineboot --init`.

## Configuration

### Where the files live

Repository:

    config.example.sh          copy to config.sh, git-ignored
    mods.example.txt           copy to mods.txt, git-ignored
    scripts/common.sh          shared path and config resolution
    scripts/start.sh           start
    scripts/stop.sh            stop, with the save-age guard
    scripts/watchdog.sh        crash detection and restart, launchd agent
    scripts/enable-scripts.sh  programmable block on/off, across the three files
    panel/settings.py          local settings page
    game/                      server binaries (git-ignored, steamcmd)
    prefix/                    Wine prefix (git-ignored, recreate with wineboot)

Server data, inside the prefix:

    prefix/drive_c/users/<your-user>/AppData/Roaming/SpaceEngineersDedicated/
      SpaceEngineers-Dedicated.cfg
      Saves/$SE_WORLD/
        Sandbox_config.sbc
        Sandbox.sbc
        SANDBOX_0_0_0_.sbs
        Backup/

One timestamped `.log` per start sits in that same folder. That is also the
directory `start.sh` watches to decide whether a start succeeded.

Nothing under `prefix*/`, `game/` or `backup-*/` belongs in git. The prefix
leaks the account name through the folder name alone, and server logs and world
files carry player handles, identity ids and the password hash.

`$SE_ROOT/logs/` **is** created, by `start.sh` and by `watchdog.sh`, and holds
the Wine console capture, the watchdog's own log and the crash history. Treat it
exactly like the server's logs: the console capture is the server's stdout, so
it carries player handles and everything Wine prints. `.gitignore` lists `logs/`
and `*.log`, which covers it.

### The three-files rule

**The `<SessionSettings>` block of the `.cfg` is ignored when an existing world
is loaded.** The settings that actually apply live in
`Saves/<world>/Sandbox_config.sbc`.

In practice a setting has to be written in all three files to be certain it
takes: `Saves/<world>/Sandbox_config.sbc`, `Saves/<world>/Sandbox.sbc` and
`SpaceEngineers-Dedicated.cfg`. That is what the settings panel and
`enable-scripts.sh` do.

**Never edit world files while the server is running.** The next autosave
rewrites the world from memory and discards the edit. Stop first.

**Edit them in place, inside the prefix.** Do not copy `Sandbox.sbc`,
`Sandbox_config.sbc` or `SpaceEngineers-Dedicated.cfg` into the checkout to
work on them: they carry player handles, IdentityIds and the password hash and
salt. `.gitignore` covers those names wherever they land, precisely because
this step invites a stray working copy, but a file you never put there is the
only one that can never leak.

### Crossplay (EOS)

In `SpaceEngineers-Dedicated.cfg`:

    <NetworkType>EOS</NetworkType>
    <ConsoleCompatibility>true</ConsoleCompatibility>

Confirm in the startup log:

    Networking service: EOS
    Console compatibility: Yes

Non-negotiable constraints of crossplay:

- **Steam Workshop is excluded, mod.io is supported.** That is the path
  intended for Xbox and PlayStation. Script mods only run when their code is
  server-side; client code is not executed.
- **The world must be console-compatible.** Start from a premade world under
  `Content/CustomWorlds/<name>/XBox`, marked `<ConsoleCompatible>true`. Known
  compatible premade worlds: Alien System, Distant Moons, Earth Planet,
  Home System, Mars Planet, Moon Base.
- **An existing save cannot be converted.** Start a new world.
- The `<ConsoleCompatible>` marker only ever exists in the premade world's
  `.scf` file, never in a save. It is the server's `<ConsoleCompatibility>`
  that governs crossplay, so its absence from the save is expected.

### Password

`<ServerPassword>` in plain text **does not exist and does nothing**. The server
reads a hash and a salt:

    <ServerPasswordHash>   PBKDF2-HMAC-SHA1, 10000 iterations, 20-byte key, base64
    <ServerPasswordSalt>   16 random bytes, base64

Generate both locally, without sending the password anywhere:

    python3 - <<'PY'
    import base64, hashlib, os
    password = b"changeme"          # replace before running
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha1", password, salt, 10000, 20)
    print("<ServerPasswordSalt>" + base64.b64encode(salt).decode() + "</ServerPasswordSalt>")
    print("<ServerPasswordHash>" + base64.b64encode(key).decode() + "</ServerPasswordHash>")
    PY

To recompute a hash against a salt already present in the config:

    python3 -c "import hashlib,base64; \
    salt=base64.b64decode('<SALT_FROM_THE_CFG>'); \
    print(base64.b64encode(hashlib.pbkdf2_hmac('sha1', b'changeme', salt, 10000, 20)).decode())"

Paste the two values into `SpaceEngineers-Dedicated.cfg`, in place, inside the
prefix. The world also carries a `<Password>` field in
`Saves/<world>/Sandbox.sbc`; setting it there as well costs nothing.

That config file is the one thing in the whole tree that must never reach a
public repository: the hash and the salt are enough to attack the password
offline. It is git-ignored by name, but keep it out of the checkout anyway.

Not verified server-side: the log always prints `Password = ` empty at load.
Only a real connection attempt settles which value is enforced.

### Admins

The `<Administrators>` list in the config **does not work for console players**:
their ClientIds are `0` in the save, and there is no Steam64 id to put in the
list.

Promotions actually live in `Sandbox.sbc`, in the `<AllPlayersData>` block, one
entry per player, with a `<PromoteLevel>` field. Values: `None`, `Admin`.

To promote someone:

1. Stop the server.
2. Find the player in `<AllPlayersData>` in `Saves/<world>/Sandbox.sbc` and read
   their `IdentityId`, an 18-digit number.
3. Set their `<PromoteLevel>` to `Admin`.
4. Start the server.

The player must have connected at least once to exist in that block.

**Promotions are per world.** After switching worlds, every admin has to be
promoted again on the new world, after their first connection to it.

### Mods (optional)

mod.io only, and mod ids are numeric. `mods.example.txt` is a commented
template with placeholder ids: copy it to `mods.txt` (git-ignored) and put your
own ids in the copy. It is an inventory you keep for yourself, not an input to
any script; the server reads its mods from the `<Mods>` block of
`SpaceEngineers-Dedicated.cfg`.

- **Finding a mod.io id without the API:** it is in the thumbnail URL,
  `https://thumb.modcdn.io/mods/xxxx/<ID>/crop_320x180/...`. The site search
  does not match authors, and slugs are not guessable.
- `<AutodetectDependencies>true</AutodetectDependencies>` pulls in mods that are
  not in your list. Always check the real list in the startup log.
- **Loading time.** A vanilla world is ready in about 45 seconds. A dozen mods,
  planet mods especially, push that to several minutes. `SE_START_TIMEOUT`
  (default 900 seconds) is the per-attempt budget in `start.sh`.

Two in-game script mods were confirmed available on mod.io, which makes them
reachable from a console: `Isy's Solar Alignment Script` and
`Isy's Inventory Manager`. Both require Experimental mode and
`EnableIngameScripts` set to true.

Known defect of the Inventory Manager on a dedicated server: containers desync,
or items cannot be taken out. The fix documented by its author is to disable
"Internal Sorting" in the script configuration, then reconnect.

### In-game scripts (programmable block)

Microsoft and Sony forbid running custom scripts on console. The programmable
block is therefore unavailable in single player and in console-hosted
multiplayer. **On a dedicated server it works**, and console players get it by
joining.

    ./scripts/enable-scripts.sh           # stops, edits the three files, restarts
    ./scripts/enable-scripts.sh --off     # disable again
    ./scripts/enable-scripts.sh --force   # stop even on a stale save
    ./scripts/enable-scripts.sh --help    # the notes at the top of the script

`EnableIngameScripts` forces the world into Experimental mode. The script keeps
`stop.sh`'s save-age guard, so it refuses rather than lose work; `--force`
passes through to it.

## Running the server

    ./scripts/start.sh          # start
    ./scripts/stop.sh           # stop, with the save-age guard
    ./scripts/stop.sh --force   # stop anyway
    tmux attach -t se           # live console, session name is SE_TMUX_SESSION
                                # (detach: Ctrl+B then D)

`start.sh` runs Wine directly inside the tmux pane, with nothing in between, so
the game is the pane process and the foreground process group of the pty: the
console stays usable and signals go where you think they go. `caffeinate` is
started alongside instead, watching the server PID, so the machine stays awake
while the server runs. Each attempt watches the newest `.log` for `Game ready`, and gives
up on `CRASH INFO`, `FATAL UNHANDLED` or `Session can not start`.

`stop.sh` sends `SIGINT` to the game, which is Space Engineers' own shutdown:
it saves the world, unloads the session and closes Steam. The script waits up to
`SE_STOP_TIMEOUT` for the log to show `Exiting..` followed by a completed save,
then cuts the process, which never exits by itself (see below).

If the game never answers, it falls back to the old guard: it refuses to cut
when the save is older than `SE_SAVE_MAX_AGE`, and when the save file is missing
entirely (a wrong `SE_WORLD` would otherwise disarm the guard silently).
`--force` overrides both.

### Watchdog

The server dies on its own, at random, inside the physics engine. See
[The Havok crash](#the-havok-crash). Nothing can prevent it, so the watchdog
restarts it instead.

    ./scripts/watchdog.sh install     # a launchd agent, checks every 60 s
    ./scripts/watchdog.sh status      # what it thinks, and the crash history
    ./scripts/watchdog.sh check       # run one check by hand
    ./scripts/watchdog.sh uninstall

**It does not judge by whether the process exists.** A crashed server keeps its
process: the thread that dies holds a critical section, every other thread then
blocks on it forever, and the process sits there burning a core. `se_pid()`
still finds it and the settings panel still calls it online. What it no longer
does is simulate or save.

So the test is whether the game is **still writing**. It logs `GC Memory:` every
30 s regardless of load, so a log untouched for `SE_WATCHDOG_SILENCE` seconds is
a dead server whatever `ps` says. On that signal the watchdog kills the process,
records the crash and runs `start.sh`.

Two flags in `$SE_ROOT/run/` keep it from fighting the other scripts.
`stop.sh` writes `stopped-on-purpose` before it does anything, because a polite
shutdown also stops the log and would otherwise look exactly like a crash;
`start.sh` clears it and holds up the watchdog with `starting` while mods load.

`logs/crashes.log` gets one line per crash: when, how long the server had been
up, and the Havok signature if the console capture caught it. That history is
the only way to tell whether anything you change actually reduces the crash
rate, so read it before believing any explanation.

After `SE_WATCHDOG_MAX_RESTARTS` restarts within `SE_WATCHDOG_WINDOW` it stops
and says so. A world that crashes as it loads is not bad luck, and reloading it
every minute only costs mod downloads.

### Settings panel

    python3 panel/settings.py     ->  http://127.0.0.1:8777 (SE_PANEL_PORT)

A local page that reads **every world setting the save file holds**, 190 of
them, presents them grouped with a label and a help line each, then stops the
server, rewrites them into the three files and starts it again. Sixteen groups:
yield, survival, interface, world, mechanics, blocks and limits, weapons,
NPCs and encounters, economy and factions, environment, voxels and terrain,
cleanup, grid storage, multiplayer, performance, match. A filter box narrows
them by label, XML key or help text, because 190 rows are not browsable.

Twenty-five dangerous ones carry a warning stripe: permanent death, disabling
saving, resetting ownership, changing the procedural seed on a live world, and
the rest.

Two settings are deliberately left out, `LimitBlocksBy` and `OnlineMode`: their
valid values are not documented anywhere reachable, and a value the game
refuses stops the world from loading at all.

Four more sections sit below the settings:

- **Server** edits the server name and the world's display name, and sets or
  clears the password. The password is hashed locally, into the two separate
  fields the server expects, and never stored or sent in clear.
- **Players** lists everyone the world knows, with their promotion level and
  ban state, and toggles either one. Promotions live in the save and are
  therefore per world; bans live in the server config and apply to every world.
- **Backups** lists `Backup/`, reveals a backup in Finder, and restores one.
  Restoring moves the current world into `Backup/` under a timestamped name
  first, so a restore is undone by another restore.
- **Mods** lists the active mods in load order. The world stores only numeric
  ids, `Mods/` is empty on a dedicated install and the cache is named by number,
  so the names are read from the server's own log. No network call, no mod.io
  key: the public API refuses every request without one, and a URL built from a
  numeric id redirects to the game's catalogue.

It reads the same `config.sh` as the scripts, sourced the way
`scripts/common.sh` sources it, so `SE_WORLD`, `SE_PREFIX` and the `SE_PANEL_*`
values apply here too. The environment still wins over the file.

Applying does three things, in this order:

1. runs `stop.sh`. Without the force option it keeps the save-age guard, so a
   refusal ends the request and **nothing is written**;
2. writes the settings into the three files, then deletes
   `SANDBOX_0_0_0_.sbsB5`, the stale compressed copy Space Engineers would
   otherwise read in preference (the same reason `enable-scripts.sh` deletes
   it);
3. runs `start.sh`.

Writing before stopping would be pointless anyway: the next autosave rewrites
the world from memory.

**Forcing the stop is a checkbox, off by default.** Ticking it adds `--force`
to `stop.sh`, which bypasses the save-age guard: everything built since the
last autosave is lost. The header shows the age of the last save and the number
of players online; read them before applying.

The page loads no remote resource: system fonts only, no webfont, no CDN. The
server listens on `127.0.0.1` unless `SE_PANEL_HOST` moves it, and the panel
exits with an explanation unless `SE_PANEL_ALLOW_REMOTE=1` confirms you meant
it.

There is no authentication. `POST /api/appliquer` rewrites the world files then
runs `stop.sh` and `start.sh`, so **whoever reaches the port can restart the
server.** What the panel verifies is only that the request came from this
machine:

- `Host` has to be a loopback name, or the address the panel was explicitly
  told to bind, with the port it listens on. This closes DNS rebinding, where a
  hostname that resolves to `127.0.0.1` lets a remote page talk to the panel.
  When `SE_PANEL_ALLOW_REMOTE=1` deliberately opens the panel to a network,
  the name is no longer checked (there is no way to know which name or address
  the machine will be reached by) and only the port is; the `Origin` rule below
  keeps working either way.
- `Origin`, when present, has to match the `Host` of the same request exactly.
  A page served from anywhere else is refused, and so is an opaque `null`
  origin.
- `POST` requires `Content-Type: application/json`, which a cross-site HTML
  form cannot send without a CORS preflight it will not pass.

None of that is authentication: a local script or a `curl` on the port still
has full control. That is what the loopback default is for, and why reaching
the panel from another machine should be a tunnel rather than an open port:

    ssh -N -L 8777:127.0.0.1:8777 user@the-mac

The panel is in French; the settings it writes are the game's own English keys.

To add a setting, edit the settings list at the top of the file: a tuple of
`(key, label, type, help, [choices])`, where the type is numeric, boolean, or a
fixed list of choices.

## Gameplay settings

Values below are the ones that were verified to apply. They are a starting
point, not a recommendation.

Modern survival (Apex Survival 1.207):

    EnableSurvivalBuffs   true      food bar and buffs
    FoodConsumptionRate   0.5       float, 0 disables hunger
    EnableIngameScripts   true      programmable block, forces Experimental mode

PVE:

    EnableEncounters            true    wrecks and encounters
    EnablePlanetaryEncounters   true    ground bases (SE does not log this one)
    CargoShipsEnabled           true    NPC cargo ships
    EnableSpiders               true
    EnableWolfs                 true
    EnableEconomy               true    trade stations, contracts
    EnvironmentHostility        NORMAL

`EnvironmentHostility` accepts `CATACLYSM` and `ARMAGEDDON`, but Keen never
ships those two values in its own worlds: only `SAFE` and `NORMAL`.

Performance:

    TrashRemovalEnabled             true    clears debris, net gain
    EnableSelectivePhysicsUpdates   false   see Known limitations
    AutoSaveInMinutes               2       every crash costs everything since
    SyncDistance                    2000    capped, see Known limitations

## Known limitations

**The shutdown is clean, but it is the game's own and it takes its time.**
`SIGINT` sent to the game triggers `Exiting..`, `Autosave in unload`, then the
save. Nothing is lost. But the game only honours the request when it feels like
it: 0.1 s on a server that has been up a few minutes, and up to 3 min on one
that has just reached `Game ready` or is still loading a world. `stop.sh` waits
`SE_STOP_TIMEOUT` for it, which is why stopping is not instant.

**The process does not exit after that shutdown.** It sits on "press any key to
close this window", spinning a core, and no key sent from a script reaches it:
`tmux send-keys` writes the byte and the console echoes `^C`, but nothing
happens. `stop.sh` therefore kills the process, AFTER the log confirms the save.

This was believed impossible for a long time, and the reason is a trap worth
knowing: `pgrep -f "SpaceEngineersDedicated.exe"` matches the `tmux` process
too, because the wine command line sits in its arguments, and `tmux` always has
the lower PID. Every `| head -1` therefore returned TMUX. The signals went to
the terminal, which closed the pty under the game, which died unsaved. The fix
is `se_pid()` in `scripts/common.sh`: filter on the process NAME, not on its
command line.

Still untested for lack of need: the remote API on port 8080, which does not
listen.

**A `kill -9` during an autosave can corrupt the world.** Observed once, as
`Exception while loading world`. The next start recovered, and
`Saves/<world>/Backup/` keeps several restore points.

### The Havok crash

**The server dies at random, mid-game, inside the physics engine.** Measured
here at roughly one crash per two hours with players connected, on uptimes of
68, 122 and 147 minutes. Nothing in the game's own log announces it: the log
simply stops mid-frame, in the middle of ordinary activity.

The crash only appears in the Wine console, which is why `start.sh` captures it:

    =================================================================
        Native Crash Reporting
    =================================================================
    Got a UNKNOWN while executing native code.
        at Havok.HkJobQueue:HkJobQueue_ProcessAllJobs
        at Sandbox.Engine.Physics.MyPhysics:StepWorldsParallel
        at Sandbox.Engine.Physics.MyPhysics:StepWorlds
    wine: Unhandled page fault on read access to <address> (thread 0024)

A read through a dead pointer in Havok's parallel job queue. The thread that
dies holds a critical section, so the rest of the process piles up behind it:

    err:sync:RtlpWaitForCriticalSection ... blocked by 0024, retrying (60 sec)

The signature is not specific to this setup. Unmodded dedicated servers on
Windows report the identical stack, and Keen has published no fix, so the crash
itself is not repairable from here. How **often** it lands is a separate
question, and one this setup may well make worse: the game expects the .NET
Framework and gets Wine Mono, and the crash sits exactly on the managed-to-native
boundary. Measure with `logs/crashes.log` rather than trust that reasoning,
including where it appears above.

There is a sequential physics path in the binaries, `StepWorldsSequential`, but
it is selected by `MyFakes.ENABLE_HAVOK_MULTITHREADING`, a compile-time flag.
The dedicated server accepts no command-line switch and exposes no setting for
it, so reaching that path means patching game code, which this repository does
not do.

What is left is surviving it: see [Watchdog](#watchdog), and lower
`AutoSaveInMinutes`, since every crash costs everything since the last autosave.

**Turn Wine's crash dialog off.** By default Wine puts up a modal "Program
Error" window, and until someone clicks it the dead process stays in the list,
holding its tmux session and its prefix. It also swallows the native backtrace.
One registry value fixes both, and the backtrace then lands in the console
capture:

    WINEPREFIX=... wine reg add 'HKEY_CURRENT_USER\Software\Wine\WineDbg' \
        /v ShowCrashDialog /t REG_DWORD /d 0 /f

**Startup crashes roughly once in four**, with a `NullReferenceException` in
`MyGameService.UpdateNetworkThread`. `start.sh` retries. Suspected cause: the
host name does not resolve, which is also why Wine logs
`getaddrinfo Failed to resolve your host name`. Untested fix:

    sudo sh -c 'echo "127.0.0.1 $(scutil --get LocalHostName)" >> /etc/hosts'

**`EnableContainerDrops` does not stick.** Set to `true` in all three files, the
server reloads it as `False` at every start. Cause not identified; a possible
lead is the empty `Installed DLCs:` line. Every other setting applies. This is
the only one that resists.

**`SyncDistance` is capped at 2000.** All three files can hold 4000; the server
logs 2000 at load and does not rewrite the files. In all likelihood a cap
imposed by console compatibility.

**Do not enable `EnableSelectivePhysicsUpdates`.** Modular Encounters Systems
detects it and warns:

    WARNING: Selective Physics Updates is Enabled with SyncDistance Less Than
    10000. Modular Encounters Systems may not work correctly.

Since `SyncDistance` cannot exceed 2000, the combination is impossible. If your
PVE mods depend on MES, leave selective physics off.

**The simulation is single-threaded.** No setting spreads it over several cores.
The process carries about 51 threads (network, loading, Havok) but the main tick
is serial: the real limit is the speed of one core. `CPULoadLimit` at 100 means
there is no internal throttle.

**The process runs at nice 5**, lowered by macOS. Raising it needs root:

    sudo renice -n 0 -p $(pgrep -f SpaceEngineersDedicated.exe | head -1)

**Networking is only partly verified.** The server opens no local listening
port: `lsof` shows nothing on 27016 or 8766. Everything is outbound to EOS, so
port forwarding is probably unnecessary. If a player cannot find the server, the
first thing to try is still forwarding UDP 27016 to the LAN address of the host
machine.

**`Failed to parse texture header` lines in the log are harmless.** A dedicated
server renders nothing.

**Water is not covered here.** Water does not create itself: the water mod
cannot find its `WaterConfig.xml` because the `FileExistsInModLocation` API is
broken on a dedicated server, and the radius has to be set in game with
`/wcreate 1.0038`. That value is relative to `MinimumRadius`; the default of 1
would put the water surface at the lowest point of the terrain.

## What we learned

Findings that cost time to obtain and are not documented elsewhere.

**The .NET Framework 4.8 installer is 32-bit and deadlocks under Wine 11.**
Wine Mono is the answer. See [The .NET Framework trap](#the-net-framework-trap).

**Space Engineers caps the number of planet TYPES per world.** Adding a fourth
type to a world that already has three gives:

    MyLoadingException: World contains too many planet types and could not be loaded.

This is not a console restriction: the engine refuses to load the world at all.
It is the same wall as the in-game message about exceeding the scenario limits.
So a planet cannot be **added**, only **replaced**.

**A planet is two things, and both are mandatory.**

1. An entity in `SANDBOX_0_0_0_.sbs`, which is readable XML: `<Name>`,
   `<StorageName>`, `<PlanetGenerator>`, `<Seed>`, `<Radius>` and a position.
   `StorageName` has the form `<Generator>-<seed>d<diameter>`.
2. A voxel file named `<StorageName>.vx2`. Without it the world refuses to load:

       MyIncompatibleDataException: No storage loaded for planet ...

   Space Engineers does **not** regenerate that file from the seed.

**The `.vx2` format is gzip.** Decompressed it is small (about 613 bytes for an
untouched planet) and contains an octree, the material list, and then the
generator name **as a length-prefixed string**: `\x04Mars`, `\x09EarthLike`.
Swapping one generator for another is a matter of replacing that string and
renaming the file. Names of different lengths work as well, as long as the
length byte is updated to match: a 4-character generator was swapped for a
6-character one and loaded correctly.

**Delete `SANDBOX_0_0_0_.sbsB5` after editing the world.** Otherwise Space
Engineers reads the stale compressed copy and ignores the edits entirely.

**Space Engineers recomputes `MinimumSurfaceRadius` and `MaximumHillRadius`**
from the `HillParams` of the mod's planet definition, and only writes them to
the file when it saves. There is no point setting them by hand: right after a
start the file still shows the old values. That is normal; wait for the
autosave.

**Space Engineers does NOT recompute `SurfaceGravity`.** It is the one field to
fix by hand when replacing a planet. A replacement once kept the moon's `0.25`
instead of the intended `0.92` for hours: a quarter of the expected gravity, and
the planet was unplayable.

**Check `HillParams` before installing a planet mod.** The Min value has to be
negative, so there is margin below the surface. Vanilla references: Moon `-0.03`,
Mars and Earth `-0.01`. Out of six mod planets examined, one had `Min = 0`, no
margin at all, and was dropped for that reason.

**The client caches planets.** After replacing one, quit the game entirely, not
just the server, or you see the old terrain with the new gravity.

**Mod ids are in the thumbnail URL.** No API needed, and the site search will
not help: it does not match authors, and slugs are not guessable from the
display name.

**A planet mod does not add its planet to an already generated world.** Planets
that come from mods have to be placed through the admin menu in game.

**Autodetected dependencies load mods you never listed.** Read the real mod list
from the startup log rather than from the config.

## Repository contents

    config.example.sh           every tunable, with defaults and comments
    mods.example.txt            mod.io list template, placeholder ids
    scripts/common.sh           root, config and path resolution, sourced by the rest
    scripts/start.sh            start with retries, tmux session, caffeinate
    scripts/stop.sh             stop with the save-age guard
    scripts/watchdog.sh         crash detection and restart, launchd agent
    scripts/enable-scripts.sh   programmable block on/off, across the three files
    panel/settings.py           local settings page on 127.0.0.1
    LICENSE

Deliberately absent, and git-ignored: `game/` (server binaries, not ours to
redistribute), `prefix*/` (multi-GB, and the account name leaks through a
folder name; the glob covers a throwaway test prefix too), `backup-*/` and
`*.log` (server name, player handles, identity ids, password hash and salt,
absolute home paths), `config.sh` (your paths and world name), `mods.txt` (your
list). `Sandbox.sbc`, `Sandbox_config.sbc` and `SpaceEngineers-Dedicated.cfg`
are ignored by name wherever they appear, because editing them by hand is a
documented step and a working copy left in the checkout would carry the
password hash and salt into a commit.

None of that applies until this tree is a git repository: see
[If you did not clone](#if-you-did-not-clone-git-init-first).

Not affiliated with Keen Software House. Space Engineers and its server binaries
belong to their respective owners; this repository only automates running them.
