"""Piece 1 — inventory: find every .app bundle on the machine.

Scans /Applications and ~/Applications (never /System/Applications, which is
owned by macOS and must not be touched). For each bundle it reads the bundle
identifier from Contents/Info.plist (key CFBundleIdentifier) using the stdlib
plistlib, and returns an App dataclass: {name, path, bundle_id}.

This module is pure read-only: it only lists files and reads plists. It never
mutates, installs, or deletes anything, so it is always safe to run.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import sys
from pathlib import Path

from .streams import out as _out

# Directories we consider "user-installable" app locations. /System/Applications
# is deliberately excluded: those bundles ship with macOS and are not something
# brewjanitor should ever try to manage or replace.
DEFAULT_SCAN_DIRS = ("/Applications", os.path.expanduser("~/Applications"))


@dataclasses.dataclass(frozen=True)
class App:
    """A discovered .app bundle on disk.

    Attributes:
        name: the bundle directory name, e.g. "Safari.app".
        path: absolute path to the .app bundle directory.
        bundle_id: the CFBundleIdentifier from Info.plist, or "" if the plist
            was missing, unreadable, or did not contain the key.
    """

    name: str
    path: str
    bundle_id: str


def _bundle_identifier(app_path: Path) -> str:
    """Return CFBundleIdentifier for a bundle, or "" on any failure.

    Defensive by design: a broken, missing, or malformed plist must never crash
    the inventory pass. A bundle without a usable identifier simply gets an
    empty string so later stages can decide what to do with it.
    """
    info_plist = app_path / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return ""
    value = info.get("CFBundleIdentifier", "")
    return value if isinstance(value, str) else str(value or "")


def scan_dir(directory: str) -> list[App]:
    """Return every *.app bundle found directly under `directory`.

    We only look one level deep: macOS apps are self-contained .app directories,
    and nested bundles (inside another app's Contents/) are not independently
    managed apps. Nonexistent directories simply yield no results.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    apps: list[App] = []
    for entry in sorted(os.listdir(root)):
        if not entry.endswith(".app"):
            continue
        app_path = root / entry
        if not app_path.is_dir():
            continue
        apps.append(
            App(
                name=entry,
                path=str(app_path),
                bundle_id=_bundle_identifier(app_path),
            )
        )
    return apps


def inventory(directories: tuple[str, ...] | None = None) -> list[App]:
    """Find every .app bundle across the given directories.

    Args:
        directories: locations to scan. Defaults to DEFAULT_SCAN_DIRS, i.e.
            /Applications and ~/Applications.

    Returns:
        A deduplicated list of App objects (sorted by path), since an app can
        legitimately appear in both /Applications and ~/Applications.
    """
    dirs = directories if directories is not None else DEFAULT_SCAN_DIRS
    seen: dict[str, App] = {}
    for directory in dirs:
        for app in scan_dir(directory):
            seen[app.path] = app
    return sorted(seen.values(), key=lambda a: a.path)


def main() -> int:
    """Entry point for `python -m brewjanitor.inventory`.

    Prints one tab-separated line per app: name<TAB>path<TAB>bundle_id.
    Exits 0 on success.
    """
    for app in inventory():
        _out(f"{app.name}\t{app.path}\t{app.bundle_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
