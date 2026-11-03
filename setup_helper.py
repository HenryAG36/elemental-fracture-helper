#!/usr/bin/env python3
"""Setup/undo for the Elemental Fracture CrossOver helper.

Subcommands:
  check  read-only: report exe hash state, config, journal summary
  apply  verify the launcher exe hash, apply the compatibility patch,
         set the per-exe xinput1_3 override, journal every step
  undo   revert ONLY what a recorded apply did (verified by hashes and
         the journal) — never clobbers state it didn't create

Only launcher version 3.6.0 is supported. Whole-file SHA-256 gates every
edit: an unrecognized binary is never touched.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import helper_config as hc

SUPPORTED_VERSION = "3.6.0"

# ---- patch manifest ------------------------------------------------
# Launcher 3.6.0, length 13242880 bytes.
# Patch A @0x157f0b: `call RevokeDragDrop` rel32 -> `xor eax,eax; nop*3`
# (skips the call site that crashes the launcher under CrossOver).
# Patch B @0xb031d8 (105-byte command-line field): swap the
# --autoplay-policy flag for --remote-debugging-port=9339 so the Tauri
# webview exposes CDP, space-padded to the original length.
KNOWN_LAUNCHERS = {
    SUPPORTED_VERSION: {
        "size": 13242880,
        "original_sha256":
            "cb1590bb594adb75a0ec2fb0e6bad4e31d272cfd28ed05cc36dea820fe748eb6",
        "patched_sha256":
            "c0774720ae6707563d59f38f533bbec891a837652f6389f20ee38795727f5021",
        "patches": [
            {"offset": 0x157F0B,
             "old": bytes.fromhex("e8506d5500"),
             "new": bytes.fromhex("31c0909090")},
            {"offset": 0xB031D8,
             "length": 105,
             "old": (b"--disable-features=msWebOOUI,msPdfOOUI,"
                     b"msSmartScreenProtection"
                     b" --autoplay-policy=no-user-gesture-required"),
             "new": (b"--disable-features=msWebOOUI,msPdfOOUI,"
                     b"msSmartScreenProtection"
                     b" --remote-debugging-port=9339")},
        ],
    }
}

OVERRIDE_KEY = ("HKCU\\Software\\Wine\\AppDefaults\\"
                "g3-Win64-Test.exe\\DllOverrides")
OVERRIDE_NAME = "xinput1_3"
OVERRIDE_VALUE = "native,builtin"

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         ".state")
JOURNAL = os.path.join(STATE_DIR, "setup-journal.json")


class SetupError(Exception):
    pass


# ---- primitives ----------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exe_state(path, known):
    """original | patched | unknown | missing"""
    if not os.path.exists(path):
        return "missing"
    if os.path.getsize(path) != known["size"]:
        return "unknown"
    digest = sha256_file(path)
    if digest == known["original_sha256"]:
        return "original"
    if digest == known["patched_sha256"]:
        return "patched"
    return "unknown"


def _field(patch, key):
    raw = patch[key]
    length = patch.get("length", len(raw))
    if len(raw) > length:
        raise SetupError("patch field longer than slot")
    return raw + b" " * (length - len(raw))


def _write_fields(path, known, old_key, new_key):
    """Verify every slot holds old_key bytes, then write new_key."""
    with open(path, "r+b") as f:
        for p in known["patches"]:
            f.seek(p["offset"])
            if f.read(len(_field(p, old_key))) != _field(p, old_key):
                raise SetupError(
                    f"unexpected bytes at {p['offset']:#x} — refusing "
                    "to patch")
        for p in known["patches"]:
            f.seek(p["offset"])
            f.write(_field(p, new_key))


def _unique_backup(path, ts=None):
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.bak-{ts}"
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.bak-{ts}-{n}"
    with open(path, "rb") as src, open(dest, "wb") as dst:
        for chunk in iter(lambda: src.read(1 << 20), b""):
            dst.write(chunk)
    return dest


def _runner(cfg):
    def run(*argv):
        return subprocess.run(
            [cfg.wine_bin, "--bottle", cfg.bottle, *argv],
            capture_output=True, text=True)
    return run


def reg_query(run, key=OVERRIDE_KEY, name=OVERRIDE_NAME):
    """Return the current value, or None if absent. Errors raise."""
    r = run("reg", "query", key, "/v", name)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith(name):
                parts = line.split("REG_SZ")
                if len(parts) == 2:
                    return parts[1].strip()
        raise SetupError(f"reg query succeeded but value not parsed: "
                         f"{out.strip()}")
    if "unable to find" in out.lower() or "not find" in out.lower() \
            or "not exist" in out.lower():
        return None
    raise SetupError(f"reg query failed (rc={r.returncode}): "
                     f"{out.strip()}")


def reg_add(run, key=OVERRIDE_KEY, name=OVERRIDE_NAME,
            value=OVERRIDE_VALUE):
    r = run("reg", "add", key, "/v", name, "/d", value, "/f")
    if r.returncode != 0:
        raise SetupError(
            f"reg add failed (rc={r.returncode}): "
            f"{(r.stdout or '')}{(r.stderr or '')}".strip())


def reg_delete(run, key=OVERRIDE_KEY, name=OVERRIDE_NAME):
    r = run("reg", "delete", key, "/v", name, "/f")
    out = ((r.stdout or "") + (r.stderr or "")).lower()
    if r.returncode != 0 and "unable to find" not in out:
        raise SetupError(
            f"reg delete failed (rc={r.returncode}): {out.strip()}")


# ---- journal --------------------------------------------------------

def _journal_path():
    return JOURNAL


def _load_journal():
    if not os.path.exists(_journal_path()):
        return None
    with open(_journal_path()) as f:
        return json.load(f)


def _save_journal(j):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _journal_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(j, f, indent=2)
    os.replace(tmp, _journal_path())


def _journal_for_target(cfg):
    """Return the existing journal if it describes THIS target.

    A journal for a different bottle/exe means unfinished business on
    another target — refuse rather than overwrite its provenance.
    """
    j = _load_journal()
    if not j:
        return None
    if j.get("bottle_dir") != cfg.bottle_dir \
            or j.get("exe") != cfg.launcher_exe:
        raise SetupError(
            f"an unfinished setup journal exists for "
            f"{j.get('exe')!r} in {j.get('bottle_dir')!r} — run undo "
            "for that target (or delete "
            f"{_journal_path()} after manual cleanup) before applying "
            "here")
    return j


def reg_set_verified(run, key=OVERRIDE_KEY, name=OVERRIDE_NAME,
                     value=OVERRIDE_VALUE):
    """reg add, then read it back — fail honestly if it didn't take."""
    reg_add(run, key, name, value)
    got = reg_query(run, key, name)
    if got != value:
        raise SetupError(
            f"reg add readback mismatch for {name}: got {got!r}, "
            f"wanted {value!r}")


