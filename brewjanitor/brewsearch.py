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


def _pick_cask_by_name(app: App, candidates: list[str]) -> str | None:
    """Return the cask name that matches the app's normalized name, or None.

    This is the fast path: no `brew info` network call. Casks install apps, so
    we only consider cask candidates (lines suffixed ` (cask)`) whose cleaned
    name equals the app's hyphenated lowercased name. A name match alone is not
    enough to call something `verified` -- that requires a brew info artifact
    check -- but it is enough to decide the app is installable.
    """
    term = _app_name_lower(app)
    for raw in candidates:
        if not _is_cask_candidate(raw):
            continue
        name = _clean_candidate(raw)
        if name == term:
            return name
    return None


def _verify_cask(app: App, brew: Brew, name: str) -> bool:
    """True if cask `name` installs an app matching `app` by bundle id or path.

    This is the (slow, network) verification step: `brew info --json=v2 <name>`.
    It is only called when a caller needs the `verified` flag -- i.e. right
    before an actual install under --apply. A dry run never needs it.
    """
    info = brew.info_json_any(name)
    if not info or not info.get("casks"):
        return False
    for app_path, bid in _cask_app_artifacts(info):
        if app.bundle_id and bid and bid == app.bundle_id:
            return True
        if Path(app_path).name.lower() == app.name.lower():
            return True
    return False


def _evaluate(
    app: App, candidates: list[str], brew: Brew, verify: bool = False
) -> AppCandidate:
    """Pick the best brew candidate for one app from its search results.

    Fast by default: a cask whose name matches the app is accepted as
    installable (verified=False) with no `brew info` call. When `verify` is
    True (e.g. right before --apply), the chosen cask is confirmed via
    `brew info` against the app's bundle id or .app filename, and verified is
    set accordingly. Formula candidates are only accepted as a last resort.
    """
    # Preferred: a same-named cask.
    cask_name = _pick_cask_by_name(app, candidates)
    if cask_name is not None:
        verified = _verify_cask(app, brew, cask_name) if verify else False
        return AppCandidate(
            app=app,
            installable=True,
            brew_name=cask_name,
            brew_kind="cask",
            verified=verified,
        )

    # Last resort: a same-named formula (apps rarely install from formulae).
    term = _app_name_lower(app)
    for raw in candidates:
        if _is_cask_candidate(raw):
            continue
        if _clean_candidate(raw) == term:
            return AppCandidate(
                app=app,
                installable=True,
                brew_name=term,
                brew_kind="formula",
                verified=False,
            )

    return AppCandidate(app=app, installable=False, brew_name="", brew_kind="", verified=False)


def search(
    apps: list[App],
    brew: Brew | None = None,
    offline: bool = False,
    verify: bool = False,
    progress: bool = False,
) -> list[AppCandidate]:
    """For each App, decide whether Homebrew could install it.

    Args:
        apps: apps to evaluate. Typically the unmanaged subset from the brew
            check, but any App list is accepted.
        brew: optional Brew wrapper (e.g. a test fake). Defaults to a real Brew
            discovered on PATH.
        offline: when True, force verify=False (no per-candidate `brew info`
            calls). The default path is already fast: name matching needs no
            `brew info`. Kept for the existing --offline flag.
        verify: when True, confirm each matched cask via `brew info` (network)
            so the `verified` flag reflects a real artifact match. Only needed
            before an actual install (e.g. under --apply); a dry run never needs
            it, which is why dry runs are now fast by default.
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
        results.append(_evaluate(app, candidates, brew, verify=verify and not offline))
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
