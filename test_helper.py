#!/usr/bin/env python3
"""Unit tests for elemental_fracture.py — all CDP/socket calls mocked."""

import json
import unittest
from unittest import mock

import elemental_fracture as ef


PAGE = {"type": "page", "url": "http://tauri.localhost/",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9339/x"}


class FakeWS:
    """Mock websocket with canned Runtime.evaluate responses."""

    def __init__(self, responses=None):
        self.sent = []
        self.responses = responses or {}
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        mid = json.loads(self.sent[-1])["id"]
        expr = json.loads(self.sent[-1])["params"]["expression"]
        for key, val in self.responses.items():
            if key in expr:
                return json.dumps({
                    "id": mid, "result": {
                        "result": {"type": "string", "value": val}}})
        return json.dumps({
            "id": mid, "result": {
                "result": {"type": "string", "value": None}}})

    def close(self):
        self.closed = True


def make_cdp(responses=None, ws=None, pages=None):
    ws = ws or FakeWS(responses)
    cdp = ef.CDP(sock_factory=lambda *a, **k: ws,
                 http_get=lambda url: pages if pages is not None else [PAGE])
    return cdp, ws


def fake_cdp(responses=None):
    cdp, ws = make_cdp(responses)
    cdp.connect()
    return cdp, ws


class TestURLValidation(unittest.TestCase):
    def test_page_url_rules(self):
        self.assertTrue(ef._valid_page_url("http://tauri.localhost/"))
        self.assertTrue(ef._valid_page_url("http://tauri.localhost/x?y=1"))
        self.assertFalse(ef._valid_page_url("http://tauri.localhost.evil/"))
        self.assertFalse(ef._valid_page_url(
            "http://evil.com/tauri.localhost"))
        self.assertFalse(ef._valid_page_url("https://tauri.localhost/"))
        self.assertFalse(ef._valid_page_url("file://tauri.localhost/x"))
        self.assertFalse(ef._valid_page_url("not a url"))

    def test_ws_url_rules(self):
        self.assertTrue(ef._valid_ws_url("ws://127.0.0.1:9339/abc"))
        self.assertTrue(ef._valid_ws_url("ws://localhost:9339/x"))
        self.assertTrue(ef._valid_ws_url("ws://[::1]:9339/x"))
        self.assertFalse(ef._valid_ws_url("ws://evil.com:9339/x"))
        self.assertFalse(ef._valid_ws_url("ws://127.0.0.1.evil:9339/x"))
        self.assertFalse(ef._valid_ws_url("ws://127.0.0.1:4444/x"))
        self.assertFalse(ef._valid_ws_url("wss://127.0.0.1:9339/x"))
        self.assertFalse(ef._valid_ws_url("http://127.0.0.1:9339/x"))

    def test_attaches_only_valid_page(self):
        pages = [
            {"type": "page", "url": "http://tauri.localhost.evil/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9339/a"},
            {"type": "page", "url": "http://tauri.localhost/",
             "webSocketDebuggerUrl": "ws://evil.com:9339/b"},
            {"type": "worker", "url": "http://tauri.localhost/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9339/c"},
            {"type": "page", "url": "http://tauri.localhost/",
             "webSocketDebuggerUrl": "ws://127.0.0.1:9339/good"},
        ]
        seen = []
        ws = FakeWS()
        cdp = ef.CDP(sock_factory=lambda u, **k: (seen.append(u), ws)[1],
                     http_get=lambda url: pages)
        cdp.connect()
        self.assertEqual(seen, ["ws://127.0.0.1:9339/good"])

    def test_no_matching_page_raises(self):
        cdp = ef.CDP(sock_factory=lambda u, **k: FakeWS(),
                     http_get=lambda url: [{"type": "page", "url": "x"}])
        with self.assertRaises(ef.HelperError):
            cdp.connect()


class TestConnectErrors(unittest.TestCase):
    def test_http_oserror_wrapped(self):
        def boom(url):
            raise OSError("connection refused")
        cdp = ef.CDP(http_get=boom)
        with self.assertRaises(ef.HelperError):
            cdp.connect()

    def test_socket_oserror_wrapped(self):
        def factory(*a, **k):
            raise OSError("refused")
        cdp = ef.CDP(sock_factory=factory, http_get=lambda u: [PAGE])
        with self.assertRaises(ef.HelperError):
            cdp.connect()

    def test_status_no_startup_prompt(self):
        """--status must connect only — never prompt or start."""
        calls = []
        cdp = ef.CDP(http_get=lambda u: (_ for _ in ()).throw(
            OSError("refused")))
        h = ef.Helper(cdp=cdp,
                      start_fn=lambda: calls.append("started"),
                      out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True) as cf:
            with self.assertRaises(ef.HelperError):
                h.connect_or_start(offer_start=False)
        cf.assert_not_called()
        self.assertEqual(calls, [])


class TestInvoke(unittest.TestCase):
    def test_ok_response(self):
        cdp, _ = fake_cdp({'"get_status"': json.dumps({"ok": {"a": 1}})})
        self.assertEqual(cdp.invoke("get_status"), {"a": 1})

    def test_err_response_raises(self):
        cdp, _ = fake_cdp({'"get_status"': json.dumps({"err": "nope"})})
        with self.assertRaises(ef.HelperError):
            cdp.invoke("get_status")

    def test_protocol_error_raises(self):
        class BadWS(FakeWS):
            def recv(self):
                return json.dumps({"id": json.loads(self.sent[-1])["id"],
                                   "error": {"code": -32000,
                                             "message": "bad"}})
        cdp, _ = make_cdp(ws=BadWS())
        cdp.connect()
        with self.assertRaises(ef.HelperError):
            cdp.invoke("get_status")

    def test_exception_details_raises(self):
        class ExWS(FakeWS):
            def recv(self):
                return json.dumps({"id": json.loads(self.sent[-1])["id"],
                                   "result": {"exceptionDetails": {
                                       "text": "ReferenceError"}}})
        cdp, _ = make_cdp(ws=ExWS())
        cdp.connect()
        with self.assertRaises(ef.HelperError):
            cdp.invoke("get_status")


class JobWS(FakeWS):
    """Responds to job-slot protocol; states taken from queue."""

    states = ["pending", "done"]
    job_value = 42

    def recv(self):
        mid = json.loads(self.sent[-1])["id"]
        expr = json.loads(self.sent[-1])["params"]["expression"]
        for key, val in self.responses.items():
            if key in expr:
                return json.dumps({"id": mid, "result": {
                    "result": {"value": val}}})
        if "__EF_HELPER_JOBS" in expr and "invoke" not in expr:
            state = self.states.pop(0) if self.states else "done"
            return json.dumps({"id": mid, "result": {"result": {
                "value": json.dumps({"state": state,
                                     "value": self.job_value})}}})
        if "invoke" in expr:
            return json.dumps({"id": mid, "result": {"result": {
                "value": json.dumps({"ok": None})}}})
        return json.dumps({"id": mid, "result": {
            "result": {"value": "[]"}}})


class TestLongJobPoll(unittest.TestCase):
    def test_poll_returns_done(self):
        ws = JobWS()
        ws.states = ["pending", "pending", "done"]
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        val = ef.run_long(cdp, "download_game", {"dest": "C:\\x"})
        self.assertEqual(val, 42)

    def test_job_ids_unique(self):
        cdp, _ = fake_cdp({})
        j1 = cdp.start_job("verify_game_files")
        j2 = cdp.start_job("verify_game_files")
        self.assertNotEqual(j1, j2)

    def test_missing_state_is_unknown_outcome(self):
        ws = JobWS()
        ws.states = ["pending", "missing"]
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        with self.assertRaises(ef.DisconnectedError):
            ef.run_long(cdp, "verify_game_files", {"repair": False})

    def test_no_retry_on_disconnect(self):
        """A disconnect mid-call must raise, never silently retry."""
        class DeadWS(FakeWS):
            calls = [0]

            def send(self, payload):
                super().send(payload)
                DeadWS.calls[0] += 1
                if DeadWS.calls[0] > 1:
                    raise OSError("socket dead")
        ws = DeadWS({'"get_status"': json.dumps({"ok": {"valid": True}})})
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        cdp.invoke("get_status")
        with self.assertRaises(ef.DisconnectedError):
            cdp.invoke("download_game", {"dest": "C:\\x"})


class TestOutdatedLogic(unittest.TestCase):
    def test_mismatch(self):
        self.assertIs(ef.is_outdated(
            {"local_game": 1, "remote_game": 2}), True)

    def test_match(self):
        self.assertIs(ef.is_outdated(
            {"local_game": 2, "remote_game": 2,
             "local_mod": 1, "remote_mod": 1}), False)

    def test_mod_mismatch(self):
        self.assertIs(ef.is_outdated(
            {"local_game": 2, "remote_game": 2,
             "local_mod": 0, "remote_mod": 1}), True)

    def test_one_populated_component_matching_is_current(self):
        # remote_game set+matching, remote_mod empty => current, no
        # fallthrough to combined remote/local
        self.assertIs(ef.is_outdated(
            {"local_game": "6.4.5", "remote_game": "6.4.5",
             "local_mod": None, "remote_mod": "",
             "local": 0, "remote": 9}), False)

    def test_empty_strings_falsy(self):
        self.assertIs(ef.is_outdated(
            {"remote_game": "", "remote_mod": "",
             "local": 1, "remote": 2}), True)
        self.assertEqual(ef.is_outdated(
            {"remote_game": "", "remote_mod": "", "local": 1}),
            "unknown")

    def test_remote_only(self):
        self.assertIs(ef.is_outdated({"local": 1, "remote": 2}), True)
        self.assertIs(ef.is_outdated({"local": 2, "remote": 2}), False)

    def test_unknown_remote(self):
        self.assertEqual(ef.is_outdated({"local": 1}), "unknown")
        self.assertEqual(ef.is_outdated(None), "unknown")


class TestSecrets(unittest.TestCase):
    def test_sanitize_hides_tokens(self):
        s = ef.sanitize({"auth_token": "abc123secret", "name": "u"})
        self.assertEqual(s["auth_token"], "***")
        self.assertEqual(s["name"], "u")

    def test_signed_in_requires_auth_token(self):
        # username alone proves nothing
        ok, user = ef.signed_in({"username": "henry"})
        self.assertFalse(ok)
        self.assertEqual(user, "henry")
        ok, user = ef.signed_in({"auth_token": "SECRET",
                                 "username": "henry"})
        self.assertTrue(ok)
        self.assertEqual(user, "henry")
        ok, user = ef.signed_in({"account": {"username": "ghost"}})
        self.assertFalse(ok)
        self.assertIsNone(user)

    def test_status_never_prints_token(self):
        out = []
        settings = {"auth_token": "SUPERSECRET", "username": "u1"}
        cdp, _ = fake_cdp({
            '"get_settings"': json.dumps({"ok": settings}),
            '"get_status"': json.dumps({"ok": {"valid": True}}),
            '"get_versions"': json.dumps({"ok": {"local": 1,
                                                "remote": 1}}),
            '"get_launcher_update"': json.dumps({"ok": {"update": False}}),
        })
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=out.append)
        h.status()
        joined = "\n".join(out)
        self.assertNotIn("SUPERSECRET", joined)
        self.assertIn("u1", joined)

    def test_login_backend_error_never_leaks_password(self):
        cdp, _ = fake_cdp({
            '"login_email"': json.dumps({
                "err": "login failed for pass=hunter2! bad creds"}),
        })
        h = ef.Helper(cdp=cdp, start_fn=lambda: None,
                      out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True), \
                mock.patch("builtins.input", return_value="a@b.c"), \
                mock.patch.object(ef.getpass, "getpass",
                                  return_value="hunter2!"):
            with self.assertRaises(ef.HelperError) as ctx:
                h.sign_in()
        self.assertNotIn("hunter2", str(ctx.exception))
        # raise-from-None suppresses the leaking exception in tracebacks
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_login_eval_exception_never_leaks_password(self):
        class ExWS(FakeWS):
            def recv(self):
                return json.dumps({
                    "id": json.loads(self.sent[-1])["id"],
                    "result": {"exceptionDetails": {
                        "text": "hunter2! was undefined"}}})
        cdp, _ = make_cdp(ws=ExWS())
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None,
                      out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True), \
                mock.patch("builtins.input", return_value="a@b.c"), \
                mock.patch.object(ef.getpass, "getpass",
                                  return_value="hunter2!"):
            with self.assertRaises(ef.HelperError) as ctx:
                h.sign_in()
        self.assertNotIn("hunter2", str(ctx.exception))