# ---- commands -------------------------------------------------------

def cmd_check(cfg, known):
    print(f"bottle:      {cfg.bottle}")
    print(f"bottle_dir:  {cfg.bottle_dir}")
    print(f"crossover:   {cfg.crossover}")
    print(f"wine:        {cfg.wine_bin}")
    print(f"launcher:    {cfg.launcher_exe}")
    print(f"supported:   Elemental Fracture launcher {SUPPORTED_VERSION}")
    state = exe_state(cfg.launcher_exe, known)
    print(f"exe state:   {state}", end="")
    if os.path.exists(cfg.launcher_exe):
        print(f" (sha256 {sha256_file(cfg.launcher_exe)[:16]}...)")
    else:
        print()
    j = _load_journal()
    if j:
        st = j.get("stages", {})
        print(f"journal:     patch={st.get('patch', {}).get('state')} "
              f"override={st.get('override', {}).get('state')}")
    else:
        print("journal:     none")
    return 0


def cmd_apply(cfg, known, run=None, confirm=None, out=print):
    run = run or _runner(cfg)
    confirm = confirm or _confirm
    state = exe_state(cfg.launcher_exe, known)
    if state == "missing":
        raise SetupError(f"launcher exe not found: {cfg.launcher_exe}")
    if state == "unknown":
        raise SetupError(
            "launcher exe hash is not a recognized Elemental Fracture "
            f"{SUPPORTED_VERSION} build (sha256 "
            f"{sha256_file(cfg.launcher_exe)}) — refusing to patch an "
            "unrecognized version")

    # a journal for another target blocks BEFORE anything mutates
    journal = _journal_for_target(cfg) or {
        "version": 1, "bottle": cfg.bottle,
        "bottle_dir": cfg.bottle_dir, "exe": cfg.launcher_exe,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "stages": {}}
    stages = journal["stages"]

    out(f"exe state: {state}")
    out("Before continuing: quit the launcher and any game running in "
        f"the '{cfg.bottle}' bottle (the setup will not kill them).")
    if not confirm("Apply compatibility patch + xinput1_3 override?"):
        out("cancelled")
        return 1

    ts = time.strftime("%Y%m%d-%H%M%S")
    user_reg = os.path.join(cfg.bottle_dir, "user.reg")
    if "reg_backup" not in stages and os.path.exists(user_reg):
        b = _unique_backup(user_reg, ts)
        stages["reg_backup"] = {"path": b}
        _save_journal(journal)
        out(f"user.reg snapshot: {b}")

    # --- patch stage ---
    patch_state = stages.get("patch", {}).get("state")
    if patch_state == "applied" and state != "patched":
        raise SetupError(
            f"journal records an applied patch but the exe is {state!r} "
            "— inconsistent; refusing to guess. Run undo or inspect "
            "the exe manually.")
    if patch_state == "applied":
        out("patch already recorded as applied — provenance kept")
    elif state == "patched":
        if patch_state == "pending":
            # a previous apply wrote the bytes but stopped before
            # recording completion — keep its exe_backup provenance
            stages["patch"]["state"] = "applied"
            _save_journal(journal)
            out("pending patch found applied — marked applied, "
                "backup provenance kept")
        else:
            stages["patch"] = {"state": "preexisting-patched"}
            _save_journal(journal)
            out("exe already recognized-patched — no bytes written")
    else:
        # state == original: fresh apply, or resume a pending write
        # (reuses the already-recorded backup if one exists)
        if "exe_backup" not in stages:
            b = _unique_backup(cfg.launcher_exe, ts)
            stages["exe_backup"] = {"path": b,
                                    "sha256": sha256_file(b)}
            out(f"exe backup: {b}")
        stages["patch"] = {"state": "pending"}
        _save_journal(journal)          # restore info on disk FIRST
        _write_fields(cfg.launcher_exe, known, "old", "new")
        if exe_state(cfg.launcher_exe, known) != "patched":
            _save_journal(journal)
            raise SetupError("patch written but hash check failed — "
                             "journal retained; run undo to restore")
        stages["patch"]["state"] = "applied"
        _save_journal(journal)
        out("compatibility patch applied and verified")

    # --- override stage ---
    try:
        current = reg_query(run)
    except SetupError:
        _save_journal(journal)
        raise
    ovr_stage = stages.get("override", {})
    if current == OVERRIDE_VALUE:
        if ovr_stage.get("state") == "set":
            out("xinput1_3 override already set — provenance kept")
        elif ovr_stage.get("state") == "pending":
            # a previous apply recorded intent then stopped; the value is
            # present now, so keep its recorded 'previous' provenance
            ovr_stage["state"] = "set"
            _save_journal(journal)
            out("xinput1_3 override present — pending apply completed")
        else:
            stages["override"] = {
                "state": "preexisting-same", "previous": current,
                "key": OVERRIDE_KEY, "name": OVERRIDE_NAME,
                "value": OVERRIDE_VALUE}
            _save_journal(journal)
            out("xinput1_3 override already present — preserved")
    else:
        # write needed; keep a pending stage's recorded 'previous'
        previous = (ovr_stage.get("previous")
                    if ovr_stage.get("state") == "pending" else current)
        stages["override"] = {
            "state": "pending", "previous": previous,
            "key": OVERRIDE_KEY, "name": OVERRIDE_NAME,
            "value": OVERRIDE_VALUE}
        _save_journal(journal)
        try:
            reg_set_verified(run)
        except SetupError:
            _save_journal(journal)
            raise
        stages["override"]["state"] = "set"
        _save_journal(journal)
        out(f"xinput1_3 override set to '{OVERRIDE_VALUE}' "
            f"(previous: {previous!r})")

    out(f"journal: {_journal_path()}")
    out("done — start the launcher, then use the helper menu")
    return 0


