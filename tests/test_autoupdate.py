"""Tests for the autoupdate launchd job.

Nothing here loads a real job or writes to the real ~/Library: Path.home is
redirected at a temporary directory throughout.
"""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brewjanitor import autoupdate

BREW = "/opt/homebrew/bin/brew"


class HomeRedirected(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestParserIsImportable(HomeRedirected):
    def test_help_does_not_raise_a_name_error(self):
        # Regression: `argparse` was used but never imported, so every path into
        # this parser died with NameError before printing anything.
        with mock.patch("sys.stdout"):
            with self.assertRaises(SystemExit) as ctx:
                autoupdate.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_argparse_is_actually_bound_in_the_module(self):
        self.assertTrue(hasattr(autoupdate, "argparse"))

    def test_no_action_prints_help_and_returns_zero(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(autoupdate.main([]), 0)

    def test_install_rejects_an_out_of_range_hour(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(autoupdate.main(["--install", "--hour", "24"]), 1)
            self.assertEqual(autoupdate.main(["--install", "--hour", "-1"]), 1)

    def test_install_rejects_an_out_of_range_minute(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(autoupdate.main(["--install", "--minute", "60"]), 1)


class TestBuildPlist(HomeRedirected):
    def _plist(self, **kw):
        opts = dict(brew_path=BREW, hour=8, minute=0, greedy=False, cleanup=False)
        opts.update(kw)
        return plistlib.loads(autoupdate._build_plist(**opts))

    def test_schedule_is_a_daily_calendar_interval(self):
        p = self._plist(hour=7, minute=15)
        self.assertEqual(p["StartCalendarInterval"], {"Hour": 7, "Minute": 15})

    def test_never_runs_at_load(self):
        # Installing the job must not kick off an upgrade there and then.
        self.assertFalse(self._plist()["RunAtLoad"])

    def test_the_job_only_ever_invokes_brew(self):
        command = self._plist()["ProgramArguments"][-1]
        for word in command.split():
            if word in ("&&", "update", "upgrade", "cleanup", "--greedy"):
                continue
            self.assertEqual(word, BREW, f"unexpected command in job: {word}")

    def test_default_job_is_update_then_upgrade(self):
        command = self._plist()["ProgramArguments"][-1]
        self.assertEqual(command, f"{BREW} update && {BREW} upgrade")

    def test_greedy_is_appended_to_upgrade_only(self):
        command = self._plist(greedy=True)["ProgramArguments"][-1]
        self.assertEqual(command, f"{BREW} update && {BREW} upgrade --greedy")

    def test_cleanup_is_appended_as_its_own_step(self):
        command = self._plist(cleanup=True)["ProgramArguments"][-1]
        self.assertEqual(command, f"{BREW} update && {BREW} upgrade && {BREW} cleanup")

    def test_label_is_stable_and_logs_go_under_the_user_home(self):
        p = self._plist()
        self.assertEqual(p["Label"], autoupdate.LABEL)
        self.assertTrue(p["StandardOutPath"].startswith(str(self.home)))


class TestStatusAndRemove(HomeRedirected):
    def test_status_reports_nothing_installed(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(autoupdate.status(), 0)

    def test_remove_is_a_no_op_when_nothing_is_installed(self):
        with mock.patch("sys.stdout"), mock.patch.object(autoupdate, "unload", return_value=0):
            self.assertEqual(autoupdate.remove(), 0)

    def test_status_reads_back_an_installed_plist(self):
        target = autoupdate._plist_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(autoupdate._build_plist(BREW, 9, 30, False, False))
        with mock.patch("sys.stdout"):
            self.assertEqual(autoupdate.status(), 0)

    def test_install_fails_cleanly_when_brew_is_missing(self):
        with mock.patch.object(autoupdate, "_resolve_brew", return_value=None), \
             mock.patch("sys.stderr"):
            self.assertEqual(autoupdate.install(8, 0, False, False), 1)


if __name__ == "__main__":
    unittest.main()
