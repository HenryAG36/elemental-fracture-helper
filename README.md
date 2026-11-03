# Elemental Fracture Helper

Minimal terminal helper for running the **Elemental Fracture**
(Spellbreak) launcher under **CrossOver** on macOS — sign in, download,
update, verify, and play without clicking through a webview UI. The
setup also applies the small compatibility repair the launcher needs to
run under CrossOver at all, and the Wine DLL override that makes the
game's bundled `xinput1_3.dll` mod loader actually load.

## What it does

- **`setup_helper.py`** (also `Setup.command`):
  - `check` — read-only report: exe hash state, resolved paths, journal.
  - `apply` — SHA-256-gated in-place compatibility patch for launcher
    3.6.0 **only** (see `PATCHES.md` for exact bytes/offsets), plus the
    per-executable `xinput1_3=native,builtin` Wine override for
    `g3-Win64-Test.exe`. Makes unique timestamped backups and a journal
    of every change under `.state/`.
  - `undo` — reverts only what a recorded apply did: restores patch
    bytes (verified by hash first) and restores/removes the registry
    override (only if the current value is still ours). The journal is
    kept until undo fully succeeds.
- **`elemental_fracture.py`** (also `Elemental Fracture.command`):
  terminal menu driving the launcher's Tauri backend over its local CDP
  debug port — Status / Sign in / Download / Update / Verify / Play.

## Requirements

- macOS with **CrossOver** installed at `/Applications/CrossOver.app`
  (a different path works via `--crossover`).
- A CrossOver bottle named `Spellbreak` (`--bottle` to change), with the
  official **Elemental Fracture launcher 3.6.0** installed inside it —
  grab the launcher installer from the official site (https://elefrac.com) and run it in the
  bottle yourself. Tested on exactly one Apple Silicon Mac + one
  CrossOver/wine build; other machines are unverified.
- `python3` + `websocket-client==1.9.0` — `Setup.command` offers to
  build a local `.venv` from a pinned `requirements.txt` (asks before
  any network use; nothing installs system-wide).

## Install

1. Download/extract this repo's ZIP.
2. Install CrossOver and the launcher (official installer → run in a
   `Spellbreak` bottle).
3. Double-click **`Setup.command`** → choose `apply`. Quit the launcher
   first if it's running.
4. Double-click **`Elemental Fracture.command`** → sign in, download,
   play from the menu. `python3 elemental_fracture.py --status` prints a
   read-only status without the menu.

Both `.command` scripts accept `--bottle`,
`--crossover`, `--launcher-exe` (forwarded through the `.command` scripts' `"$@"`).

## How it works

The launcher is a Tauri app wrapping an embedded WebView2 browser.
Patch B (see `PATCHES.md`) swaps its embedded `--autoplay-policy` flag
for `--remote-debugging-port=9339`, exposing Chrome DevTools on
loopback. The helper finds the `tauri.localhost` page, opens the
loopback ws debugger, and calls the same
`window.__TAURI_INTERNALS__.invoke(command, args)` surface the official
UI uses — same commands, same order (`ensure_runtime` → `launch_game`,
`check_game_folder` verdict gate, per-component version checks). Long
operations are promise jobs polled for progress. Patch A skips an OLE
drag-drop call that crashes the launcher under CrossOver's wine.

## Caveats

- **Legal/scope:** the patch modifies bytes inside the proprietary
  Windows launcher on disk — your own copy, at your own risk, for
  interoperability. No binaries are distributed here, no affiliation
  with the Elemental Fracture/Spellbreak developers, Microsoft, or CodeWeavers.
  MIT license covers this repo's code only.
- **Debug port risk:** while the launcher runs, loopback port 9339 grants
  local account control of the launcher. Close the launcher when done.
- **Launcher updates:** an update will change the exe hash and overwrite
  the patch — the helper then refuses to run setup and reports the
  repair is needed. Never auto-update the launcher from inside it.
- **Credentials:** the helper asks for email + hidden password once per
  sign-in, sends them to the launcher's own `login_email` command, and
  never stores or echoes them (the launcher itself manages the session;
  tokens are masked in all output). The local setup journal records file
  paths, hashes, and the prior registry value — no credentials or
  account data.
- **No monitoring:** nothing installs launch agents, watchers, or login
  items; both scripts do their one thing and exit.
- **One launcher at a time:** the debug port is fixed at 9339, so only a
  single launcher instance can expose it. `--bottle`/`--crossover`/
  `--launcher-exe` choose which bottle the *setup* and *launcher-start*
  paths target — they don't select among multiple already-running
  launcher instances; whichever is running owns the port.
- Tested end-to-end on one machine, including online matchmaking after
  the override. Your mileage may vary on other setups.

## Development

```sh
python3 -m unittest test_helper test_setup -v
python3 -m py_compile *.py
bash -n Setup.command "Elemental Fracture.command"
```

Tests are fully mocked — no Wine, no launcher, no network. The GitHub
Actions workflow runs the same compile + unittest steps on ubuntu.