def cmd_undo(cfg, known, run=None, out=print):
    run = run or _runner(cfg)
    j = _journal_for_target(cfg)
    if not j:
        raise SetupError("no setup journal found — nothing to undo")
    stages = j.get("stages", {})
    problems = []

    patch = stages.get("patch") or {}
    if patch.get("state") in ("applied", "pending"):
        state = exe_state(cfg.launcher_exe, known)
        try:
            if state == "original":
                patch["state"] = "reverted"
                out("exe already at original bytes — patch reverted")
            elif state == "patched":
                _write_fields(cfg.launcher_exe, known, "new", "old")
                if exe_state(cfg.launcher_exe, known) == "original":
                    patch["state"] = "reverted"
                    out("compatibility patch reverted")
                else:
                    problems.append("patch revert written but hash "
                                    "check failed — journal retained")
            else:
                problems.append(
                    f"exe is {state!r}, not a recognized build — "
                    "not touching it")
        finally:
            _save_journal(j)   # stage result persisted either way
    elif patch.get("state"):
        out(f"patch stage '{patch['state']}' — not ours, left alone")

    ovr = stages.get("override") or {}
    if ovr.get("state") in ("set", "pending"):
        try:
            current = reg_query(run, ovr["key"], ovr["name"])
            if current == ovr["value"]:
                if ovr.get("previous") is None:
                    reg_delete(run, ovr["key"], ovr["name"])
                    out("xinput1_3 override removed "
                        "(was absent before)")
                else:
                    reg_set_verified(run, ovr["key"], ovr["name"],
                                     ovr["previous"])
                    out(f"xinput1_3 override restored to "
                        f"{ovr['previous']!r}")
                ovr["state"] = "reverted"
            elif current is None:
                ovr["state"] = "already-absent"
                out("xinput1_3 override already absent")
            else:
                problems.append(
                    f"xinput1_3 override is now {current!r}, "
                    "not ours — left alone")
        except SetupError as e:
            problems.append(str(e))
        finally:
            _save_journal(j)
    elif ovr.get("state"):
        out(f"override stage '{ovr['state']}' — not ours, left alone")

    if problems:
        for p in problems:
            out(f"not undone: {p}")
        raise SetupError(
            "partial undo — journal retained at "
            f"{_journal_path()}; resolve and re-run undo")
    try:
        os.remove(_journal_path())
        out("journal removed — undo complete")
    except OSError:
        pass
    return 0


def _confirm(prompt):
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def main(argv):
    top = argparse.ArgumentParser(
        prog="setup_helper.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = top.add_subparsers(dest="cmd", required=True)
    parsers = {}
    for name in ("check", "apply", "undo"):
        p = sub.add_parser(name)
        hc.add_config_args(p)
        parsers[name] = p
    args = top.parse_args(argv)
    try:
        cfg = hc.config_from_args(args)
    except hc.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    known = KNOWN_LAUNCHERS[SUPPORTED_VERSION]
    try:
        if args.cmd == "check":
            return cmd_check(cfg, known)
        if args.cmd == "apply":
            return cmd_apply(cfg, known)
        if args.cmd == "undo":
            print("Quit the launcher and any game running in the "
                  f"'{cfg.bottle}' bottle before undoing.")
            if not _confirm("Undo the recorded setup changes?"):
                print("cancelled")
                return 1
            return cmd_undo(cfg, known)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
