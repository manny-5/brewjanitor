"""Tests for piece 2 — brew check, and the Brew command wrapper.

The regression of record here is the scoped-search fix: `brew search` only emits
`==> Casks` section headers when stdout is a TTY, and brewjanitor always
captures through a pipe, so header-parsing classified every cask as a formula.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from brewjanitor import brewcheck
from brewjanitor.brewcheck import BREW_TIMEOUT, Brew, check
from brewjanitor.inventory import App
from tests.fakes import FakeBrew, cask_info


class ScriptedBrew(Brew):
    """A real Brew with _run replaced, so argv construction is under test."""

    def __init__(self, responses=None):
        super().__init__(brew_path="/fake/brew")
        self.calls: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self._responses = responses or {}

    def _run(self, args, timeout=None):
        self.calls.append(list(args))
        stdout = self._responses.get(tuple(args), "")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    def _run_streaming(self, args):
        # Mutating calls take the streaming path; record them the same way.
        self.streamed.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


class TestInstallArgv(unittest.TestCase):
    """`--adopt` is what makes replacing an app in /Applications possible."""

    def test_cask_install_with_adopt(self):
        b = ScriptedBrew()
        b.install("firefox", is_cask=True, adopt=True)
        self.assertEqual(b.streamed[0], ["install", "--adopt", "--cask", "firefox"])

    def test_cask_install_without_adopt(self):
        b = ScriptedBrew()
        b.install("firefox", is_cask=True, adopt=False)
        self.assertEqual(b.streamed[0], ["install", "--cask", "firefox"])

    def test_adopt_is_never_passed_for_a_formula(self):
        # brew rejects --adopt for formulae; it is a cask-artifact concept.
        b = ScriptedBrew()
        b.install("wget", is_cask=False, adopt=True)
        self.assertEqual(b.streamed[0], ["install", "wget"])

    def test_uninstall_argv(self):
        b = ScriptedBrew()
        b.uninstall("firefox", is_cask=True)
        b.uninstall("wget", is_cask=False)
        self.assertEqual(b.streamed, [["uninstall", "--cask", "firefox"], ["uninstall", "wget"]])


class TestScopedSearch(unittest.TestCase):
    def test_search_casks_uses_the_casks_scope(self):
        b = ScriptedBrew()
        b.search_casks("firefox")
        self.assertEqual(b.calls[0], ["search", "--casks", "firefox"])

    def test_search_formulae_uses_the_formula_scope(self):
        b = ScriptedBrew()
        b.search_formulae("wget")
        self.assertEqual(b.calls[0], ["search", "--formula", "wget"])

    def test_returns_bare_names_one_per_line(self):
        b = ScriptedBrew({("search", "--casks", "firefox"): "firefox\nfirefox@beta\n\nmultifirefox\n"})
        self.assertEqual(b.search_casks("firefox"), ["firefox", "firefox@beta", "multifirefox"])

    def test_no_header_parsing_headers_are_not_expected_or_stripped(self):
        # Regression: the old implementation depended on `==>` headers that brew
        # does not emit through a pipe. Scoped output has none, and any line is
        # taken at face value -- no ` (cask)` suffix games.
        b = ScriptedBrew({("search", "--casks", "slack"): "slack\n"})
        names = b.search_casks("slack")
        self.assertEqual(names, ["slack"])
        self.assertFalse(any("(cask)" in n for n in names))

    def test_cask_and_formula_scopes_are_cached_separately(self):
        # Regression risk: a cache keyed on the term alone would return cask
        # results for a formula query (and vice versa).
        b = ScriptedBrew({
            ("search", "--casks", "docker"): "docker\n",
            ("search", "--formula", "docker"): "docker-compose\n",
        })
        self.assertEqual(b.search_casks("docker"), ["docker"])
        self.assertEqual(b.search_formulae("docker"), ["docker-compose"])

    def test_results_are_cached_per_term(self):
        b = ScriptedBrew({("search", "--casks", "firefox"): "firefox\n"})
        b.search_casks("firefox")
        b.search_casks("firefox")
        self.assertEqual(len(b.calls), 1)

    def test_unavailable_brew_returns_empty(self):
        # An empty brew_path falls back to shutil.which, so "no brew" has to be
        # simulated at the lookup itself.
        with mock.patch.object(brewcheck.shutil, "which", return_value=None):
            b = Brew()
        self.assertFalse(b.available)
        self.assertEqual(b.search_casks("firefox"), [])
        self.assertEqual(b.search_formulae("firefox"), [])


class TestRunTimeout(unittest.TestCase):
    def test_timeout_becomes_a_nonzero_result_not_an_exception(self):
        b = Brew(brew_path="/fake/brew")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="brew", timeout=BREW_TIMEOUT)

        original = subprocess.run
        subprocess.run = boom
        try:
            result = b._run(["info", "firefox"], timeout=BREW_TIMEOUT)
        finally:
            subprocess.run = original
        self.assertEqual(result.returncode, 124)
        self.assertIn("timed out", result.stderr)


class TestCheck(unittest.TestCase):
    def test_unavailable_brew_reports_everything_unmanaged(self):
        apps = [App(name="A.app", path="/Applications/A.app", bundle_id="x")]
        results = check(apps, FakeBrew(available=False))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].brew_managed)
        self.assertEqual(results[0].brew_name, "")

    def test_app_at_a_brew_owned_path_is_managed(self):
        brew = FakeBrew(installed_casks=cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])["casks"])
        apps = [App(name="Firefox.app", path="/Applications/Firefox.app", bundle_id="org.mozilla.firefox")]
        result = check(apps, brew)[0]
        self.assertTrue(result.brew_managed)
        self.assertEqual(result.brew_name, "firefox")
        self.assertEqual(result.brew_kind, "cask")

    def test_unknown_app_is_unmanaged(self):
        brew = FakeBrew(installed_casks=cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])["casks"])
        apps = [App(name="Whatever.app", path="/Applications/Whatever.app", bundle_id="com.x.y")]
        self.assertFalse(check(apps, brew)[0].brew_managed)

    def test_result_order_matches_input_order(self):
        brew = FakeBrew()
        apps = [App(name=f"{i}.app", path=f"/Applications/{i}.app", bundle_id="") for i in range(5)]
        self.assertEqual([c.app.name for c in check(apps, brew)], [a.name for a in apps])

    def test_module_resolves_the_App_annotation_it_references(self):
        # `App` was used in annotations but never imported; only the deferred
        # evaluation of `from __future__ import annotations` hid the NameError.
        self.assertIs(brewcheck.App, App)


if __name__ == "__main__":
    unittest.main()


class TestBrewEnvironment(unittest.TestCase):
    """Read-only calls must not be able to trigger a surprise `brew update`."""

    def test_read_only_calls_disable_auto_update_and_hints(self):
        env = Brew(brew_path="/fake/brew")._env(read_only=True)
        self.assertEqual(env["HOMEBREW_NO_AUTO_UPDATE"], "1")
        self.assertEqual(env["HOMEBREW_NO_ENV_HINTS"], "1")

    def test_installs_keep_auto_update_but_still_silence_hints(self):
        # An install genuinely wants fresh cask metadata first.
        env = Brew(brew_path="/fake/brew")._env(read_only=False)
        self.assertNotIn("HOMEBREW_NO_AUTO_UPDATE", env)
        self.assertEqual(env["HOMEBREW_NO_ENV_HINTS"], "1")

    def test_the_ambient_environment_is_preserved(self):
        with mock.patch.dict("os.environ", {"BREWJANITOR_MARKER": "kept"}):
            self.assertEqual(Brew(brew_path="/fake/brew")._env(read_only=True)["BREWJANITOR_MARKER"], "kept")


class TestRunNeverRaises(unittest.TestCase):
    def test_a_missing_brew_binary_is_a_return_code_not_an_exception(self):
        # Previously only TimeoutExpired was caught, so an OSError from deep
        # inside a per-app loop took down the whole scan.
        result = Brew(brew_path="/nonexistent/path/to/brew")._run(["list"])
        self.assertEqual(result.returncode, 127)
        self.assertIn("could not run brew", result.stderr)

    def test_search_survives_a_broken_brew_binary(self):
        self.assertEqual(Brew(brew_path="/nonexistent/brew").search_casks("firefox"), [])

    def test_info_survives_a_broken_brew_binary(self):
        self.assertIsNone(Brew(brew_path="/nonexistent/brew").info_json("firefox", is_cask=True))


class TestTolerantJsonParsing(unittest.TestCase):
    def test_clean_json_parses(self):
        self.assertEqual(brewcheck._loads_json('{"casks": []}'), {"casks": []})

    def test_a_warning_prepended_to_stdout_does_not_lose_the_payload(self):
        # A pre-release OS is exactly where brew grows new output. Today the
        # warnings go to stderr, but this fails safe if that ever changes.
        noisy = 'Warning: You are using macOS 27.\n{"casks": [{"token": "firefox"}]}'
        parsed = brewcheck._loads_json(noisy)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["casks"][0]["token"], "firefox")

    def test_genuinely_unparseable_output_returns_none(self):
        self.assertIsNone(brewcheck._loads_json("not json at all"))
        self.assertIsNone(brewcheck._loads_json(""))
        self.assertIsNone(brewcheck._loads_json("Warning: something\n{broken"))


class TestPrereleaseDetection(unittest.TestCase):
    def _brew_answering(self, stdout, returncode=0):
        b = Brew(brew_path="/fake/brew")
        b._run = lambda args, timeout=None: subprocess.CompletedProcess(
            args=args, returncode=returncode, stdout=stdout, stderr="")
        return b

    def test_true_is_reported(self):
        self.assertIs(self._brew_answering("true\n").prerelease_macos(), True)

    def test_false_is_reported(self):
        self.assertIs(self._brew_answering("false\n").prerelease_macos(), False)

    def test_leading_brew_chatter_is_ignored(self):
        # `brew ruby` can print its own notices before the answer.
        self.assertIs(self._brew_answering("  brew developer off\n\ntrue\n").prerelease_macos(), True)

    def test_an_unusable_answer_is_unknown_not_false(self):
        # "could not tell" must stay distinguishable from "supported".
        self.assertIsNone(self._brew_answering("").prerelease_macos())
        self.assertIsNone(self._brew_answering("banana\n").prerelease_macos())
        self.assertIsNone(self._brew_answering("true\n", returncode=1).prerelease_macos())

    def test_unavailable_brew_is_unknown(self):
        with mock.patch.object(brewcheck.shutil, "which", return_value=None):
            self.assertIsNone(Brew().prerelease_macos())

    def test_the_answer_is_cached(self):
        calls = []
        b = Brew(brew_path="/fake/brew")
        def counted(args, timeout=None):
            calls.append(args)
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="true\n", stderr="")
        b._run = counted
        b.prerelease_macos()
        b.prerelease_macos()
        self.assertEqual(len(calls), 1)
