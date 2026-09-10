"""Tests for piece 3 — brew search: which unmanaged apps could Homebrew install.

Covers the classification fix (casks are found as casks) and the removal of the
formula fallback that produced false "installable" verdicts.
"""

from __future__ import annotations

import unittest

from brewjanitor.brewsearch import _cask_bundle_ids, _cask_is_pkg, _search_term, search
from brewjanitor.inventory import App
from tests.fakes import FakeBrew, cask_info, cask_info_pkg


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


class TestPkgCaskIdentity(unittest.TestCase):
    """A pkg cask (Malwarebytes, Microsoft Office, NordVPN) has no .app artifact.

    Its identity lives in the uninstall/zap directives, so verify must match on
    those bundle ids instead of an .app path.
    """

    def test_an_app_cask_is_not_a_pkg_cask(self):
        self.assertFalse(_cask_is_pkg(cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])["casks"][0]))

    def test_a_pkg_cask_is_detected_by_its_pkg_artifact(self):
        self.assertTrue(_cask_is_pkg(cask_info_pkg("microsoft-excel", ["com.microsoft.Excel"])["casks"][0]))

    def test_bundle_ids_come_from_quit_directives(self):
        cask = cask_info_pkg("microsoft-excel", ["com.microsoft.Excel", "com.microsoft.autoupdate2"])["casks"][0]
        self.assertEqual(set(_cask_bundle_ids(cask)), {"com.microsoft.Excel", "com.microsoft.autoupdate2"})

    def test_bundle_ids_come_from_a_string_quit(self):
        # brew allows `quit:` as a single string, not just a list.
        info = cask_info_pkg("malwarebytes", uninstall=[{"quit": "com.malwarebytes.mbam.frontend.agent"}])
        self.assertIn("com.malwarebytes.mbam.frontend.agent", _cask_bundle_ids(info["casks"][0]))

    def test_bundle_ids_come_from_pkgutil_directives(self):
        info = cask_info_pkg("thing", uninstall=[{"pkgutil": ["com.example.thing", "com.example.helper"]}])
        self.assertEqual(set(_cask_bundle_ids(info["casks"][0])), {"com.example.thing", "com.example.helper"})

    def test_bundle_ids_come_from_login_item(self):
        info = cask_info_pkg("nordvpn", uninstall=[{"login_item": "NordVPN"}])
        self.assertIn("NordVPN", _cask_bundle_ids(info["casks"][0]))

    def test_bundle_ids_come_from_trash_leaf_paths(self):
        info = cask_info_pkg(
            "thing",
            uninstall=[{"quit": "com.example.thing"}],
            zap=[{"trash": ["~/Library/Application Support/com.example.thing"]}],
        )
        ids = _cask_bundle_ids(info["casks"][0])
        self.assertIn("com.example.thing", ids)
        self.assertIn("com.example.thing", ids)  # from both quit and trash, de-duped

    def test_bundle_ids_are_de_duplicated(self):
        info = cask_info_pkg(
            "thing",
            uninstall=[{"quit": "com.example.thing", "pkgutil": "com.example.thing"}],
        )
        self.assertEqual(_cask_bundle_ids(info["casks"][0]).count("com.example.thing"), 1)

    def test_a_cask_with_no_identity_returns_no_ids(self):
        info = cask_info_pkg("anon", bundle_ids=(), uninstall=[{}])
        self.assertEqual(_cask_bundle_ids(info["casks"][0]), [])

    def test_an_app_cask_reports_its_bundle_identifier(self):
        # An app cask's primary id is bundle_identifier; uninstall directives
        # are additive, not a replacement for it.
        cask = cask_info("firefox", "org.mozilla.firefox", ["Firefox.app"])["casks"][0]
        self.assertEqual(_cask_bundle_ids(cask), ["org.mozilla.firefox"])


class TestPkgCaskVerification(unittest.TestCase):
    def test_a_pkg_cask_verifies_on_bundle_id_from_quit(self):
        # The user's app reports CFBundleIdentifier com.microsoft.Excel; the
        # cask names that id in its `quit` directive. With no .app artifact to
        # match a path, bundle id is the only signal -- and it is enough.
        brew = FakeBrew(
            casks_by_term={"microsoft-excel": ["microsoft-excel"]},
            info_by_name={"microsoft-excel": cask_info_pkg("microsoft-excel", ["com.microsoft.Excel"])},
        )
        result = search([app("Microsoft Excel.app", "com.microsoft.Excel")], brew, verify=True)[0]
        self.assertTrue(result.installable)
        self.assertTrue(result.verified)

    def test_a_pkg_cask_with_a_mismatched_bundle_id_is_not_verified(self):
        # The app's bundle id is not among the cask's uninstall identities, so
        # we cannot confirm this cask manages this app. Stay not-verified
        # (fail closed) rather than guessing.
        brew = FakeBrew(
            casks_by_term={"microsoft-excel": ["microsoft-excel"]},
            info_by_name={"microsoft-excel": cask_info_pkg("microsoft-excel", ["com.microsoft.Excel"])},
        )
        result = search([app("Microsoft Excel.app", "com.totally.different")], brew, verify=True)[0]
        self.assertTrue(result.installable)
        self.assertFalse(result.verified)

    def test_a_pkg_cask_with_no_app_bundle_id_cannot_verify(self):
        # An app with no CFBundleIdentifier has nothing to match against a pkg
        # cask's identity, so verification cannot succeed. (A dry run still
        # marks it installable by name; only the verify step fails.)
        brew = FakeBrew(
            casks_by_term={"malwarebytes": ["malwarebytes"]},
            info_by_name={"malwarebytes": cask_info_pkg("malwarebytes", ["com.malwarebytes.mbam.frontend.agent"])},
        )
        result = search([app("Malwarebytes.app", "")], brew, verify=True)[0]
        self.assertTrue(result.installable)
        self.assertFalse(result.verified)


if __name__ == "__main__":
    unittest.main()
