"""Tests for piece 6 — the CLI wiring.

The pipeline stages are stubbed out: what is under test is argument parsing,
the dry-run/apply gate, report-file behaviour, and exit codes.
"""

from __future__ import annotations

import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brewjanitor import cli
from brewjanitor.brewsearch import AppCandidate
from brewjanitor.inventory import App
from brewjanitor.replace import ReplaceResult


def an_app(name="Thing.app"):
    return App(name=name, path=f"/Applications/{name}", bundle_id="com.example.thing")


class CliHarness(unittest.TestCase):
    """Stubs every stage so `run()` exercises only the CLI's own logic."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.apps = [an_app("Keep.app"), an_app("Drop.app")]
        self.candidates = [
            AppCandidate(app=self.apps[0], installable=True, brew_name="keep",
                         brew_kind="cask", verified=True),
            AppCandidate(app=self.apps[1], installable=False, brew_name="",
                         brew_kind="", verified=False),
        ]
        self.replace_results = [
            ReplaceResult(app=self.apps[0], brew_name="keep", brew_kind="cask",
                          status="dry-run", reason="would adopt"),
        ]
        self._patchers = [
            mock.patch.object(cli, "inventory", return_value=self.apps),
            mock.patch.object(cli, "check", side_effect=self._fake_check),
            mock.patch.object(cli, "search", return_value=self.candidates),
            mock.patch.object(cli, "replace", side_effect=self._fake_replace),
            mock.patch.object(cli, "Brew", return_value=mock.Mock(available=True)),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _fake_check(self, apps, brew):
        from brewjanitor.brewcheck import CheckedApp
        return [CheckedApp(app=a, brew_managed=False, brew_name="", brew_kind="") for a in apps]

    def _fake_replace(self, installable, brew, apply=False):
        return self.replace_results

    def _run(self, **kw):
        opts = dict(apply=False, report_path=None)
        opts.update(kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli.run(**opts)
        return code, buf.getvalue()


class TestReportFileBehaviour(CliHarness):
    def test_a_plain_dry_run_writes_no_file(self):
        before = set(Path.cwd().iterdir())
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertEqual(set(Path.cwd().iterdir()), before)
        self.assertIn("could not be replaced", out)

    def test_an_explicit_report_path_is_written_even_in_a_dry_run(self):
        target = self.root / "unreplaced.csv"
        code, _ = self._run(report_path=str(target))
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        self.assertIn("Drop.app", target.read_text())

    def test_the_report_lists_only_the_not_installable_apps(self):
        target = self.root / "r.csv"
        self._run(report_path=str(target))
        body = target.read_text()
        self.assertIn("Drop.app", body)
        self.assertNotIn("Keep.app", body)


class TestExitCodes(CliHarness):
    def test_a_clean_dry_run_exits_zero(self):
        self.assertEqual(self._run()[0], 0)

    def test_a_failed_replace_under_apply_exits_one(self):
        self.replace_results = [
            ReplaceResult(app=self.apps[0], brew_name="keep", brew_kind="cask",
                          status="failed", reason="install failed"),
        ]
        code, _ = self._run(apply=True, report_path=str(self.root / "r.csv"))
        self.assertEqual(code, 1)

    def test_a_failure_in_a_dry_run_does_not_fail_the_process(self):
        self.replace_results = [
            ReplaceResult(app=self.apps[0], brew_name="keep", brew_kind="cask",
                          status="failed", reason="install failed"),
        ]
        self.assertEqual(self._run(apply=False)[0], 0)

    def test_successful_apply_exits_zero(self):
        self.replace_results = [
            ReplaceResult(app=self.apps[0], brew_name="keep", brew_kind="cask",
                          status="replaced", reason="adopted in place"),
        ]
        code, _ = self._run(apply=True, report_path=str(self.root / "r.csv"))
        self.assertEqual(code, 0)


class TestArgumentParsing(unittest.TestCase):
    def test_defaults_are_safe(self):
        args = cli._build_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.reconcile)
        self.assertIsNone(args.report)

    def test_flags_parse(self):
        args = cli._build_parser().parse_args(["--apply", "--offline", "--verbose", "--report", "x.csv"])
        self.assertTrue(args.apply and args.offline and args.verbose)
        self.assertEqual(args.report, "x.csv")

    def test_autoupdate_subcommand_is_recognised(self):
        args = cli._build_parser().parse_args(["autoupdate", "--install", "--hour", "7"])
        self.assertEqual(args.command, "autoupdate")
        self.assertTrue(args.install)
        self.assertEqual(args.hour, 7)

    def test_autoupdate_help_path_does_not_crash(self):
        # Regression: this route ran into the missing `argparse` import.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["autoupdate"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("autoupdate", buf.getvalue())

    def test_autoupdate_hour_validation_returns_one(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(cli.main(["autoupdate", "--install", "--hour", "99"]), 1)


class TestReconcileRouting(unittest.TestCase):
    def test_reconcile_bypasses_the_scan_and_never_installs(self):
        fake_brew = mock.Mock(available=True)
        with mock.patch.object(cli, "Brew", return_value=fake_brew), \
             mock.patch.object(cli, "inventory", return_value=[an_app()]), \
             mock.patch("brewjanitor.replace.reconcile", return_value=[]) as rec, \
             mock.patch.object(cli, "search") as searched, \
             mock.patch.object(cli, "replace") as replaced:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = cli.main(["--reconcile"])
        self.assertEqual(code, 0)
        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs.get("apply"), False)
        searched.assert_not_called()
        replaced.assert_not_called()

    def test_reconcile_without_brew_exits_one(self):
        with mock.patch.object(cli, "Brew", return_value=mock.Mock(available=False)):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.assertEqual(cli.main(["--reconcile"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestPrereleaseNotice(unittest.TestCase):
    def _notice(self, prerelease, apply=False):
        brew = mock.Mock()
        brew.prerelease_macos.return_value = prerelease
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            cli._note_prerelease_macos(brew, apply)
        return buf.getvalue()

    def test_a_prerelease_macos_is_reported_once(self):
        out = self._notice(True)
        self.assertIn("pre-release", out)
        self.assertIn("does not affect brewjanitor", out)

    def test_the_notice_says_why_casks_are_unaffected(self):
        # The point of the notice is to stop the warning being alarming: the
        # part a pre-release OS breaks (formulae) is the part we never touch.
        out = self._notice(True)
        self.assertIn("cask", out.lower())
        self.assertIn("formulae", out.lower())

    def test_apply_adds_a_line_about_brews_own_warning(self):
        self.assertIn("during --apply", self._notice(True, apply=True))

    def test_a_supported_macos_prints_nothing(self):
        self.assertEqual(self._notice(False), "")

    def test_an_unknown_answer_prints_nothing(self):
        # Never guess out loud: unknown is not the same as unsupported.
        self.assertEqual(self._notice(None), "")


class TestFormulaeSubcommand(unittest.TestCase):
    def test_it_parses_with_its_own_flags(self):
        args = cli._build_parser().parse_args(["formulae", "--all", "--report", "f.csv", "--verbose"])
        self.assertEqual(args.command, "formulae")
        self.assertTrue(args.all and args.verbose)
        self.assertEqual(args.report, "f.csv")

    def test_defaults_are_narrow(self):
        args = cli._build_parser().parse_args(["formulae"])
        self.assertFalse(args.all)
        self.assertIsNone(args.report)

    def test_it_exposes_no_apply_flag(self):
        # The read-only promise, enforced at the CLI boundary too.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli._build_parser().parse_args(["formulae", "--apply"])

    def test_it_routes_to_the_read_only_report(self):
        with mock.patch.object(cli, "Brew", return_value=mock.Mock(available=True)), \
             mock.patch("brewjanitor.binaries.report", return_value=0) as rep:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                code = cli.main(["formulae", "--all"])
        self.assertEqual(code, 0)
        rep.assert_called_once()
        self.assertIs(rep.call_args.kwargs["all_path"], True)

    def test_it_never_reaches_the_replace_pipeline(self):
        with mock.patch.object(cli, "Brew", return_value=mock.Mock(available=True)), \
             mock.patch("brewjanitor.binaries.report", return_value=0), \
             mock.patch.object(cli, "replace") as replaced, \
             mock.patch.object(cli, "inventory") as inv:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                cli.main(["formulae"])
        replaced.assert_not_called()
        inv.assert_not_called()
