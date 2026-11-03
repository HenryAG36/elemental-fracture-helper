#!/usr/bin/env python3
"""Minimal Elemental Fracture (Spellbreak) launcher helper for CrossOver.

Talks to the running launcher's Tauri backend over its Chrome DevTools
Protocol (CDP) port (9339). Provides sign-in, download, update, verify,
and play actions through a small terminal menu.

This helper never stores credentials, never prints auth tokens, and never
retries a mutating command after a disconnect (the outcome would be
unknowable). See README.md for usage and caveats.
"""

import getpass
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

import helper_config as hc

CDP_HTTP = "http://127.0.0.1:9339/json"
CDP_PORT = 9339
PAGE_HOST = "tauri.localhost"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
POLL_SECS = 0.5

LAUNCHER_EXE = os.path.expanduser(
    "~/Library/Application Support/CrossOver/Bottles/Spellbreak/drive_c/"
    "users/crossover/AppData/Local/Programs/Elemental Fracture/"
    "Elemental Fracture.exe")
WINE_BIN = ("/Applications/CrossOver.app/Contents/SharedSupport/"
            "CrossOver/bin/wine")
BOTTLE = "Spellbreak"
WINE_EXE_PATH = ("C:\\users\\crossover\\AppData\\Local\\Programs\\"
                 "Elemental Fracture\\Elemental Fracture.exe")

# Compatibility-repair bytes we rely on. If they change (launcher update),
# CDP access disappears and the launcher may not even run under CrossOver.
PATCH_OFF, PATCH_BYTES = 0x157F0B, bytes.fromhex("31c0909090")
DEBUG_OFF = 0xB031D8
DEBUG_FLAGS = (b"--disable-features=msWebOOUI,msPdfOOUI,"
               b"msSmartScreenProtection --remote-debugging-port=9339")
DEBUG_LEN = 105

# Tauri invoke commands (official, observed in the launcher's own bundle).
CMD_GET_SETTINGS = "get_settings"
CMD_GET_STATUS = "get_status"
CMD_GET_VERSIONS = "get_versions"
CMD_GET_LAUNCHER_UPDATE = "get_launcher_update"
CMD_SUGGESTED_DIR = "suggested_install_dir"
CMD_LOGIN = "login_email"
CMD_REFRESH = "refresh_account"
CMD_CHECK_DIR = "check_game_folder"
CMD_DOWNLOAD = "download_game"
CMD_UPDATE = "update_patch"
CMD_VERIFY = "verify_game_files"
CMD_RUNTIME = "ensure_runtime"
CMD_PLAY = "launch_game"
PROGRESS_LISTEN = "plugin:event|listen"
PROGRESS_UNLISTEN = "plugin:event|unlisten"
PROGRESS_EVENT = "install-progress"

# Scalar progress fields we may display; everything else stays hidden.
PROGRESS_NUMERIC = {"progress", "percent", "percentage", "done", "total",
                    "count", "remaining", "downloaded", "size", "speed",
                    "step", "current", "index", "eta", "seconds"}
PROGRESS_TEXT = {"stage", "message", "status", "phase", "step_name"}

# String fields we never echo verbatim from launcher responses.
SECRETISH_KEYS = {"auth_token", "token", "access_token", "refresh_token",
                  "password", "secret", "session"}


class HelperError(Exception):
    pass


class DisconnectedError(HelperError):
    """Socket/CDP broke mid-command — outcome of in-flight calls unknown."""


def _require_websocket():
    try:
        import websocket  # noqa: F401 — websocket-client package
    except ImportError:
        raise HelperError(
            "python3 'websocket-client' is required "
            "(pip3 install websocket-client)")
    return websocket


def _valid_page_url(url):
    """Only the official tauri.localhost page counts."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    return p.scheme == "http" and (p.hostname or "") == PAGE_HOST


def _valid_ws_url(url):
    """Debug socket must be ws:// on loopback:9339 — nothing else."""
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    return (p.scheme == "ws" and host in LOOPBACK_HOSTS
            and p.port == CDP_PORT)


