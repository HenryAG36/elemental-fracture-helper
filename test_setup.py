#!/usr/bin/env python3
"""Tests for setup_helper.py — synthetic exe fixture, mocked registry."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import helper_config as hc
import setup_helper as sh


# ---- synthetic known-launcher manifest -----------------------------

def make_known():
    """A tiny synthetic manifest (no real proprietary bytes)."""
    original_body = bytearray(b"LAUNCHER" * 1000)  # 8000 bytes
    original_body[0x100:0x103] = b"AAA"            # patch A slot
    original_body[0x400:0x408] = b"ORIGSTR!"       # patch B slot
    patched_body = bytearray(original_body)
    patched_body[0x100:0x103] = b"BBB"
    patched_body[0x400:0x408] = b"PATCHED!"

    def digest(b):
        return hashlib.sha256(bytes(b)).hexdigest()
    return {
        "size": len(original_body),
        "original_sha256": digest(original_body),
        "patched_sha256": digest(patched_body),
        "patches": [
            {"offset": 0x100, "old": b"AAA", "new": b"BBB"},
            {"offset": 0x400, "old": b"ORIGSTR!", "new": b"PATCHED!"},
        ],
        "_original_body": bytes(original_body),
        "_patched_body": bytes(patched_body),
    }


KNOWN = make_known()


class Env(unittest.TestCase):
    """Sandboxed bottle layout + registry stub per test."""

    def setUp(self):
        # realpath: macOS TMPDIR is a symlink and config uses realpath
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.bottles = os.path.join(self.tmp, "Bottles dir with space")
        self.bottle_dir = os.path.join(self.bottles, "Spellbreak")
        self.drive_c = os.path.join(self.bottle_dir, "drive_c")
        self.exe_dir = os.path.join(
            self.drive_c, "users/crossover/AppData/Local/Programs",
            "Elemental Fracture")
        os.makedirs(self.exe_dir)
        self.exe = os.path.join(self.exe_dir, "Elemental Fracture.exe")
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_original_body"])
        with open(os.path.join(self.bottle_dir, "user.reg"), "w") as f:
            f.write("WINE REGISTRY Version 2\n")
        # registry: {key: {name: value}} — run() implements query/add/del
        self.reg = {}
        self.cfg = hc.Config(
            bottle="Spellbreak", bottles_dir=self.bottles,
            crossover="/nonexistent/CrossOver.app",
            launcher_exe=self.exe, wine_exe_path="C:\\x\\ef.exe")
        self.state_p = mock.patch.object(
            sh, "STATE_DIR", os.path.join(self.tmp, ".state"))
        self.journal_p = mock.patch.object(
            sh, "JOURNAL",
            os.path.join(self.tmp, ".state", "setup-journal.json"))
        self.state_p.start()
        self.journal_p.start()

    def tearDown(self):
        self.journal_p.stop()
        self.state_p.stop()
        shutil.rmtree(self.tmp)

    # registry runner implementing reg query/add/delete ----------------
    def run_reg(self, *argv):
        class R:
            def __init__(self, rc, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err
        cmd, key = argv[1], argv[2]
        if cmd == "query":
            name = argv[4]
            store = self.reg.get(key, {})
            if name in store:
                return R(0, f"\n{key}\n    {name}    REG_SZ    "
                           f"{store[name]}\n")
            return R(1, "", "ERROR: The system was unable to find the "
                            "specified registry value.")
        if cmd == "add":
            name, value = argv[4], argv[6]
            self.reg.setdefault(key, {})[name] = value
            return R(0, "The operation completed successfully")
        if cmd == "delete":
            name = argv[4]
            if self.reg.get(key, {}).pop(name, None) is not None:
                return R(0, "The operation completed successfully")
            return R(1, "", "ERROR: unable to find value")
        return R(1, "", "unknown")


class TestExeState(Env):
    def test_original_patched_unknown(self):
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "original")
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_patched_body"])
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "patched")
        with open(self.exe, "wb") as f:
            f.write(b"garbage")
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "unknown")
        os.unlink(self.exe)
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "missing")

    def test_truncated_is_unknown(self):
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_original_body"][:-1])
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "unknown")


class TestApply(Env):
    def test_apply_patches_and_sets_override(self):
        out = []
        rc = sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                          confirm=lambda *a: True, out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "patched")
        self.assertEqual(
            self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME],
            sh.OVERRIDE_VALUE)
        j = sh._load_journal()
        self.assertEqual(j["stages"]["patch"]["state"], "applied")
        self.assertEqual(j["stages"]["override"]["state"], "set")
        self.assertIsNone(j["stages"]["override"]["previous"])
        self.assertTrue(os.path.exists(
            j["stages"]["exe_backup"]["path"]))
        self.assertTrue(os.path.exists(
            j["stages"]["reg_backup"]["path"]))

    def test_apply_declined_writes_nothing(self):
        with open(self.exe, "rb") as f:
            before = f.read()
        rc = sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                          confirm=lambda *a: False,
                          out=lambda *a: None)
        self.assertEqual(rc, 1)
        with open(self.exe, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.reg, {})
        self.assertFalse(os.path.exists(sh._journal_path()))

    def test_apply_unknown_exe_refuses(self):
        with open(self.exe, "wb") as f:
            f.write(b"totally different")
        with self.assertRaises(sh.SetupError):
            sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                         confirm=lambda *a: True, out=lambda *a: None)

    def test_apply_idempotent_on_patched(self):
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_patched_body"])
        out = []
        rc = sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                          confirm=lambda *a: True, out=out.append)
        self.assertEqual(rc, 0)
        j = sh._load_journal()
        self.assertEqual(j["stages"]["patch"]["state"],
                         "preexisting-patched")

    def test_apply_preserves_existing_override(self):
        self.reg.setdefault(sh.OVERRIDE_KEY, {})[sh.OVERRIDE_NAME] = \
            "native,builtin"
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        j = sh._load_journal()
        self.assertEqual(j["stages"]["override"]["state"],
                         "preexisting-same")

    def test_backup_before_write(self):
        calls = []

        def spy_run(*argv):
            calls.append(argv[0])
            return self.run_reg(*argv)
        sh.cmd_apply(self.cfg, KNOWN, run=spy_run,
                     confirm=lambda *a: True, out=lambda *a: None)
        j = sh._load_journal()
        backup = j["stages"]["exe_backup"]
        self.assertTrue(os.path.exists(backup["path"]))
        self.assertEqual(backup["sha256"], KNOWN["original_sha256"])

    def test_apply_twice_keeps_provenance_then_undo(self):
        """Second apply must not clobber first apply's journal."""
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        j1 = sh._load_journal()
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        j2 = sh._load_journal()
        self.assertEqual(j2["stages"]["patch"]["state"], "applied")
        self.assertEqual(j2["stages"]["override"]["state"], "set")
        self.assertEqual(j2["stages"]["exe_backup"]["path"],
                         j1["stages"]["exe_backup"]["path"])
        # undo after double apply still returns to original
        sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                    out=lambda *a: None)
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "original")
        self.assertNotIn(sh.OVERRIDE_NAME,
                         self.reg.get(sh.OVERRIDE_KEY, {}))

    def test_wrong_bottle_apply_refuses_mutation(self):
        """Journal for target A must block apply on target B entirely."""
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        other_exe = os.path.join(self.tmp, "other", "Elemental Fracture.exe")
        os.makedirs(os.path.dirname(other_exe))
        with open(other_exe, "wb") as f:
            f.write(KNOWN["_original_body"])
        cfg2 = hc.Config(bottle="Other", bottles_dir=self.bottles,
                         crossover="/nonexistent/CrossOver.app",
                         launcher_exe=other_exe,
                         wine_exe_path="C:\\o\\ef.exe")
        with self.assertRaises(sh.SetupError):
            sh.cmd_apply(cfg2, KNOWN, run=self.run_reg,
                         confirm=lambda *a: True, out=lambda *a: None)
        # nothing about target B was touched
        self.assertEqual(sh.exe_state(other_exe, KNOWN), "original")

    def test_wrong_bottle_undo_refuses(self):
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        cfg2 = hc.Config(bottle="Other", bottles_dir=self.bottles,
                         crossover="/nonexistent/CrossOver.app",
                         launcher_exe=os.path.join(self.tmp, "nope.exe"),
                         wine_exe_path="C:\\o\\ef.exe")
        with self.assertRaises(sh.SetupError):
            sh.cmd_undo(cfg2, KNOWN, run=self.run_reg,
                        out=lambda *a: None)
        # our target's state is untouched
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "patched")
        self.assertEqual(self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME],
                         sh.OVERRIDE_VALUE)

    def test_pending_patch_promotes_to_applied_keeping_backup(self):
        """Crash between write and journal: pending + patched exe must
        promote to 'applied' with the original exe_backup preserved."""
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        j = sh._load_journal()
        backup = j["stages"]["exe_backup"]["path"]
        # simulate interrupted apply: mark patch pending again
        j["stages"]["patch"]["state"] = "pending"
        sh._save_journal(j)
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        j2 = sh._load_journal()
        self.assertEqual(j2["stages"]["patch"]["state"], "applied")
        self.assertEqual(j2["stages"]["exe_backup"]["path"], backup)

    def test_applied_journal_with_original_exe_refuses(self):
        """Journal says applied but exe is original -> inconsistent."""
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_original_body"])
        with self.assertRaises(sh.SetupError):
            sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                         confirm=lambda *a: True, out=lambda *a: None)
        # journal provenance untouched
        self.assertEqual(
            sh._load_journal()["stages"]["patch"]["state"], "applied")

    def test_reg_write_failure_keeps_patch_journal(self):
        """A registry failure after patching must leave undo info."""
        def broken_run(*argv):
            r = self.run_reg(*argv)
            if argv[1] == "add":
                class R:
                    returncode, stdout, stderr = 1, "", "add failed"
                return R()
            return r
        with self.assertRaises(sh.SetupError):
            sh.cmd_apply(self.cfg, KNOWN, run=broken_run,
                         confirm=lambda *a: True, out=lambda *a: None)
        j = sh._load_journal()
        # patch provenance + backup survive the failed override stage
        self.assertEqual(j["stages"]["patch"]["state"], "applied")
        self.assertIn("exe_backup", j["stages"])
        # and undo still restores the exe even with a broken reg path
        sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                    out=lambda *a: None)
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "original")


