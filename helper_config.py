#!/usr/bin/env python3
"""Shared path/configuration resolution for the Elemental Fracture helper.

Both ``elemental_fracture.py`` (runtime helper) and ``setup_helper.py``
(setup/undo) take the same option set so they always target the same
bottle, CrossOver install, and launcher exe.
"""

import os
import re
from dataclasses import dataclass

DEFAULT_BOTTLE = "Spellbreak"
DEFAULT_BOTTLES_DIR = os.path.expanduser(
    "~/Library/Application Support/CrossOver/Bottles")
DEFAULT_CROSSOVER = "/Applications/CrossOver.app"

LAUNCHER_EXE_REL = (
    "drive_c/users/crossover/AppData/Local/Programs/Elemental Fracture/"
    "Elemental Fracture.exe")
LAUNCHER_WIN_REL = (
    "C:\\users\\crossover\\AppData\\Local\\Programs\\Elemental Fracture\\"
    "Elemental Fracture.exe")

_BOTTLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


class ConfigError(Exception):
    pass


@dataclass
class Config:
    bottle: str
    bottles_dir: str
    crossover: str
    launcher_exe: str
    wine_exe_path: str

    @property
    def bottle_dir(self):
        return os.path.join(self.bottles_dir, self.bottle)

    @property
    def wine_bin(self):
        return os.path.join(
            self.crossover,
            "Contents/SharedSupport/CrossOver/bin/wine")


def _valid_bottle_name(name):
    """Bottle must be a plain directory basename — no traversal."""
    return bool(name) and _BOTTLE_NAME_RE.match(name) is not None \
        and os.path.basename(name) == name


def add_config_args(parser):
    """Attach the shared CLI options to an argparse parser."""
    parser.add_argument("--bottle", default=DEFAULT_BOTTLE,
                        help=f"CrossOver bottle name (default: "
                             f"{DEFAULT_BOTTLE})")
    parser.add_argument("--crossover", default=DEFAULT_CROSSOVER,
                        help="path to CrossOver.app")
    parser.add_argument("--launcher-exe", default=None,
                        help="launcher exe path (must live inside the "
                             "selected bottle's drive_c)")
    return parser


def config_from_args(args):
    """Build a validated Config from parsed args."""
    bottle = getattr(args, "bottle", DEFAULT_BOTTLE)
    if not _valid_bottle_name(bottle):
        raise ConfigError(
            f"invalid bottle name {bottle!r} — must be a plain directory "
            "name, no path separators")
    # realpath so a symlink can't escape the bottle for containment
    bottles_dir = os.path.realpath(os.path.expanduser(
        getattr(args, "bottles_dir", DEFAULT_BOTTLES_DIR)))
    crossover = os.path.realpath(os.path.expanduser(
        getattr(args, "crossover", DEFAULT_CROSSOVER)))
    bottle_dir = os.path.join(bottles_dir, bottle)

    override = getattr(args, "launcher_exe", None)
    if override:
        launcher_exe = os.path.realpath(os.path.expanduser(override))
        drive_c = os.path.join(bottle_dir, "drive_c") + os.sep
        if not launcher_exe.startswith(drive_c):
            raise ConfigError(
                f"--launcher-exe must resolve inside {drive_c}")
        rel = os.path.relpath(launcher_exe, os.path.join(
            bottle_dir, "drive_c"))
        wine_exe_path = "C:\\" + rel.replace("/", "\\")
    else:
        launcher_exe = os.path.join(bottle_dir, LAUNCHER_EXE_REL)
        wine_exe_path = LAUNCHER_WIN_REL

    return Config(bottle=bottle, bottles_dir=bottles_dir,
                  crossover=crossover, launcher_exe=launcher_exe,
                  wine_exe_path=wine_exe_path)


def default_config():
    class _A:
        pass
    return config_from_args(_A())
