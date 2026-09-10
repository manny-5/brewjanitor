"""Piece 4 — replace: hand an app over to Homebrew, preferring adoption.

This is the only piece that mutates the system. Safety model:

  * `replace(...)` defaults to **dry-run**: it computes and prints the plan for
    each installable candidate but changes nothing.
  * The destructive path is only entered when `apply=True`. Even then the
    install-before-delete order is mandatory:
      1. `brew install --cask --adopt <name>`,
      2. verify the brew install actually placed an .app that matches the
         original by bundle_id (or, failing that, by .app path),
      3. only if verification succeeds -- AND brew installed a separate copy
         somewhere else -- remove the original bundle from disk.
  * If the install or the verification fails, the original bundle is left
    untouched and the candidate is reported as `failed` (with a reason). We
    never delete first.

`--adopt` is central. An unmanaged app in /Applications occupies exactly the
path its cask installs to, and a plain `brew install --cask` refuses to
overwrite it, so the pre-adopt version of this module failed at step 1 for every
app it was designed to handle. Adoption also means the usual outcome deletes
NOTHING: brew takes ownership of the bundle already on disk. The rmtree path
below now only runs for the genuine leftover case (original in ~/Applications,
cask installed to /Applications).

Removing a bundle uses shutil.rmtree on the bundle directory. We refuse to
remove any path outside the configured scan directories (DEFAULT_SCAN_DIRS) as
a guard against deleting something unexpected.

This module is intentionally easy to test: a fake brew wrapper controls install
behaviour, and the removal step is isolated in `_remove_bundle` so a test can
swap it out.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
from pathlib import Path

from .brewcheck import Brew
from .brewsearch import AppCandidate
from .inventory import App, DEFAULT_SCAN_DIRS
from .streams import err as _err, out as _out


@dataclasses.dataclass(frozen=True)
class ReplaceResult:
    """Outcome of attempting to replace one app with a brew-managed install.

    Attributes:
        app: the original discovered App.
        brew_name: the formula/cask name installed (or attempted), or "".
        brew_kind: "formula" | "cask" | "".
        status: "replaced" | "skipped" | "failed" | "dry-run".
        reason: human-readable detail, e.g. why a verify or install failed.
    """

    app: App
    brew_name: str
    brew_kind: str
    status: str
    reason: str


def _is_safe_to_remove(app_path: str, allowed_roots: tuple[str, ...]) -> bool:
    """True only if app_path is a .app bundle strictly inside an allowed root.

    This is the last line of defence before an rmtree, so it is deliberately
    strict on three counts:

    * The path must end in `.app`. We only ever delete app bundles.
    * It must be a *strict descendant* of a root, never the root itself.
      `Path.relative_to` succeeds when the two paths are equal, so the earlier
      version returned True for "/Applications" -- i.e. it would have approved
      deleting the entire scan root.
    * BOTH the literal path and its symlink-resolved form must be inside an
      allowed root. Checking only the resolved form let a bundle be approved on
      the strength of where it points while rmtree acted on where it sits (and
      vice versa); requiring both closes that gap.

    Each form is compared against the matching form of the root -- literal
    against literal, resolved against resolved. Comparing a literal path to a
    resolved root instead rejects anything under a symlinked ANCESTOR that the
    two share, which on macOS includes every path under /var (a symlink to
    /private/var). That refuses legitimate bundles rather than admitting bad
    ones, but a guard that quietly says no to real work gets removed, and then
    it protects nothing.
    """
    literal = Path(os.path.abspath(app_path))
    resolved = Path(app_path).resolve(strict=False)
    if literal.suffix.lower() != ".app" or resolved.suffix.lower() != ".app":
        return False

    def _inside(target: Path, root: Path) -> bool:
        if target == root:
            return False  # the root itself is never removable
        try:
            target.relative_to(root)
        except ValueError:
            return False
        return True

    for raw_root in allowed_roots:
        try:
            root_literal = Path(os.path.abspath(raw_root))
            root_resolved = Path(raw_root).resolve(strict=False)
        except OSError:
            continue
        if _inside(literal, root_literal) and _inside(resolved, root_resolved):
            return True
    return False


def _remove_bundle(app_path: str) -> tuple[bool, str]:
    """Delete a .app bundle directory. Returns (success, reason)."""
    path = Path(app_path)
    if path.is_symlink():
        # Ambiguous: deleting the link leaves the real bundle, and deleting the
        # target leaves a dangling link. Refuse rather than guess.
        return False, f"refusing to remove a symlinked bundle: {app_path}"
    if not path.exists():
        return False, f"bundle not found: {app_path}"
    if not path.is_dir():
        return False, f"not a directory: {app_path}"
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return False, f"remove failed: {exc}"
    return True, ""


def reconcile(
    apps: list[App],
    brew: Brew | None = None,
    apply: bool = False,
    allowed_roots: tuple[str, ...] | None = None,
    remover=_remove_bundle,
) -> list[ReplaceResult]:
    """Detect and clean up leftover old bundles from an interrupted --apply run.

    If a previous `brewjanitor --apply` was killed after a cask install
    succeeded but before the old bundle was removed, the system now has BOTH
    the brew-managed app and the original bundle on disk. A normal re-run would
    see the original as "unmanaged" and try to install it again -- wasteful and
    confusing. This instead detects the orphan: an app that is NOT brew-managed
    (the old copy) but where brew now manages a cask declaring the same
    bundle_id, and removes ONLY the old bundle (no re-install). It is a pure
    cleanup of a leftover, never a fresh install.

    Safety mirrors replace(): dry-run by default (prints what it would remove),
    the path guard applies, and removal is the only mutation.
    """
    from .brewcheck import _build_brew_ownership

    brew = brew if brew is not None else Brew()
    roots = allowed_roots if allowed_roots is not None else DEFAULT_SCAN_DIRS
    results: list[ReplaceResult] = []
    if not brew.available:
        return [
            ReplaceResult(app=a, brew_name="", brew_kind="", status="skipped", reason="brew not available")
            for a in apps
        ]

    by_path, _, _ = _build_brew_ownership(brew)

    # Map bundle_id -> brew_name for every installed cask that reports one.
    by_bundle_id: dict[str, str] = {}
    for cask in brew.info_installed_all(is_cask=True):
        token = cask.get("token") or cask.get("full_name") or cask.get("name") or ""
        bid = cask.get("bundle_identifier", "") or ""
        if isinstance(token, str) and isinstance(bid, str) and bid:
            by_bundle_id.setdefault(bid, token)

    for a in apps:
        # Is the app itself already brew-managed BY PATH? The new brew-installed
        # copy lives at a path brew owns, so by_path matches it. We deliberately
        # do NOT use the basename index here: an old bundle at a DIFFERENT path
        # shares the .app basename with the new copy, but its path is not owned
        # by brew -- that mismatch is exactly what makes it an orphan.
        already = by_path.get(_normalize_path(a.path))
        if already:
            results.append(
                ReplaceResult(
                    app=a, brew_name=already[0], brew_kind=already[1], status="skipped",
                    reason="already brew-managed (not an orphan)",
                )
            )
            continue

        # Does brew now manage a cask matching this app's bundle_id?
        #
        # Bundle id ONLY -- never the .app basename. A basename match says
        # nothing more than "two files share a name": if a cask installed
        # /Applications/Firefox.app and you separately keep
        # ~/Applications/Firefox.app (a beta, or a pinned old version), a
        # basename rule declares your second copy an orphan and deletes it.
        # Worse, check() treats that same basename match as *managed, leave
        # alone*, so the two halves of the tool disagreed about the same app.
        # The bundle id is the identity brew itself records, so it is the only
        # signal strong enough to justify an rmtree.
        #
        # An app with no bundle id (unreadable Info.plist) is never an orphan
        # candidate -- we have nothing to match on, so we leave it alone.
        match_name = ""
        if a.bundle_id and a.bundle_id in by_bundle_id:
            match_name = by_bundle_id[a.bundle_id]

        if not match_name:
            results.append(
                ReplaceResult(app=a, brew_name="", brew_kind="", status="skipped", reason="no brew cask owns this bundle id; not an orphan")
            )
            continue

        if not apply:
            results.append(
                ReplaceResult(
                    app=a, brew_name=match_name, brew_kind="cask", status="dry-run",
                    reason=f"leftover from interrupted run: brew now manages {match_name}; would remove old bundle {a.path}",
                )
            )
            continue

        if not _is_safe_to_remove(a.path, roots):
            results.append(
                ReplaceResult(app=a, brew_name=match_name, brew_kind="cask", status="failed", reason=f"refusing to remove outside allowed roots: {a.path}")
            )
            continue

        removed, why = remover(a.path)
        status = "replaced" if removed else "failed"
        reason = f"removed leftover old bundle for {match_name}" + (f": {why}" if why else "")
        results.append(ReplaceResult(app=a, brew_name=match_name, brew_kind="cask", status=status, reason=reason))

    return results


def _normalize_path(path: str) -> str:
    """Resolve a path for stable comparison (mirror of brewcheck._normalize)."""
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return os.path.normpath(path)


def _owned_app_paths(info: dict) -> list[str]:
    """Absolute, normalized .app paths an installed cask now owns.

    Used to tell an *adopted in place* install (brew took ownership of the very
    bundle we started from) apart from a *fresh* install elsewhere (which leaves
    the original as a leftover we should clean up).
    """
    from .brewcheck import _cask_appdir_one, _cask_artifact_names, _resolve_artifact

    owned: list[str] = []
    for cask in info.get("casks", []):
        appdir = _cask_appdir_one(cask)
        for raw in _cask_artifact_names(cask):
            owned.append(_normalize_path(_resolve_artifact(raw, appdir)))
    return owned


def _verify_install(
    app: App, brew: Brew, brew_name: str, is_cask: bool
) -> tuple[bool, str, list[str]]:
    """Check that the brew install actually placed the expected app.

    We re-read `brew info --json=v2 --installed <name>` and look for an installed
    artifact whose bundle_id matches the original app's bundle_id, or whose .app
    filename matches the original. A match means brew now owns an equivalent
    app.

    Returns (ok, reason, owned_paths) -- owned_paths lets the caller decide
    whether the original bundle IS the adopted one (nothing to remove) or a
    separate leftover copy.
    """
    if not app.bundle_id and not app.name:
        return False, "no bundle id or name to verify against", []
    info = brew.info_json(brew_name, is_cask=is_cask)
    if not info:
        return False, f"brew info returned nothing for {brew_name}", []

    if not is_cask:
        # Apps come from casks, not formulae. brewsearch no longer proposes
        # formula candidates at all, but this stays as a backstop: we cannot
        # confirm a formula placed a matching .app, and no verified match means
        # no delete.
        return False, "formula installs are not auto-replaced (apps come from casks)", []

    from .brewsearch import _cask_app_artifacts

    owned = _owned_app_paths(info)
    for art_path, bid in _cask_app_artifacts(info):
        if app.bundle_id and bid and bid == app.bundle_id:
            return True, f"bundle id matches: {bid}", owned
        if Path(art_path).name.lower() == app.name.lower():
            return True, f"app path matches: {art_path}", owned
    return False, "installed artifact did not match original app", owned


def _install_failure_reason(stderr: str) -> str:
    """Turn a brew install failure into something the user can act on.

    The one worth naming is the --adopt version mismatch: brew will only adopt
    an existing bundle whose version matches the cask's current version, so an
    out-of-date app fails with a message that does not, on its own, suggest a
    remedy.
    """
    err = (stderr or "").strip()
    if "is different from the one being installed" in err:
        return (
            "cannot adopt: the installed app's version differs from the cask's. "
            "Update the app to the current version (or move it aside) and re-run, "
            "so brew can take ownership of it"
        )
    return f"install failed: {err[:200]}"


def replace(
    candidates: list[AppCandidate],
    brew: Brew | None = None,
    apply: bool = False,
    allowed_roots: tuple[str, ...] | None = None,
    remover=_remove_bundle,
) -> list[ReplaceResult]:
    """Replace installable apps with brew-managed installs.

    Args:
        candidates: installable AppCandidates from piece 3. Non-installable
            candidates are reported as `skipped`.
        brew: optional Brew wrapper (real or fake). Defaults to a real Brew.
        apply: when False (default), nothing is changed -- the plan is returned
            with status "dry-run" (and "skipped" for non-installable items). When
            True, installs run and verified originals are removed.
        allowed_roots: directories the remover is allowed to delete from. Defaults
            to DEFAULT_SCAN_DIRS. Any bundle outside these is never removed.
        remover: the function used to delete a bundle (overridable for tests).

    Returns:
        One ReplaceResult per candidate, in input order. Never raises on a
        per-item failure; failures become `failed` results with a reason.
    """
    brew = brew if brew is not None else Brew()
    roots = allowed_roots if allowed_roots is not None else DEFAULT_SCAN_DIRS
    results: list[ReplaceResult] = []

    for cand in candidates:
        if not cand.installable:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name="",
                    brew_kind="",
                    status="skipped",
                    reason="not installable via Homebrew",
                )
            )
            continue

        if not brew.available:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="skipped",
                    reason="brew not available",
                )
            )
            continue

        if not apply:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="dry-run",
                    reason=(
                        f"would `brew install --cask --adopt {cand.brew_name}` so brew "
                        f"takes ownership of {cand.app.path} in place (only removing it "
                        f"if brew installs a separate copy elsewhere)"
                    ),
                )
            )
            continue

        # --- apply path ---
        # `--adopt` is what makes this reachable at all. An unmanaged app in
        # /Applications sits on exactly the path the cask installs to, and a
        # plain `brew install --cask` refuses to overwrite it ("It seems there
        # is already an App at ..."), so the old code failed here every time for
        # the apps it was built to handle. With --adopt, brew verifies the
        # existing bundle matches the cask and takes ownership in place.
        is_cask = cand.brew_kind == "cask"
        install = brew.install(cand.brew_name, is_cask=is_cask, adopt=is_cask)
        if install.returncode != 0:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="failed",
                    reason=_install_failure_reason(install.stderr),
                )
            )
            continue

        ok, why, owned = _verify_install(cand.app, brew, cand.brew_name, is_cask=is_cask)
        if not ok:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="failed",
                    reason=f"verify failed: {why}",
                )
            )
            continue

        # Adopted in place: brew now owns the very bundle we started from, so
        # there is nothing left to delete. This is the common, and by far the
        # safest, outcome -- the whole rmtree path below is skipped.
        if _normalize_path(cand.app.path) in owned:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="replaced",
                    reason=f"brew now manages {cand.app.path} as {cand.brew_name} (adopted in place; nothing removed)",
                )
            )
            continue

        # Otherwise brew installed a fresh copy somewhere else (typically the
        # original lives in ~/Applications while the cask installs to
        # /Applications), so the original really is a leftover.
        if not _is_safe_to_remove(cand.app.path, roots):
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="failed",
                    reason=f"refusing to remove outside allowed roots: {cand.app.path}",
                )
            )
            continue

        removed, why = remover(cand.app.path)
        if not removed:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="failed",
                    reason=f"install ok but remove failed: {why}",
                )
            )
            continue

        results.append(
            ReplaceResult(
                app=cand.app,
                brew_name=cand.brew_name,
                brew_kind=cand.brew_kind,
                status="replaced",
                reason=f"installed {cand.brew_name} and removed {cand.app.path}",
            )
        )

    return results


def main() -> int:
    """Entry point for `python -m brewjanitor.replace`.

    Runs the full pipeline inventory -> brew check -> brew search -> replace in
    DRY-RUN mode (apply=False). Prints one line per result.
    """
    from .brewcheck import check
    from .brewsearch import search
    from .inventory import inventory

    brew = Brew()
    if not brew.available:
        _err("brew not found on PATH; nothing to replace.")
    unmanaged = [c.app for c in check(inventory(), brew) if not c.brew_managed]
    candidates = search(unmanaged, brew)
    installable = [c for c in candidates if c.installable]
    for res in replace(installable, brew, apply=False):
        _out(
            f"{res.status}\t{res.brew_kind}\t{res.brew_name}\t"
            f"{res.app.name}\t{res.reason}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