class CDP:
    """Tiny CDP client: Runtime.evaluate on the tauri.localhost page."""

    def __init__(self, sock_factory=None, http_get=None):
        self._http_get = http_get or self._default_http_get
        self._factory = sock_factory
        self._ws = None
        self._next_id = 0
        self._events = []

    @staticmethod
    def _default_http_get(url):
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())

    def _targets(self):
        return self._http_get(CDP_HTTP)

    def connect(self):
        try:
            pages = self._targets()
        except (OSError, ValueError) as e:
            raise HelperError(f"cannot reach CDP :9339: {e}") from None
        pages = [p for p in pages
                 if p.get("type") == "page"
                 and _valid_page_url(p.get("url") or "")
                 and _valid_ws_url(p.get("webSocketDebuggerUrl") or "")]
        if not pages:
            raise HelperError(
                "no Elemental Fracture page with a loopback debugger "
                "found on CDP :9339 — is the launcher running?")
        ws_url = pages[0]["webSocketDebuggerUrl"]
        factory = self._factory or _require_websocket().create_connection
        try:
            self._ws = factory(ws_url, timeout=120, suppress_origin=True)
        except OSError as e:
            raise HelperError(f"cannot connect to debug socket: {e}") \
                from None

    def close(self):
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    def _send_eval(self, expr, await_promise=True):
        self._next_id += 1
        mid = self._next_id
        try:
            self._ws.send(json.dumps({
                "id": mid, "method": "Runtime.evaluate",
                "params": {"expression": expr,
                           "returnByValue": True,
                           "awaitPromise": await_promise}}))
        except Exception as e:
            raise DisconnectedError(f"CDP send failed: {e}") from None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self._ws.recv())
            except Exception as e:
                raise DisconnectedError(f"CDP recv failed: {e}") from None
            if msg.get("id") == mid:
                return msg
            self._events.append(msg)  # unrelated events, kept bounded
            if len(self._events) > 50:
                self._events = self._events[-50:]
        # a hung evaluate is a lost channel — in-flight state is unknown
        raise DisconnectedError("CDP evaluate timed out")

    def eval_expr(self, expr, await_promise=True):
        """Evaluate JS; returns the value or raises on errors."""
        r = self._send_eval(expr, await_promise)
        if "error" in r:
            raise HelperError(f"CDP protocol error: {r['error']}")
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            raise HelperError("eval exception: " + json.dumps(
                res["exceptionDetails"], default=str)[:400])
        return res.get("result", {}).get("value")

    def invoke(self, command, args=None):
        """Synchronous invoke for short commands."""
        arg_js = json.dumps(args if args is not None else {})
        expr = (
            "window.__TAURI_INTERNALS__.invoke(%s,%s)"
            ".then(v=>JSON.stringify({ok:v}))"
            ".catch(e=>JSON.stringify({err:String("
            "e&&e.message||JSON.stringify(e))}))" % (
                json.dumps(command), arg_js))
        raw = self.eval_expr(expr)
        if raw is None:
            raise HelperError(f"{command}: no response")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise HelperError(f"{command}: unexpected response") from None
        if not isinstance(parsed, dict):
            raise HelperError(f"{command}: unexpected response")
        if "err" in parsed:
            raise HelperError(f"{command} failed: {parsed['err']}")
        return parsed.get("ok")

    def start_job(self, command, args=None):
        """Begin a long-running invoke; returns a unique job id."""
        arg_js = json.dumps(args if args is not None else {})
        job = "efjob-%s-%s" % (command, uuid.uuid4().hex[:12])
        j = json.dumps(job)
        expr = (
            "window.__EF_HELPER_JOBS=window.__EF_HELPER_JOBS||{};"
            "window.__EF_HELPER_JOBS[%s]={state:'pending',value:null};"
            "window.__TAURI_INTERNALS__.invoke(%s,%s)"
            ".then(v=>{window.__EF_HELPER_JOBS[%s]"
            ".state='done';window.__EF_HELPER_JOBS[%s].value=v})"
            ".catch(e=>{window.__EF_HELPER_JOBS[%s].state='err';"
            "window.__EF_HELPER_JOBS[%s].value=String("
            "e&&e.message||JSON.stringify(e))});'ok'" % (
                j, json.dumps(command), arg_js, j, j, j, j))
        self.eval_expr(expr, await_promise=False)
        return job

    def poll_job(self, job):
        """Return job state dict {state, value} from the job slot."""
        expr = ("JSON.stringify((window.__EF_HELPER_JOBS||{})[%s]||"
                "{state:'missing'})") % json.dumps(job)
        raw = self.eval_expr(expr)
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"state": "unknown"}
        return parsed if isinstance(parsed, dict) else {"state": "unknown"}

    def drain_events(self, limit=20):
        out, self._events = self._events[:limit], self._events[limit:]
        return out