class TestDownloadFlow(unittest.TestCase):
    def _helper(self, responses):
        cdp, ws = fake_cdp(responses)
        return ef.Helper(cdp=cdp, start_fn=lambda: None,
                         out=lambda *a: None), ws

    def test_verdict_object_ok_invokes_download(self):
        class DLWS(JobWS):
            def send(self, payload):
                super().send(payload)
        resp = {
            '"suggested_install_dir"': json.dumps({"ok": "C:\\Games\\EF"}),
            '"check_game_folder"': json.dumps(
                {"ok": {"verdict": "ok", "fixed": False}}),
        }
        ws = DLWS(resp)
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None,
                      out=lambda *a: None)
        with mock.patch("builtins.input", return_value=""), \
                mock.patch.object(ef, "confirm", return_value=True):
            h.download()
        self.assertIn("download_game", "\n".join(ws.sent))

    def test_bad_verdict_refuses(self):
        resp = {
            '"suggested_install_dir"': json.dumps({"ok": "C:\\x"}),
            '"check_game_folder"': json.dumps(
                {"ok": {"verdict": "unusual-verdict", "fixed": False}}),
        }
        h, ws = self._helper(resp)
        with mock.patch("builtins.input", return_value=""):
            h.download()
        self.assertNotIn("download_game", "\n".join(ws.sent))

    def test_decline_download_no_mutation(self):
        resp = {
            '"suggested_install_dir"': json.dumps({"ok": "C:\\Games\\EF"}),
            '"check_game_folder"': json.dumps(
                {"ok": {"verdict": "ok", "fixed": False}}),
        }
        h, ws = self._helper(resp)
        with mock.patch("builtins.input", return_value=""), \
                mock.patch.object(ef, "confirm", return_value=False):
            h.download()
        self.assertNotIn("download_game", "\n".join(ws.sent))


