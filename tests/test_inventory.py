"""Tests for piece 1 — inventory. Pure filesystem reads, no brew involved."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from brewjanitor.inventory import DEFAULT_SCAN_DIRS, App, inventory, scan_dir


def make_app(root: Path, name: str, bundle_id: str | None = "com.example.app") -> Path:
    """Create a minimal .app bundle on disk. bundle_id=None writes no plist."""
    app = root / name
    (app / "Contents").mkdir(parents=True)
    if bundle_id is not None:
        (app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": bundle_id})
        )
    return app


class TestScanDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_finds_app_bundles_and_reads_bundle_id(self):
        make_app(self.root, "Firefox.app", "org.mozilla.firefox")
        apps = scan_dir(str(self.root))
        self.assertEqual([a.name for a in apps], ["Firefox.app"])
        self.assertEqual(apps[0].bundle_id, "org.mozilla.firefox")

    def test_ignores_non_app_entries_and_plain_files(self):
        make_app(self.root, "Real.app")
        (self.root / "notes.txt").write_text("hi")
        (self.root / "SomeFolder").mkdir()
        (self.root / "Impostor.app").write_text("a file, not a bundle")
        self.assertEqual([a.name for a in scan_dir(str(self.root))], ["Real.app"])

    def test_missing_directory_yields_nothing(self):
        self.assertEqual(scan_dir(str(self.root / "nope")), [])

    def test_missing_plist_gives_empty_bundle_id_without_raising(self):
        make_app(self.root, "NoPlist.app", bundle_id=None)
        self.assertEqual(scan_dir(str(self.root))[0].bundle_id, "")

    def test_malformed_plist_gives_empty_bundle_id_without_raising(self):
        app = make_app(self.root, "Broken.app", bundle_id=None)
        (app / "Contents" / "Info.plist").write_bytes(b"\x00\x01 not a plist \xff")
        self.assertEqual(scan_dir(str(self.root))[0].bundle_id, "")

    def test_does_not_descend_into_nested_bundles(self):
        outer = make_app(self.root, "Outer.app")
        make_app(outer / "Contents", "Helper.app")
        self.assertEqual([a.name for a in scan_dir(str(self.root))], ["Outer.app"])


class TestInventory(unittest.TestCase):
    def test_dedupes_by_path_and_sorts(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            make_app(Path(a), "Zed.app")
            make_app(Path(b), "Alpha.app")
            apps = inventory((a, b, a))  # `a` listed twice on purpose
            self.assertEqual(len(apps), 2)
            self.assertEqual([x.path for x in apps], sorted(x.path for x in apps))

    def test_system_applications_is_never_scanned(self):
        # macOS-owned bundles must stay out of reach of every later stage.
        self.assertNotIn("/System/Applications", DEFAULT_SCAN_DIRS)
        self.assertIn("/Applications", DEFAULT_SCAN_DIRS)

    def test_app_is_hashable_and_frozen(self):
        app = App(name="A.app", path="/Applications/A.app", bundle_id="x")
        self.assertEqual(len({app, app}), 1)
        with self.assertRaises(Exception):
            app.name = "B.app"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