def sanitize(value, depth=0):
    """Strip secrets and shrink structures for display."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(s in kl for s in SECRETISH_KEYS):
                out[k] = "***"
            elif isinstance(v, (dict, list)):
                out[k] = sanitize(v, depth + 1)
            elif isinstance(v, str) and len(v) > 80:
                out[k] = v[:77] + "..."
            else:
                out[k] = v
        return out
    if isinstance(value, list):
        return [sanitize(v, depth + 1) for v in value[:12]]
    if isinstance(value, str) and len(value) > 120:
        return value[:117] + "..."
    return value


def signed_in(settings):
    """auth_token presence is the only authoritative sign-in signal."""
    if not isinstance(settings, dict):
        return False, None
    user = None
    for key in ("username", "user", "display_name", "name", "email"):
        v = settings.get(key)
        if isinstance(v, str) and v:
            user = v
            break
    return bool(settings.get("auth_token")), user


def is_outdated(versions):
    """Official outdated logic on truthy remote fields.

    If any component remote is populated, decide on those components
    alone — a populated, matching component means current. Otherwise
    fall back to the combined remote/local pair; no remote at all is
    unknown (never claim up-to-date on missing data).
    """
    if not isinstance(versions, dict):
        return "unknown"
    rg, rm = versions.get("remote_game"), versions.get("remote_mod")
    if rg or rm:
        if rg and versions.get("local_game") != rg:
            return True
        if rm and versions.get("local_mod") != rm:
            return True
        return False
    remote = versions.get("remote")
    if remote:
        return versions.get("local") != remote
    return "unknown"


def confirm(prompt):
    try:
        ans = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def check_compat():
    """Verify the compatibility repair bytes before starting wine."""
    if not os.path.exists(LAUNCHER_EXE):
        raise HelperError(f"Launcher exe missing: {LAUNCHER_EXE}")
    with open(LAUNCHER_EXE, "rb") as f:
        f.seek(PATCH_OFF)
        if f.read(len(PATCH_BYTES)) != PATCH_BYTES:
            raise HelperError(
                "Compatibility patch bytes changed — the launcher was "
                "probably updated and now requires compatibility repair "
                "before it can run (crash patch differs at %#x)."
                % PATCH_OFF)
        f.seek(DEBUG_OFF)
        raw = f.read(DEBUG_LEN)
        if not raw.startswith(DEBUG_FLAGS):
            raise HelperError(
                "Debug-flags bytes changed — the launcher was probably "
                "updated and now requires compatibility repair before CDP "
                "control will work (flags differ at %#x)." % DEBUG_OFF)


def start_launcher():
    check_compat()
    if not os.path.exists(WINE_BIN):
        raise HelperError(f"CrossOver wine not found: {WINE_BIN}")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}
    subprocess.Popen(
        [WINE_BIN, "--bottle", BOTTLE, WINE_EXE_PATH],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)


def progress_fields(payload):
    """Allowlisted scalar progress fields only."""
    if not isinstance(payload, dict):
        return None
    out = {}
    for k, v in payload.items():
        kl = str(k).lower()
        if any(s in kl for s in SECRETISH_KEYS):
            continue
        if kl in PROGRESS_NUMERIC and isinstance(v, (int, float, bool)):
            out[k] = v
        elif kl in PROGRESS_TEXT and isinstance(v, str):
            out[k] = v[:80]
    return out or None


def _progress_printer(cdp):
    """Attach the install-progress listener BEFORE starting a job.

    Returns a read() thunk; read.unlisten() releases the subscription.
    The event object is {payload: ...} — only the payload is displayed.
    """
    ok = [False]

    try:
        expr = (
            "window.__EF_PROGRESS=[];"
            "window.__EF_PROGRESS_CB=window.__TAURI_INTERNALS__"
            ".transformCallback(e=>{const p=e&&e.payload!==undefined?"
            "e.payload:e;window.__EF_PROGRESS.push(p);"
            "if(window.__EF_PROGRESS.length>200)"
            "window.__EF_PROGRESS=window.__EF_PROGRESS.slice(-100)});"
            "window.__TAURI_INTERNALS__.invoke(%s,{event:%s,"
            "target:{kind:'Any'},handler:window.__EF_PROGRESS_CB})"
            ".then(id=>{window.__EF_PROGRESS_EID=id;"
            "return JSON.stringify({ok:1})})"
            ".catch(e=>JSON.stringify({err:String("
            "e&&e.message||JSON.stringify(e))}))" % (
                json.dumps(PROGRESS_LISTEN),
                json.dumps(PROGRESS_EVENT)))
        raw = cdp.eval_expr(expr)
        try:
            ok[0] = bool(json.loads(raw).get("ok"))
        except (TypeError, json.JSONDecodeError, AttributeError):
            pass
    except HelperError:
        pass

    def read():
        if not ok[0]:
            return None
        try:
            raw = cdp.eval_expr(
                "(()=>{const q=window.__EF_PROGRESS||[];"
                "window.__EF_PROGRESS=[];return JSON.stringify(q)})()",
                await_promise=False)
            items = json.loads(raw) if raw else []
        except (HelperError, json.JSONDecodeError):
            return None
        out = []
        for it in items[-5:]:
            scal = progress_fields(it)
            if scal:
                out.append(scal)
        return out

    def unlisten():
        if not ok[0]:
            return
        try:
            cdp.eval_expr(
                "window.__EF_PROGRESS_EID!==undefined&&"
                "window.__TAURI_INTERNALS__.invoke(%s,{event:%s,"
                "eventId:window.__EF_PROGRESS_EID}).catch(()=>{});"
                "window.__TAURI_INTERNALS__.unregisterCallback&&"
                "window.__TAURI_INTERNALS__.unregisterCallback("
                "window.__EF_PROGRESS_CB);'ok'" % (
                    json.dumps(PROGRESS_UNLISTEN),
                    json.dumps(PROGRESS_EVENT)),
                await_promise=False)
        except Exception:
            pass
    read.unlisten = unlisten
    return read


def run_long(cdp, command, args=None, label="working"):
    """Start an invoke, poll it, show bounded progress; no retry."""
    progress = _progress_printer(cdp)   # listener before the job starts
    t0 = time.monotonic()
    try:
        job = cdp.start_job(command, args)
        while True:
            st = cdp.poll_job(job)
            if not isinstance(st, dict):
                raise DisconnectedError(
                    f"{command}: malformed job state — outcome unknown")
            state = st.get("state")
            if state == "done":
                return st.get("value")
            if state == "err":
                raise HelperError(f"{command} failed: {st.get('value')}")
            if state != "pending":
                raise DisconnectedError(
                    f"{command}: job state {state!r} — outcome unknown")
            ev = progress()
            elapsed = int(time.monotonic() - t0)
            if ev:
                last = ev[-1]
                msg = " ".join(f"{k}={v}" for k, v in list(last.items())[:6])
                print(f"\r{label}: {msg} ({elapsed}s)   ",
                      end="", flush=True)
            else:
                print(f"\r{label}: {elapsed}s elapsed   ",
                      end="", flush=True)
            cdp.drain_events()
            time.sleep(POLL_SECS)
    except KeyboardInterrupt:
        print(f"\ninterrupted — '{command}' may still be running in the "
              "launcher; stopping helper to avoid overlapping operations")
        raise DisconnectedError("interrupted by user")
    finally:
        progress.unlisten()
        print()


class Helper:
    def __init__(self, cdp=None, start_fn=None, out=print):
        self.cdp = cdp or CDP()
        self._start = start_fn or start_launcher
        self._out = out

    def connect_or_start(self, offer_start=True):
        try:
            self.cdp.connect()
            return
        except HelperError as e:
            if not offer_start:
                raise
            self._out(f"No live CDP endpoint ({e})")
        if not confirm("Launcher not reachable. Start it now?"):
            raise HelperError("cannot connect to launcher")
        self._start()
        self._out("Launcher starting — waiting for CDP (up to 60s)...")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                self.cdp.connect()
                return
            except HelperError:
                time.sleep(2)
        raise HelperError("launcher did not expose CDP within 60s")

    # -- read-only -----------------------------------------------------
    def status(self):
        settings = self.cdp.invoke(CMD_GET_SETTINGS)
        st = self.cdp.invoke(CMD_GET_STATUS)
        versions = self.cdp.invoke(CMD_GET_VERSIONS)
        update = self.cdp.invoke(CMD_GET_LAUNCHER_UPDATE)
        ok, user = signed_in(settings)
        self._out(f"signed_in: {ok}" + (f" ({user})" if user else ""))
        if isinstance(update, dict):
            self._out("launcher: current=%s remote=%s update=%s" % (
                update.get("current"), update.get("remote"),
                update.get("update")))
            if update.get("update"):
                self._out("  note: a launcher update is available; it may "
                          "overwrite the compatibility repair. Not applied "
                          "automatically.")
        self._out("status: " + json.dumps(sanitize(st)))
        self._out("versions: " + json.dumps(sanitize(versions)))
        od = is_outdated(versions)
        self._out("outdated: " + (
            "yes" if od is True else "no" if od is False else "unknown"))
        return {"settings": settings, "status": st,
                "versions": versions, "update": update, "outdated": od}

    # -- mutating ------------------------------------------------------
    def sign_in(self):
        if not confirm("Sign in to Elemental Fracture?"):
            self._out("cancelled")
            return
        email = input("Email: ").strip()
        password = getpass.getpass("Password: ")
        if not email or not password:
            self._out("cancelled — email and password required")
            return
        args = {"email": email, "password": password}
        try:
            self.cdp.invoke(CMD_LOGIN, args)
        except DisconnectedError:
            raise DisconnectedError(
                "sign-in: connection lost — outcome unknown") from None
        except HelperError:
            # backend/eval errors can echo submitted fields — don't leak
            raise HelperError("sign-in failed (check credentials or "
                              "launcher state)") from None
        finally:
            password = None
            args.clear()
        try:
            self.cdp.invoke(CMD_REFRESH)
        except HelperError as e:
            self._out(f"login returned but refresh failed: {e}")
        settings = self.cdp.invoke(CMD_GET_SETTINGS)
        ok, user = signed_in(settings)
        self._out("signed in" if ok else "still signed out")
        if user:
            self._out(f"user: {user}")

    def download(self):
        try:
            dest = self.cdp.invoke(CMD_SUGGESTED_DIR)
        except HelperError:
            dest = None
        if isinstance(dest, dict):
            dest = dest.get("path") or dest.get("dir") or str(dest)
        dest = input(f"Install directory [{dest or '?'}]: ").strip() or dest
        if not dest:
            self._out("cancelled — no directory chosen")
            return
        verdict = self.cdp.invoke(
            CMD_CHECK_DIR,
            {"path": dest, "fix": False, "adminPrompt": None})
        # official UI switches on an object: {verdict: 'ok'|...}
        ok = (verdict.get("verdict") if isinstance(verdict, dict)
              else verdict)
        if ok != "ok":
            self._out(f"check_game_folder returned "
                      f"{json.dumps(sanitize(verdict))} — verdict is not "
                      "'ok'; refusing to download into that path. "
                      "Pick another directory.")
            return
        if not confirm(f"Download the game into '{dest}'? (large "
                       "download)"):
            self._out("cancelled")
            return
        run_long(self.cdp, CMD_DOWNLOAD, {"dest": dest},
                 label="downloading")
        self._out("download command finished")

    def update_game(self):
        """Run the game update if one is needed. Returns True on success."""
        st = self.cdp.invoke(CMD_GET_STATUS)
        if isinstance(st, dict) and not st.get("exe_present"):
            self._out("game isn't installed — download it first")
            return False
        versions = self.cdp.invoke(CMD_GET_VERSIONS)
        od = is_outdated(versions)
        if od is False:
            self._out("game is up to date")
            return True
        if od == "unknown":
            self._out("could not determine remote version — "
                      "not claiming up-to-date; update anyway?")
        else:
            self._out("update available: " +
                      json.dumps(sanitize(versions)))
        if not confirm("Run game update?"):
            self._out("cancelled")
            return False
        run_long(self.cdp, CMD_UPDATE, {}, label="updating")
        self._out("update finished")
        return True

    def verify(self):
        if not confirm("Verify installed game files (read-only check)?"):
            self._out("cancelled")
            return
        run_long(self.cdp, CMD_VERIFY, {"repair": False},
                 label="verifying")
        self._out("verification finished")

    def play(self):
        st = self.cdp.invoke(CMD_GET_STATUS)
        if not (isinstance(st, dict) and st.get("exe_present")):
            self._out("game isn't installed — download it first")
            return
        versions = self.cdp.invoke(CMD_GET_VERSIONS)
        od = is_outdated(versions)
        if od is True:
            self._out("game is outdated; an update is required.")
            if not confirm("Update now before playing?"):
                self._out("refusing to play: update declined")
                return
            if not self.update_game():
                return  # declined or failed — never launch
            # re-check: update claimed done, must now be current
            od2 = is_outdated(self.cdp.invoke(CMD_GET_VERSIONS))
            if od2 is not False:
                self._out("still not confirmed current after update — "
                          "refusing to play")
                return
        elif od == "unknown":
            self._out("couldn't confirm version state — refusing to play")
            return
        if not confirm("Launch the game now?"):
            self._out("cancelled")
            return
        run_long(self.cdp, CMD_RUNTIME, {}, label="preparing runtime")
        self.cdp.invoke(CMD_PLAY, {})
        self._out("game launch requested")


MENU = [
    ("Status", "status"),
    ("Sign in", "sign_in"),
    ("Download", "download"),
    ("Update game", "update_game"),
    ("Verify files (read-only)", "verify"),
    ("Play", "play"),
    ("Quit helper", None),
]


def interactive(helper):
    helper.connect_or_start()
    while True:
        print("\n== Elemental Fracture ==")
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i}. {label}")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
            continue
        _, action = MENU[int(choice) - 1]
        if action is None:
            break
        try:
            getattr(helper, action)()
        except DisconnectedError:
            print("\nconnection lost mid-operation — outcome unknown; "
                  "check the launcher before retrying")
            break
        except HelperError as e:
            print(f"error: {e}")
    helper.cdp.close()


def apply_config(cfg):
    """Point the module's launch/compat paths at the selected bottle."""
    global BOTTLE, WINE_BIN, LAUNCHER_EXE, WINE_EXE_PATH
    BOTTLE = cfg.bottle
    WINE_BIN = cfg.wine_bin
    LAUNCHER_EXE = cfg.launcher_exe
    WINE_EXE_PATH = cfg.wine_exe_path


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        print("usage: elemental_fracture.py [--status] [--help] "
              "[--bottle N] [--crossover PATH] [--launcher-exe PATH]")
        print("  default: interactive menu")
        print("  --status: read-only status, then exit")
        return 0
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true")
    hc.add_config_args(parser)
    args = parser.parse_args(argv)
    try:
        apply_config(hc.config_from_args(args))
    except hc.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    helper = Helper()
    try:
        if args.status:
            helper.connect_or_start(offer_start=False)
            helper.status()
        else:
            interactive(helper)
    except DisconnectedError:
        print("connection lost mid-operation — outcome unknown")
        return 2
    except HelperError as e:
        print(f"error: {e}")
        return 1
    finally:
        helper.cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