class TestUpdateAndPlay(unittest.TestCase):
    INSTALLED = {'"get_status"': json.dumps({"ok": {"exe_present": True,
                                                   "valid": True}})}

    def test_verify_declined_no_call(self):
        h, ws = (lambda c: (ef.Helper(cdp=c, start_fn=lambda: None,
                                      out=lambda *a: None), c._ws))(
            fake_cdp({})[0])
        with mock.patch.object(ef, "confirm", return_value=False):
            h.verify()
        self.assertNotIn("verify_game_files", "\n".join(ws.sent))

    def test_update_not_installed_skips(self):
        ws = FakeWS({'"get_status"': json.dumps(
            {"ok": {"exe_present": False}})})
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True):
            self.assertFalse(h.update_game())
        self.assertNotIn("update_patch", "\n".join(ws.sent))

    def test_play_not_installed_refuses(self):
        ws = FakeWS({'"get_status"': json.dumps(
            {"ok": {"exe_present": False}})})
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True):
            h.play()
        self.assertNotIn("launch_game", "\n".join(ws.sent))

    def test_play_unknown_version_refuses(self):
        resp = dict(self.INSTALLED)
        resp['"get_versions"'] = json.dumps({"ok": {"local": 1}})
        ws = FakeWS(resp)
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True):
            h.play()
        self.assertNotIn("launch_game", "\n".join(ws.sent))

    def test_update_accepted_second_declined_never_launches(self):
        """Outdated + 'update now?' yes + 'run update?' no => no launch."""
        resp = dict(self.INSTALLED)
        resp['"get_versions"'] = json.dumps(
            {"ok": {"local": 1, "remote": 2}})
        ws = FakeWS(resp)
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=lambda *a: None)
        answers = iter([True, False])
        with mock.patch.object(ef, "confirm",
                               side_effect=lambda *a: next(answers)):
            h.play()
        self.assertNotIn("launch_game", "\n".join(ws.sent))
        self.assertNotIn("update_patch", "\n".join(ws.sent))

    def test_play_sequence_runtime_then_launch(self):
        order = []

        class SeqWS(JobWS):
            def send(self, payload):
                super().send(payload)
                expr = json.loads(payload)["params"]["expression"]
                for cmd in ("ensure_runtime", "launch_game"):
                    if '"%s"' % cmd in expr:
                        order.append(cmd)

            def recv(self):
                mid = json.loads(self.sent[-1])["id"]
                expr = json.loads(self.sent[-1])["params"]["expression"]
                if "__EF_HELPER_JOBS" in expr and "invoke" not in expr:
                    return json.dumps({"id": mid, "result": {"result": {
                        "value": json.dumps({"state": "done",
                                             "value": None})}}})
                for key, val in self.responses.items():
                    if key in expr:
                        return json.dumps({"id": mid, "result": {
                            "result": {"value": val}}})
                if "invoke" in expr:
                    return json.dumps({"id": mid, "result": {"result": {
                        "value": json.dumps({"ok": None})}}})
                return json.dumps({"id": mid, "result": {
                    "result": {"value": "[]"}}})
        resp = dict(self.INSTALLED)
        resp['"get_versions"'] = json.dumps(
            {"ok": {"local": 1, "remote": 1}})
        ws = SeqWS(resp)
        cdp, _ = make_cdp(ws=ws)
        cdp.connect()
        h = ef.Helper(cdp=cdp, start_fn=lambda: None, out=lambda *a: None)
        with mock.patch.object(ef, "confirm", return_value=True):
            h.play()
        self.assertEqual(order, ["ensure_runtime", "launch_game"])