class TestUndo(Env):
    def test_undo_reverts_patch_and_override(self):
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        out = []
        rc = sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                         out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "original")
        self.assertNotIn(sh.OVERRIDE_NAME,
                         self.reg.get(sh.OVERRIDE_KEY, {}))
        self.assertFalse(os.path.exists(sh._journal_path()))

    def test_undo_restores_previous_override(self):
        self.reg.setdefault(sh.OVERRIDE_KEY, {})[sh.OVERRIDE_NAME] = \
            "builtin"
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        # apply marked it "set" with previous "builtin"
        self.assertEqual(self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME],
                         "native,builtin")
        sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                    out=lambda *a: None)
        self.assertEqual(self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME],
                         "builtin")

    def test_undo_skips_preexisting_patched(self):
        with open(self.exe, "wb") as f:
            f.write(KNOWN["_patched_body"])
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                    out=lambda *a: None)
        # patch was preexisting — must remain patched
        self.assertEqual(sh.exe_state(self.exe, KNOWN), "patched")

    def test_undo_refuses_wrong_override_value(self):
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        # someone changed the override after apply
        self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME] = "builtin"
        with self.assertRaises(sh.SetupError):
            sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                        out=lambda *a: None)
        # journal retained; value untouched
        self.assertTrue(os.path.exists(sh._journal_path()))
        self.assertEqual(self.reg[sh.OVERRIDE_KEY][sh.OVERRIDE_NAME],
                         "builtin")

    def test_undo_refuses_wrong_exe_state(self):
        sh.cmd_apply(self.cfg, KNOWN, run=self.run_reg,
                     confirm=lambda *a: True, out=lambda *a: None)
        with open(self.exe, "wb") as f:
            f.write(b"corrupted!")  # no longer recognized-patched
        with self.assertRaises(sh.SetupError):
            sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                        out=lambda *a: None)
        self.assertTrue(os.path.exists(sh._journal_path()))

    def test_undo_without_journal_fails(self):
        with self.assertRaises(sh.SetupError):
            sh.cmd_undo(self.cfg, KNOWN, run=self.run_reg,
                        out=lambda *a: None)


