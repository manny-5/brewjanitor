"""Tests for piece 4 — replace and reconcile: the only code that deletes.

Two invariants matter more than anything else here and are asserted repeatedly:

  1. Nothing is removed unless an install AND a verification both succeeded.
  2. Nothing outside an allowed scan root is ever a removal candidate.

Every test uses a RecordingRemover, so a regression shows up as an unexpected
call rather than as a deleted application.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from brewjanitor.brewsearch import AppCandidate
from brewjanitor.inventory import App
from brewjanitor.replace import (
    _install_failure_reason,
    _is_safe_to_remove,
    _remove_bundle,
    reconcile,
    replace,
)
from tests.fakes import FakeBrew, RecordingRemover, cask_info, fail, ok

ROOTS = ("/Applications", "/Users/tester/Applications")


def app(name="Firefox.app", bundle_id="org.mozilla.firefox", where="/Applications"):
    return App(name=name, path=f"{where}/{name}", bundle_id=bundle_id)


def candidate(a=None, installable=True, brew_name="firefox", kind="cask", verified=True):
    return AppCandidate(
        app=a or app(), installable=installable, brew_name=brew_name,
        brew_kind=kind, verified=verified,
    )


class TestSafeToRemove(unittest.TestCase):
    def test_a_bundle_inside_a_root_is_removable(self):
        self.assertTrue(_is_safe_to_remove("/Applications/Firefox.app", ROOTS))
        self.assertTrue(_is_safe_to_remove("/Users/tester/Applications/Firefox.app", ROOTS))

    def test_the_scan_root_itself_is_never_removable(self):
        # Regression: Path.relative_to succeeds for equal paths, so the guard
        # used to approve deleting /Applications outright.
        self.assertFalse(_is_safe_to_remove("/Applications", ROOTS))
        self.assertFalse(_is_safe_to_remove("/Applications/", ROOTS))
        self.assertFalse(_is_safe_to_remove("/Applications/..", ROOTS))

    def test_a_root_that_is_itself_a_bundle_is_still_not_removable(self):
        # Isolates the equality branch specifically. The .app suffix rule
        # already rejects "/Applications", so only a root that IS a bundle
        # proves the strict-descendant check is doing its own work.
        roots = ("/Applications/Sandbox.app",)
        self.assertFalse(_is_safe_to_remove("/Applications/Sandbox.app", roots))
        self.assertTrue(_is_safe_to_remove("/Applications/Sandbox.app/Nested.app", roots))

    def test_paths_outside_every_root_are_refused(self):
        for path in ("/etc/passwd.app", "/System/Applications/Safari.app",
                     "/Users/tester/Desktop/Thing.app", "/"):
            with self.subTest(path=path):
                self.assertFalse(_is_safe_to_remove(path, ROOTS))

    def test_traversal_out_of_a_root_is_refused(self):
        self.assertFalse(_is_safe_to_remove("/Applications/../etc/evil.app", ROOTS))
        self.assertFalse(_is_safe_to_remove("/Applications/../../Thing.app", ROOTS))

    def test_non_app_paths_are_refused(self):
        for path in ("/Applications/notes.txt", "/Applications/Utilities",
                     "/Applications/Firefox.app.bak"):
            with self.subTest(path=path):
                self.assertFalse(_is_safe_to_remove(path, ROOTS))

    def test_empty_root_list_refuses_everything(self):
        self.assertFalse(_is_safe_to_remove("/Applications/Firefox.app", ()))


class TestRemoveBundle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_removes_a_real_directory(self):
        bundle = self.root / "Thing.app"
        (bundle / "Contents").mkdir(parents=True)
        removed, why = _remove_bundle(str(bundle))
        self.assertTrue(removed, why)
        self.assertFalse(bundle.exists())

    def test_refuses_a_symlinked_bundle_and_leaves_both_sides_intact(self):
        real = self.root / "Real.app"
        (real / "Contents").mkdir(parents=True)
        link = self.root / "Link.app"
        link.symlink_to(real)
        removed, why = _remove_bundle(str(link))
        self.assertFalse(removed)
        self.assertIn("symlink", why)
        self.assertTrue(real.exists())
        self.assertTrue(link.is_symlink())

    def test_missing_path_is_a_clean_failure(self):
        removed, why = _remove_bundle(str(self.root / "Ghost.app"))
        self.assertFalse(removed)
        self.assertIn("not found", why)

    def test_a_file_is_not_treated_as_a_bundle(self):
        f = self.root / "File.app"
        f.write_text("x")
        removed, why = _remove_bundle(str(f))
        self.assertFalse(removed)
        self.assertIn("not a directory", why)


class TestReplaceDryRun(unittest.TestCase):
    def test_dry_run_installs_nothing_and_removes_nothing(self):
        brew, remover = FakeBrew(), RecordingRemover()
        results = replace([candidate()], brew, apply=False, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "dry-run")
        self.assertEqual(brew.install_calls, [])
        self.assertEqual(remover.calls, [])

    def test_dry_run_plan_mentions_adoption_rather_than_deletion(self):
        results = replace([candidate()], FakeBrew(), apply=False, allowed_roots=ROOTS)
        self.assertIn("--adopt", results[0].reason)

    def test_non_installable_candidates_are_skipped(self):
        remover = RecordingRemover()
        results = replace([candidate(installable=False, brew_name="")], FakeBrew(),
                          apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(remover.calls, [])

    def test_unavailable_brew_skips_without_touching_anything(self):
        remover = RecordingRemover()
        results = replace([candidate()], FakeBrew(available=False),
                          apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(remover.calls, [])


class TestReplaceApply(unittest.TestCase):
    def _brew(self, install_result=None, artifacts=("Firefox.app",), bundle_id="org.mozilla.firefox"):
        return FakeBrew(
            info_by_name={"firefox": cask_info("firefox", bundle_id, list(artifacts))},
            install_result=install_result or ok(),
        )

    def test_install_requests_adoption(self):
        # Regression: without --adopt, brew refuses to overwrite the very app
        # brewjanitor targets, so --apply could never succeed.
        brew = self._brew()
        replace([candidate()], brew, apply=True, allowed_roots=ROOTS, remover=RecordingRemover())
        self.assertEqual(brew.install_calls, [("firefox", True, True)])

    def test_adopted_in_place_removes_nothing(self):
        # The cask owns /Applications/Firefox.app, which IS the original bundle.
        brew, remover = self._brew(), RecordingRemover()
        results = replace([candidate()], brew, apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "replaced")
        self.assertIn("adopted in place", results[0].reason)
        self.assertEqual(remover.calls, [])

    def test_a_separate_copy_elsewhere_is_removed_as_a_leftover(self):
        # Original in ~/Applications; the cask installs to /Applications, so the
        # original really is a leftover second copy.
        original = app(where="/Users/tester/Applications")
        brew, remover = self._brew(), RecordingRemover()
        results = replace([candidate(a=original)], brew, apply=True,
                          allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "replaced")
        self.assertEqual(remover.calls, [original.path])

    def test_install_failure_removes_nothing(self):
        brew, remover = self._brew(install_result=fail("network is down")), RecordingRemover()
        results = replace([candidate()], brew, apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(remover.calls, [])

    def test_verify_failure_removes_nothing(self):
        # brew reported success but installed something that is not our app.
        brew = self._brew(artifacts=("SomethingElse.app",), bundle_id="com.other.thing")
        remover = RecordingRemover()
        results = replace([candidate()], brew, apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("verify failed", results[0].reason)
        self.assertEqual(remover.calls, [])

    def test_missing_brew_info_fails_verification(self):
        brew, remover = FakeBrew(info_by_name={}), RecordingRemover()
        results = replace([candidate()], brew, apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(remover.calls, [])

    def test_a_formula_candidate_is_never_replaced(self):
        brew, remover = self._brew(), RecordingRemover()
        results = replace([candidate(kind="formula", brew_name="firefox")], brew,
                          apply=True, allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("formula", results[0].reason)
        self.assertEqual(remover.calls, [])

    def test_a_leftover_outside_the_allowed_roots_is_refused(self):
        original = app(where="/Users/tester/Desktop")
        brew, remover = self._brew(), RecordingRemover()
        results = replace([candidate(a=original)], brew, apply=True,
                          allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("refusing to remove", results[0].reason)
        self.assertEqual(remover.calls, [])

    def test_a_failed_removal_is_reported_not_swallowed(self):
        original = app(where="/Users/tester/Applications")
        brew = self._brew()
        remover = RecordingRemover(succeed=False, reason="permission denied")
        results = replace([candidate(a=original)], brew, apply=True,
                          allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertIn("permission denied", results[0].reason)


class TestInstallFailureReason(unittest.TestCase):
    def test_adopt_version_mismatch_gets_an_actionable_message(self):
        reason = _install_failure_reason(
            "Error: It seems the existing App is different from the one being installed."
        )
        self.assertIn("cannot adopt", reason)
        self.assertIn("Update the app", reason)

    def test_other_failures_are_passed_through(self):
        self.assertIn("network is down", _install_failure_reason("network is down"))

    def test_long_stderr_is_truncated(self):
        self.assertLessEqual(len(_install_failure_reason("x" * 5000)), 240)


class TestReconcile(unittest.TestCase):
    def _brew(self, token="firefox", bundle_id="org.mozilla.firefox", artifacts=("Firefox.app",)):
        return FakeBrew(installed_casks=cask_info(token, bundle_id, list(artifacts))["casks"])

    def test_a_leftover_with_a_matching_bundle_id_is_an_orphan(self):
        leftover = app(where="/Users/tester/Applications")
        remover = RecordingRemover()
        results = reconcile([leftover], self._brew(), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "replaced")
        self.assertEqual(remover.calls, [leftover.path])

    def test_a_shared_filename_alone_is_not_an_orphan(self):
        # Regression, and the one that could destroy data: a separate copy that
        # merely shares a .app name (a beta, a pinned old build) was declared an
        # orphan and deleted. check() calls that same app "managed, leave
        # alone" -- the two halves of the tool disagreed.
        beta = app(name="Firefox.app", bundle_id="org.mozilla.firefox-beta",
                   where="/Users/tester/Applications")
        remover = RecordingRemover()
        results = reconcile([beta], self._brew(), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(remover.calls, [])

    def test_an_app_without_a_bundle_id_is_never_an_orphan(self):
        anon = app(name="Firefox.app", bundle_id="", where="/Users/tester/Applications")
        remover = RecordingRemover()
        results = reconcile([anon], self._brew(), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(remover.calls, [])

    def test_an_app_brew_already_owns_by_path_is_not_an_orphan(self):
        managed = app(where="/Applications")  # the cask's own install location
        remover = RecordingRemover()
        results = reconcile([managed], self._brew(), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertIn("not an orphan", results[0].reason)
        self.assertEqual(remover.calls, [])

    def test_dry_run_reports_without_removing(self):
        leftover = app(where="/Users/tester/Applications")
        remover = RecordingRemover()
        results = reconcile([leftover], self._brew(), apply=False,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "dry-run")
        self.assertEqual(remover.calls, [])

    def test_an_orphan_outside_the_allowed_roots_is_refused(self):
        stray = app(where="/Users/tester/Desktop")
        remover = RecordingRemover()
        results = reconcile([stray], self._brew(), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(remover.calls, [])

    def test_unavailable_brew_removes_nothing(self):
        remover = RecordingRemover()
        results = reconcile([app()], FakeBrew(available=False), apply=True,
                            allowed_roots=ROOTS, remover=remover)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(remover.calls, [])


if __name__ == "__main__":
    unittest.main()
