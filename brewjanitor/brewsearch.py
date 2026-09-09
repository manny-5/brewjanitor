"""Piece 3 — brew search: which unmanaged apps *could* be installed via Homebrew.

Given the labelled list from `brewcheck.check()` (or the unmanaged subset),
decide for each *unmanaged* app whether Homebrew knows how to install it.

Strategy
--------
For every unmanaged App:
  1. Derive a search term from the bundle name by stripping the ".app" suffix
     (e.g. "Firefox.app" -> "firefox"). This is the most reliable, user-facing
     token Homebrew indexes casks by.
  2. `brew search <term>` returns candidate formula/cask names.
  3. For each candidate, `brew info --json=v2 <name>` (read-only) confirms it is
     real and, for casks, reads the .app bundle path(s) and bundle id(s) it would
     install. We prefer a cask whose artifact app path or bundle id matches the
     discovered app; failing that we accept the first resolvable cask candidate
     as "installable but unverified".
  4. Result: an `AppCandidate` per app — installable=True/False, the chosen brew
     name/kind, and a `verified` flag saying whether we matched on path or
     bundle id (high confidence) vs. just found a same-named cask.

Everything here is read-only (search + info only). No install happens yet; that
is piece 4, and only under `--apply`. When `brew` is unavailable, every app is
reported not-installable so later stages skip it.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

from .brewcheck import Brew
from .inventory import App


def _is_cask_candidate(raw: str) -> bool:
    """True if a brew search line is a cask (suffixed ` (cask)`)."""
    return raw.strip().endswith("(cask)")


@dataclasses.dataclass(frozen=True)
class AppCandidate:
    """An app plus whether Homebrew could install it.

    Attributes:
        app: the discovered (unmanaged) App.
        installable: True if brew search + info found a usable formula/cask.
        brew_name: the formula/cask name to install, or "" if not installable.
        brew_kind: "formula" | "cask" | "".
        verified: True if we matched the candidate to the app by .app path or
            bundle id (high confidence). False means we only found a same-named
            candidate without a positive match.
    """

    app: App
    installable: bool
    brew_name: str
    brew_kind: str
    verified: bool


_TERM_SUFFIX = re.compile(r"\.app$", re.IGNORECASE)
_PARENS_CASK = re.compile(r"\s*\(cask\)\s*$")


def _search_term(app: App) -> str:
    """Turn "Firefox.app" -> "firefox", "Firefox ESR.app" -> "firefox-esr".

    Homebrew cask names are lowercased and hyphenated, so we strip the .app
    suffix, collapse whitespace into hyphens, and lowercase. This keeps the term
    compatible with `brew search`, which matches the term against cask names."""
    name = _TERM_SUFFIX.sub("", app.name)
    name = _PARENS_CASK.sub("", name)
    name = re.sub(r"\s+", "-", name.strip())
    return name.lower()


def _clean_candidate(raw: str) -> str:
    """Strip the trailing ` (cask)` that brew search appends to cask names."""
    return _PARENS_CASK.sub("", raw).strip()


def _cask_app_artifacts(info: dict) -> list[tuple[str, str]]:
    """Return (app_path, bundle_id) pairs a cask would install, from its JSON.

    Cask JSON: casks[].artifacts[].app[] (paths) and casks[].bundle_identifier.
    The bundle id is the same for all app artifacts of a cask, so we pair it
    with each path. Missing bundle id -> "".
    """
    out: list[tuple[str, str]] = []
    for cask in info.get("casks", []):
        bid = cask.get("bundle_identifier", "") or ""
        if not isinstance(bid, str):
            bid = ""
        for artifact in cask.get("artifacts", []) or []:
            if isinstance(artifact, dict):
                apps = artifact.get("app", [])
            elif isinstance(artifact, list):
                apps = artifact
            else:
                apps = []
            for item in apps:
                if isinstance(item, str) and item.endswith(".app"):
                    out.append((item, bid))
    return out


def _app_name_lower(app: App) -> str:
    """Lowercased, hyphenated bundle name without .app, mirroring cask naming."""
    name = _TERM_SUFFIX.sub("", app.name)
    name = re.sub(r"\s+", "-", name.strip())
    return name.lower()


def _evaluate(
    app: App, candidates: list[str], brew: Brew
) -> AppCandidate:
    """Pick the best brew candidate for one app from its search results.

    Prefers casks (apps install via casks), and among casks prefers one that
    matches the app by bundle id or by the .app artifact filename. Falls back
    to any resolvable cask candidate (verified=False), then any formula
    candidate (apps rarely install from formulae, so formula matches are only
    accepted as a last resort and marked unverified).
    """
    term = _app_name_lower(app)
    fallback_cask: tuple[str, bool] | None = None
    fallback_formula: tuple[str, bool] | None = None

    for raw in candidates:
        name = _clean_candidate(raw)
        if not name:
            continue
        info = brew.info_json_any(name)
        if not info:
            continue

        # Cask path: check artifacts for a path/bundle_id match against the app.
        if info.get("casks"):
            for app_path, bid in _cask_app_artifacts(info):
                art_name = Path(app_path).name
                if app.bundle_id and bid and bid == app.bundle_id:
                    return AppCandidate(
                        app=app,
                        installable=True,
                        brew_name=name,
                        brew_kind="cask",
                        verified=True,
                    )
                if art_name.lower() == app.name.lower():
                    return AppCandidate(
                        app=app,
                        installable=True,
                        brew_name=name,
                        brew_kind="cask",
                        verified=True,
                    )
            if fallback_cask is None and name == term:
                fallback_cask = (name, False)

        # Formula path: only accept as a low-confidence last resort.
        if info.get("formulae") and fallback_formula is None and name == term:
            fallback_formula = (name, False)

    if fallback_cask is not None:
        name, verified = fallback_cask
        return AppCandidate(
            app=app, installable=True, brew_name=name, brew_kind="cask", verified=verified
        )
    if fallback_formula is not None:
        name, verified = fallback_formula
        return AppCandidate(
            app=app,
            installable=True,
            brew_name=name,
            brew_kind="formula",
            verified=verified,
        )

    return AppCandidate(app=app, installable=False, brew_name="", brew_kind="", verified=False)


def _offline_evaluate(app: App, candidates: list[str]) -> AppCandidate:
    """Fast path: pick a cask by name only, with no `brew info` network call.

    We trust a same-named cask candidate (e.g. app "Firefox" -> candidate
    "firefox (cask)") without verifying its artifacts. This is much faster (no
    per-candidate brew info) but always unverified, so piece 4 will be cautious.
    """
    term = _app_name_lower(app)
    for raw in candidates:
        if not _is_cask_candidate(raw):
            continue
        name = _clean_candidate(raw)
        if name == term:
            return AppCandidate(
                app=app, installable=True, brew_name=name, brew_kind="cask", verified=False
            )
    return AppCandidate(app=app, installable=False, brew_name="", brew_kind="", verified=False)


def search(
    apps: list[App],
    brew: Brew | None = None,
    offline: bool = False,
    progress: bool = False,
) -> list[AppCandidate]:
    """For each App, decide whether Homebrew could install it.

    Args:
        apps: apps to evaluate. Typically the unmanaged subset from the brew
            check, but any App list is accepted.
        brew: optional Brew wrapper (e.g. a test fake). Defaults to a real Brew
            discovered on PATH.
        offline: when True, skip the per-candidate `brew info` network calls and
            match casks by name only (much faster, but every result is
            `verified=False`). Default False uses the full verified path.
        progress: when True, print one line per app to stderr as it is processed,
            so a long scan shows it is moving instead of appearing stuck.

    Returns:
        One AppCandidate per input App, in the same order. Always returns; never
        raises. When brew is unavailable, every App is reported not-installable.
    """
    brew = brew if brew is not None else Brew()
    if not brew.available:
        return [
            AppCandidate(app=a, installable=False, brew_name="", brew_kind="", verified=False)
            for a in apps
        ]

    total = len(apps)
    results: list[AppCandidate] = []
    for index, app in enumerate(apps, start=1):
        if progress:
            print(f"searching [{index}/{total}] {app.name} ...", file=sys.stderr, flush=True)
        candidates = brew.search(_search_term(app))
        if offline:
            results.append(_offline_evaluate(app, candidates))
        else:
            results.append(_evaluate(app, candidates, brew))
    return results


def main() -> int:
    """Entry point for `python -m brewjanitor.brewsearch`.

    Prints one tab-separated line per app:
        installable<TAB>kind<TAB>brew_name<TAB>verified<TAB>name<TAB>path<TAB>bundle_id
    """
    from .brewcheck import check
    from .inventory import inventory

    brew = Brew()
    if not brew.available:
        print("brew not found on PATH; nothing is installable.", file=sys.stderr)
    unmanaged = [c.app for c in check(inventory(), brew) if not c.brew_managed]
    for cand in search(unmanaged, brew):
        flag = "installable" if cand.installable else "not-installable"
        ver = "verified" if cand.verified else "unverified"
        print(
            f"{flag}\t{cand.brew_kind}\t{cand.brew_name}\t{ver}\t"
            f"{cand.app.name}\t{cand.app.path}\t{cand.app.bundle_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