class TestCompatCheck(unittest.TestCase):
    def test_patch_mismatch_fails(self):
        import tempfile, os
        data = bytearray(0xC00000)  # must exceed DEBUG_OFF (0xb031d8)
        data[ef.PATCH_OFF:ef.PATCH_OFF + 5] = b"\x00" * 5  # wrong bytes
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(bytes(data))
            path = f.name
        try:
            with mock.patch.object(ef, "LAUNCHER_EXE", path):
                with self.assertRaises(ef.HelperError) as ctx:
                    ef.check_compat()
                self.assertIn("compatibility repair",
                              str(ctx.exception).lower())
        finally:
            os.unlink(path)

    def test_correct_bytes_pass(self):
        import tempfile, os
        data = bytearray(0xC00000)  # must exceed DEBUG_OFF (0xb031d8)
        data[ef.PATCH_OFF:ef.PATCH_OFF + len(ef.PATCH_BYTES)] = \
            ef.PATCH_BYTES
        data[ef.DEBUG_OFF:ef.DEBUG_OFF + len(ef.DEBUG_FLAGS)] = \
            ef.DEBUG_FLAGS
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(bytes(data))
            path = f.name
        try:
            with mock.patch.object(ef, "LAUNCHER_EXE", path):
                ef.check_compat()
        finally:
            os.unlink(path)


class TestProgressFields(unittest.TestCase):
    def test_allowlist_only(self):
        p = ef.progress_fields({
            "progress": 42, "stage": "downloading", "bytes_raw": {"x": 1},
            "auth_token": "SECRET", "junk": [1, 2]})
        self.assertEqual(p, {"progress": 42, "stage": "downloading"})

    def test_non_dict_returns_none(self):
        self.assertIsNone(ef.progress_fields("nope"))
        self.assertIsNone(ef.progress_fields({"weird": object()}))


if __name__ == "__main__":
    unittest.main()
