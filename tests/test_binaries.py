"""Tests for piece 7 — binaries: the read-only formula survey.

The filtering rules carry most of the weight here. Without them a raw listing
of PATH is overwhelmingly things that are already managed, and a report nobody
can read is a report nobody uses.
"""

from __future__ import annotations

import ast
import csv
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brewjanitor import binaries as mod
from brewjanitor.binaries import (
    PRODUCT_TREE_THRESHOLD,
    Binary,
    BinaryCandidate,
    binaries,
    classify,
    report,
    scan_bin_dir,
    survey,
    write_report,
)
from tests.fakes import FakeBrew

BREW = "/opt/homebrew"


def a_binary(name="ollama", path=None, resolved=None, skip_reason=""):
    path = path or f"/usr/local/bin/{name}"
    return Binary(name=name, path=path, resolved=resolved or path, skip_reason=skip_reason)


class TestClassify(unittest.TestCase):
    def test_a_plain_manual_install_is_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "mytool")
            Path(real).write_text("#!/bin/sh\n")
            self.assertEqual(classify(real, real, BREW), "")

    def test_homebrew_owned_files_are_excluded(self):
        self.assertEqual(
            classify("/usr/local/bin/wget", f"{BREW}/Cellar/wget/1.0/bin/wget", BREW),
            "already managed by Homebrew",
        )

    def test_a_prefix_lookalike_is_not_treated_as_homebrew(self):
        # "/opt/homebrew-extra" must not match the "/opt/homebrew" prefix.
        self.assertNotEqual(
            classify("/opt/homebrew-extra/bin/x", "/opt/homebrew-extra/bin/x", BREW),
            "already managed by Homebrew",
        )

    def test_macos_owned_files_are_excluded(self):
        for resolved in ("/usr/bin/python3", "/bin/ls", "/System/Library/x/tool"):
            with self.subTest(resolved=resolved):
                self.assertEqual(classify(resolved, resolved, BREW), "part of macOS")

    def test_shims_pointing_into_an_app_bundle_are_excluded(self):
        # Every entry in /usr/local/bin on the development machine was one of
        # these. Swapping an app's CLI helper for a formula breaks the app.
        self.assertEqual(
            classify("/usr/local/bin/ollama",
                     "/Applications/Ollama.app/Contents/Resources/ollama", BREW),
            "belongs to an installed application",
        )

    def test_shims_pointing_into_a_framework_are_excluded(self):
        self.assertEqual(
            classify("/usr/local/bin/R",
                     "/Library/Frameworks/R.framework/Resources/bin/R", BREW),
            "belongs to an installed application",
        )

    def test_version_manager_files_are_excluded(self):
        for resolved in (
            "/Users/x/.pyenv/versions/3.14.4/bin/python",
            "/Users/x/.nvm/versions/node/v20/bin/node",
            "/Users/x/.cargo/bin/rg",
            "/Users/x/miniconda3/bin/conda",
        ):
            with self.subTest(resolved=resolved):
                self.assertEqual(classify(resolved, resolved, BREW),
                                 "managed by a version manager")

    def test_broken_symlinks_are_excluded(self):
        self.assertEqual(
            classify("/usr/local/bin/gone", "/nonexistent/target/gone", BREW),
            "broken symlink",
        )

    def test_an_empty_brew_prefix_does_not_match_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "tool")
            Path(real).write_text("x")
            self.assertEqual(classify(real, real, ""), "")


class TestScanBinDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _exe(self, name):
        p = self.root / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_finds_executables(self):
        self._exe("mytool")
        found = scan_bin_dir(str(self.root), BREW)
        self.assertEqual([b.name for b in found], ["mytool"])
        self.assertEqual(found[0].skip_reason, "")

    def test_ignores_non_executable_files(self):
        (self.root / "readme.txt").write_text("hi")
        self.assertEqual(scan_bin_dir(str(self.root), BREW), [])

    def test_ignores_subdirectories(self):
        (self.root / "subdir").mkdir()
        self.assertEqual(scan_bin_dir(str(self.root), BREW), [])

    def test_missing_directory_yields_nothing(self):
        self.assertEqual(scan_bin_dir(str(self.root / "nope"), BREW), [])

    def test_symlinks_are_resolved_and_classified(self):
        target = self.root / "real_tool"
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
        link = self.root / "link_tool"
        link.symlink_to(target)
        found = {b.name: b for b in scan_bin_dir(str(self.root), BREW)}
        self.assertEqual(found["link_tool"].resolved, os.path.realpath(str(target)))


class TestProductTreeCollapsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _fill(self, count):
        for i in range(count):
            p = self.root / f"tool{i:03d}"
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

    def test_a_large_non_standard_directory_collapses_to_one_line(self):
        # A scientific suite with hundreds of binaries is one installed product,
        # not hundreds of independent replacement candidates.
        self._fill(PRODUCT_TREE_THRESHOLD + 5)
        found, collapsed = binaries((str(self.root),), BREW)
        self.assertEqual(found, [])
        self.assertEqual(collapsed, [(str(self.root), PRODUCT_TREE_THRESHOLD + 5)])

    def test_a_small_directory_is_listed_normally(self):
        self._fill(3)
        found, collapsed = binaries((str(self.root),), BREW)
        self.assertEqual(len(found), 3)
        self.assertEqual(collapsed, [])

    def test_standard_locations_are_never_collapsed(self):
        # /usr/local/bin is a real manual-install location; a busy one is still
        # a list of individual tools.
        self._fill(PRODUCT_TREE_THRESHOLD + 5)
        with mock.patch.object(mod, "DEFAULT_BIN_DIRS", (str(self.root),)):
            found, collapsed = binaries((str(self.root),), BREW)
        self.assertEqual(collapsed, [])
        self.assertEqual(len(found), PRODUCT_TREE_THRESHOLD + 5)


class TestSurvey(unittest.TestCase):
    def test_an_exact_formula_match_is_reported(self):
        brew = FakeBrew(formulae_by_term={"wget": ["wget"]})
        result = survey([a_binary("wget")], brew)[0]
        self.assertTrue(result.available)
        self.assertEqual(result.formula, "wget")

    def test_substring_matches_are_rejected(self):
        # `brew search --formula ollama` really does return gollama too.
        brew = FakeBrew(formulae_by_term={"ollama": ["gollama", "ollama"]})
        self.assertEqual(survey([a_binary("ollama")], brew)[0].formula, "ollama")

    def test_only_a_near_miss_means_no_match(self):
        brew = FakeBrew(formulae_by_term={"ollama": ["gollama", "ollama-cli"]})
        result = survey([a_binary("ollama")], brew)[0]
        self.assertFalse(result.available)
        self.assertEqual(result.formula, "")

    def test_filtered_binaries_are_never_searched(self):
        brew = FakeBrew(formulae_by_term={"ollama": ["ollama"]})
        skipped = a_binary("ollama", skip_reason="belongs to an installed application")
        result = survey([skipped], brew)[0]
        self.assertFalse(result.available)
        self.assertEqual(brew.search_calls, [])

    def test_unavailable_brew_reports_nothing(self):
        result = survey([a_binary("wget")], FakeBrew(available=False))[0]
        self.assertFalse(result.available)

    def test_result_order_matches_input_order(self):
        brew = FakeBrew()
        bins = [a_binary("a"), a_binary("b"), a_binary("c")]
        self.assertEqual([r.binary.name for r in survey(bins, brew)], ["a", "b", "c"])


class TestWriteReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_only_matching_tools_are_written(self):
        cands = [
            BinaryCandidate(binary=a_binary("wget"), formula="wget", available=True),
            BinaryCandidate(binary=a_binary("nope"), formula="", available=False),
        ]
        out = self.root / "r.csv"
        self.assertEqual(write_report(cands, out), 1)
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], mod.FIELDS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "wget")

    def test_creates_parent_directories(self):
        out = self.root / "a" / "b" / "r.csv"
        write_report([], out)
        self.assertTrue(out.exists())


class TestReportIsReadOnly(unittest.TestCase):
    """The central promise of this module, asserted structurally."""

    def test_the_module_cannot_install_or_delete(self):
        # A guard against a future change quietly giving this module teeth.
        tree = ast.parse(Path(mod.__file__).read_text())
        banned_attrs = {"install", "uninstall", "rmtree", "unlink", "remove", "rmdir"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(
                    node.func.attr, banned_attrs,
                    f"{mod.__name__} must never mutate; found call to .{node.func.attr}()",
                )

    def test_report_has_no_apply_style_parameter(self):
        import inspect
        params = set(inspect.signature(report).parameters)
        self.assertEqual(params & {"apply", "force", "remove", "install"}, set())

    def test_report_exits_nonzero_without_brew(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(report(FakeBrew(available=False)), 1)


if __name__ == "__main__":
    unittest.main()
