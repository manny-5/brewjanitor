"""Tests for the autoupdate launchd job.

Nothing here loads a real job or writes to the real ~/Library: Path.home is
redirected at a temporary directory throughout.
"""

from __future__ import annotations

import contextlib
import io
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

    def test_brew_path_is_shell_quoted(self):
        path = "/tmp/brew tools/brew;echo unsafe"
        command = self._plist(brew_path=path)["ProgramArguments"][-1]
        self.assertEqual(
            command,
            "'/tmp/brew tools/brew;echo unsafe' update && "
            "'/tmp/brew tools/brew;echo unsafe' upgrade",
        )
        cleanup = self._plist(brew_path=path, cleanup=True)["ProgramArguments"][-1]
        self.assertEqual(
            cleanup,
            "'/tmp/brew tools/brew;echo unsafe' update && "
            "'/tmp/brew tools/brew;echo unsafe' upgrade && "
            "'/tmp/brew tools/brew;echo unsafe' cleanup",
        )

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


class TestMalformedPlists(HomeRedirected):
    """`status` reads a file this tool does not exclusively own.

    It can be hand-edited, written by an older version, or belong to something
    else entirely. Describing a malformed job is exactly when a readable answer
    matters most, so it must not raise.
    """

    def _status_of(self, data):
        target = autoupdate._plist_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps(data))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = autoupdate.status()
        return code, buf.getvalue()

    def test_a_plist_missing_hour_is_described_not_crashed_on(self):
        # Regression: `{hour:02d}` with the "?" default raised ValueError.
        code, out = self._status_of({"Label": "x", "ProgramArguments": ["a"]})
        self.assertEqual(code, 0)
        self.assertIn("unknown", out)

    def test_a_non_integer_hour_is_described(self):
        code, out = self._status_of({"StartCalendarInterval": {"Hour": "8", "Minute": 0}})
        self.assertEqual(code, 0)
        self.assertIn("malformed", out)

    def test_an_out_of_range_hour_is_called_out(self):
        code, out = self._status_of({"StartCalendarInterval": {"Hour": 99, "Minute": 0}})
        self.assertEqual(code, 0)
        self.assertIn("out of range", out)

    def test_an_empty_plist_is_survivable(self):
        code, out = self._status_of({})
        self.assertEqual(code, 0)
        self.assertIn("unknown", out)

    def test_non_string_program_arguments_are_coerced(self):
        # Types a plist can genuinely hold, but that are not strings.
        code, out = self._status_of({"ProgramArguments": [1, 2.5, "brew"]})
        self.assertEqual(code, 0)
        self.assertIn("brew", out)

    def test_a_valid_plist_still_reads_normally(self):
        code, out = self._status_of(
            plistlib.loads(autoupdate._build_plist(BREW, 9, 5, False, False))
        )
        self.assertEqual(code, 0)
        self.assertIn("daily at 09:05", out)

    def test_a_non_dict_schedule_does_not_raise(self):
        self.assertIn("unknown", autoupdate._describe_schedule("not a dict"))
        self.assertIn("unknown", autoupdate._describe_schedule(None))
        self.assertIn("unknown", autoupdate._describe_schedule([1, 2]))


class TestCommandLineIsSharedWithThePlist(HomeRedirected):
    """What `--install` prints must be what the job actually runs."""

    def _plist_command(self, greedy=False, cleanup=False):
        p = plistlib.loads(autoupdate._build_plist(BREW, 8, 0, greedy, cleanup))
        return p["ProgramArguments"][-1]

    def test_the_plist_uses_the_shared_builder(self):
        for greedy, cleanup in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(greedy=greedy, cleanup=cleanup):
                self.assertEqual(
                    self._plist_command(greedy, cleanup),
                    autoupdate._command_line(BREW, greedy, cleanup),
                )

    def test_greedy_attaches_to_upgrade_not_cleanup(self):
        self.assertEqual(
            autoupdate._command_line(BREW, greedy=True, cleanup=True),
            f"{BREW} update && {BREW} upgrade --greedy && {BREW} cleanup",
        )

    def test_install_prints_exactly_the_command_it_scheduled(self):
        # Regression risk: the printed line used to be reconstructed by hand
        # alongside the plist, so the two could drift apart silently.
        buf = io.StringIO()
        with mock.patch.object(autoupdate, "_resolve_brew", return_value=BREW), \
             mock.patch.object(autoupdate, "unload", return_value=0), \
             mock.patch("subprocess.run",
                        return_value=__import__("subprocess").CompletedProcess([], 0, "", "")), \
             contextlib.redirect_stdout(buf):
            self.assertEqual(autoupdate.install(7, 30, True, True), 0)
        printed = buf.getvalue()
        self.assertIn(autoupdate._command_line(BREW, True, True), printed)
        self.assertIn("at 07:30 every day.", printed)
        written = plistlib.loads(autoupdate._plist_path().read_bytes())
        self.assertIn(written["ProgramArguments"][-1], printed)


class TestAutoupdateUsesOrderedStreams(unittest.TestCase):
    def test_the_module_has_no_raw_print(self):
        # autoupdate writes to BOTH streams (status to stdout, errors to
        # stderr), so it has the same interleaving problem as the scan.
        import ast
        tree = ast.parse(Path(autoupdate.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotEqual(
                    node.func.id, "print",
                    "autoupdate must write through streams.out/err, not print()",
                )
