"""Tests for piece 3 — brew search: which unmanaged apps could Homebrew install.

Covers the classification fix (casks are found as casks) and the removal of the
formula fallback that produced false "installable" verdicts.
"""

from __future__ import annotations

import unittest

from brewjanitor.brewsearch import _search_term, search
from brewjanitor.inventory import App
from tests.fakes import FakeBrew, cask_info


def app(name, bundle_id="", where="/Applications"):
    return App(name=name, path=f"{where}/{name}", bundle_id=bundle_id)


class TestSearchTerm(unittest.TestCase):
    def test_strips_dot_app_and_lowercases(self):
        self.assertEqual(_search_term(app("Firefox.app")), "firefox")

    def test_hyphenates_spaces(self):
        self.assertEqual(_search_term(app("Microsoft Excel.app")), "microsoft-excel")
        self.assertEqual(_search_term(app("Visual Studio Code.app")), "visual-studio-code")

    def test_case_insensitive_suffix(self):
        self.assertEqual(_search_term(app("Thing.APP")), "thing")


class TestClassification(unittest.TestCase):
    def test_a_matching_cask_is_installable_and_typed_as_a_cask(self):
        # The core regression: this used to come back brew_kind="formula".
        brew = FakeBrew(casks_by_term={"firefox": ["firefox", "firefox@beta"]})
        result = search([app("Firefox.app")], brew)[0]
        self.assertTrue(result.installable)
        self.assertEqual(result.brew_kind, "cask")
        self.assertEqual(result.brew_name, "firefox")

    def test_search_queries_the_cask_scope(self):
        brew = FakeBrew(casks_by_term={"slack": ["slack"]})
        search([app("Slack.app")], brew)
        self.assertIn(("slack", "--casks"), brew.search_calls)

    def test_substring_matches_are_rejected_only_exact_names_win(self):
        # `brew search` matches substrings; "Firefox.app" must not adopt
        # "firefox@nightly" or "multifirefox".
        brew = FakeBrew(casks_by_term={"firefox": ["multifirefox", "firefox@nightly", "firefox"]})
        self.assertEqual(search([app("Firefox.app")], brew)[0].brew_name, "firefox")

    def test_no_exact_cask_means_not_installable(self):
        brew = FakeBrew(casks_by_term={"firefox": ["multifirefox", "firefox@nightly"]})
        result = search([app("Firefox.app")], brew)[0]
        self.assertFalse(result.installable)
        self.assertEqual(result.brew_name, "")

    def test_a_formula_name_match_is_not_installable(self):
        # Regression: "R.app" name-matched the `r` formula (the R language CLI),
        # which is not a replacement for the GUI app. Formulae never provide a
        # .app bundle, so they are not candidates at all.
        brew = FakeBrew(casks_by_term={"r": []}, formulae_by_term={"r": ["r"]})
        result = search([app("R.app")], brew)[0]
        self.assertFalse(result.installable)
        self.assertEqual(result.brew_kind, "")

    def test_installable_candidates_are_always_casks(self):
        brew = FakeBrew(casks_by_term={"a": ["a"], "b": ["b"]},
                        formulae_by_term={"a": ["a"], "b": ["b"]})
        results = search([app("A.app"), app("B.app")], brew)
        self.assertTrue(all(r.installable for r in results))
        self.assertEqual({r.brew_kind for r in results}, {"cask"})

    def test_unavailable_brew_reports_nothing_installable(self):
        results = search([app("Firefox.app"), app("Slack.app")], FakeBrew(available=False))
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.installable for r in results))

    def test_result_order_matches_input_order(self):
        brew = FakeBrew(casks_by_term={"a": ["a"]})
        apps = [app("A.app"), app("B.app"), app("C.app")]
        self.assertEqual([r.app.name for r in search(apps, brew)], [a.name for a in apps])


class TestVerification(unittest.TestCase):
    def test_dry_run_does_not_verify(self):
        brew = FakeBrew(casks_by_term={"firefox": ["firefox"]})
        result = search([app("Firefox.app", "org.mozilla.firefox")], brew, verify=False)[0]
        self.assertTrue(result.installable)
        self.assertFalse(result.verified)

    def test_verify_sets_the_flag_on_a_bundle_id_match(self):
        brew = FakeBrew(
            casks_by_term={"firefox": ["firefox"]},
            info_by_name={"firefox": cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])},
        )
        result = search([app("Firefox.app", "org.mozilla.firefox")], brew, verify=True)[0]
        self.assertTrue(result.verified)

    def test_verify_sets_the_flag_on_an_app_filename_match(self):
        brew = FakeBrew(
            casks_by_term={"firefox": ["firefox"]},
            info_by_name={"firefox": cask_info("firefox", "", ["Firefox.app"])},
        )
        result = search([app("Firefox.app", bundle_id="")], brew, verify=True)[0]
        self.assertTrue(result.verified)

    def test_verify_stays_false_when_the_cask_installs_a_different_app(self):
        brew = FakeBrew(
            casks_by_term={"firefox": ["firefox"]},
            info_by_name={"firefox": cask_info("firefox", "com.other.thing", ["Different.app"])},
        )
        result = search([app("Firefox.app", "org.mozilla.firefox")], brew, verify=True)[0]
        self.assertTrue(result.installable)   # still installable by name
        self.assertFalse(result.verified)     # but not confirmed

    def test_offline_suppresses_verification_even_when_asked(self):
        brew = FakeBrew(
            casks_by_term={"firefox": ["firefox"]},
            info_by_name={"firefox": cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])},
        )
        result = search([app("Firefox.app", "org.mozilla.firefox")], brew, verify=True, offline=True)[0]
        self.assertFalse(result.verified)


if __name__ == "__main__":
    unittest.main()