class TestRegHelpers(Env):
    def test_query_absent_vs_error(self):
        self.assertIsNone(sh.reg_query(self.run_reg))
        # unexpected registry failure raises, not silently "absent"
        def boom(*a):
            class R:
                returncode = 5
                stdout = ""
                stderr = "weird failure"
            return R()
        with self.assertRaises(sh.SetupError):
            sh.reg_query(boom)

    def test_query_parses_value(self):
        self.reg.setdefault(sh.OVERRIDE_KEY, {})[sh.OVERRIDE_NAME] = \
            "native,builtin"
        self.assertEqual(sh.reg_query(self.run_reg), "native,builtin")


class TestConfig(Env):
    def test_bottle_traversal_rejected(self):
        for bad in ("../evil", "a/b", "a\\b", "..", ""):
            with self.assertRaises(hc.ConfigError):
                hc.config_from_args(type("A", (), {
                    "bottle": bad, "bottles_dir": "/tmp",
                    "crossover": "/x", "launcher_exe": None})())

    def test_bottle_with_spaces_ok(self):
        cfg = hc.config_from_args(type("A", (), {
            "bottle": "My Bottle", "bottles_dir": "/tmp/x y",
            "crossover": "/x", "launcher_exe": None})())
        self.assertEqual(cfg.bottle_dir,
                         os.path.realpath("/tmp/x y/My Bottle"))

    def test_launcher_exe_must_be_in_bottle(self):
        with self.assertRaises(hc.ConfigError):
            hc.config_from_args(type("A", (), {
                "bottle": "Spellbreak", "bottles_dir": self.bottles,
                "crossover": "/x",
                "launcher_exe": "/tmp/elsewhere/evil.exe"})())
        cfg = hc.config_from_args(type("A", (), {
            "bottle": "Spellbreak", "bottles_dir": self.bottles,
            "crossover": "/x", "launcher_exe": self.exe})())
        self.assertTrue(cfg.wine_exe_path.startswith("C:\\"))

    def test_symlink_escape_rejected(self):
        """A symlink inside drive_c pointing outside must be rejected."""
        outside = os.path.join(self.tmp, "outside", "evil.exe")
        os.makedirs(os.path.dirname(outside))
        with open(outside, "wb") as f:
            f.write(b"x")
        link = os.path.join(self.exe_dir, "link.exe")
        os.symlink(outside, link)
        with self.assertRaises(hc.ConfigError):
            hc.config_from_args(type("A", (), {
                "bottle": "Spellbreak", "bottles_dir": self.bottles,
                "crossover": "/x", "launcher_exe": link})())

    def test_wine_bin_path(self):
        cfg = hc.config_from_args(type("A", (), {
            "bottle": "B", "bottles_dir": "/tmp",
            "crossover": "/Applications/CrossOver.app",
            "launcher_exe": None})())
        self.assertEqual(
            cfg.wine_bin,
            "/Applications/CrossOver.app/Contents/SharedSupport/"
            "CrossOver/bin/wine")


if __name__ == "__main__":
    unittest.main()
