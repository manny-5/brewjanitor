"""Piece 3 — brew search: which unmanaged apps *could* be installed via Homebrew.

Given the labelled list from `brewcheck.check()` (or the unmanaged subset),
decide for each *unmanaged* app whether Homebrew knows how to install it.

Strategy
--------
For every unmanaged App:
  1. Derive a search term from the bundle name by stripping the ".app" suffix
     (e.g. "Firefox.app" -> "firefox"). This is the most reliable, user-facing
     token Homebrew indexes casks by.
  2. `brew search --casks <term>` returns candidate cask names. The `--casks`
     scope matters: an unscoped `brew search` groups its output under `==>
     Formulae` / `==> Casks` headers that brew emits ONLY when stdout is a TTY,
     and we always capture through a pipe. Scoping the query means brew returns
     bare names with nothing to parse and no way to mistake a cask for a formula.
  3. An exact name match makes the app installable. `brew search` matches
     substrings, so exactness is what stops "Firefox.app" from picking up
     "firefox@nightly".
  4. Optionally (`verify=True`, used right before --apply), `brew info
     --json=v2 <name>` confirms the cask really installs an .app matching this
     one by bundle id or filename.
  5. Result: an `AppCandidate` per app — installable=True/False, the chosen cask
     name, and a `verified` flag saying whether we matched on path or bundle id
     (high confidence) vs. just found a same-named cask.

Casks only: a formula never provides a .app bundle, so formulae are not
candidates. See _evaluate for why accepting them was actively harmful.

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
from .streams import err as _err, out as _out


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


def _search_term(app: App) -> str:
    """Turn "Firefox.app" -> "firefox", "Firefox ESR.app" -> "firefox-esr".

    Homebrew cask names are lowercased and hyphenated, so we strip the .app
    suffix, collapse whitespace into hyphens, and lowercase. This keeps the term
    compatible with `brew search`, which matches the term against cask names."""
    name = _TERM_SUFFIX.sub("", app.name)
    name = re.sub(r"\s+", "-", name.strip())
    return name.lower()


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


def _pick_cask_by_name(app: App, cask_candidates: list[str]) -> str | None:
    """Return the cask name that matches the app's normalized name, or None.

    This is the fast path: no `brew info` network call. `cask_candidates` comes
    from `brew search --casks`, so every entry is already known to be a cask --
    we just need an exact name match against the app's hyphenated lowercased
    name. `brew search` matches substrings, so an exact comparison is what keeps
    "Firefox.app" from picking up "firefox@nightly" or "multifirefox".

    A name match alone is not enough to call something `verified` -- that
    requires a brew info artifact check -- but it is enough to decide the app is
    installable.
    """
    term = _app_name_lower(app)
    for name in cask_candidates:
        if name.strip() == term:
            return name.strip()
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
    app: App, cask_candidates: list[str], brew: Brew, verify: bool = False
) -> AppCandidate:
    """Pick the best brew candidate for one app from its cask search results.

    Fast by default: a cask whose name matches the app is accepted as
    installable (verified=False) with no `brew info` call. When `verify` is
    True (e.g. right before --apply), the chosen cask is confirmed via
    `brew info` against the app's bundle id or .app filename, and verified is
    set accordingly.

    Casks only. A formula is never a replacement for a .app bundle, and treating
    one as a candidate produces actively wrong matches -- "R.app" name-matches
    the `r` formula (the R language CLI), which has nothing to do with the GUI
    app. replace() already refuses to act on formula candidates, so accepting
    them here only inflated the "could be installed" count with entries that
    could never actually be replaced. An app with no matching cask is reported
    not-installable, which is the truthful answer.
    """
    cask_name = _pick_cask_by_name(app, cask_candidates)
    if cask_name is not None:
        verified = _verify_cask(app, brew, cask_name) if verify else False
        return AppCandidate(
            app=app,
            installable=True,
            brew_name=cask_name,
            brew_kind="cask",
            verified=verified,
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
            _err(f"searching [{index}/{total}] {app.name} ...")
        cask_candidates = brew.search_casks(_search_term(app))
        results.append(
            _evaluate(app, cask_candidates, brew, verify=verify and not offline)
        )
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
        _err("brew not found on PATH; nothing is installable.")
    unmanaged = [c.app for c in check(inventory(), brew) if not c.brew_managed]
    for cand in search(unmanaged, brew):
        flag = "installable" if cand.installable else "not-installable"
        ver = "verified" if cand.verified else "unverified"
        _out(
            f"{flag}\t{cand.brew_kind}\t{cand.brew_name}\t{ver}\t"
            f"{cand.app.name}\t{cand.app.path}\t{cand.app.bundle_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
