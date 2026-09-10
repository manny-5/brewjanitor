"""Piece 4 — replace: install via brew FIRST, verify, then remove the old bundle.

This is the only piece that mutates the system. Safety model:

  * `replace(...)` defaults to **dry-run**: it computes and prints the plan for
    each installable candidate but changes nothing.
  * The destructive path is only entered when `apply=True`. Even then the
    install-before-delete order is mandatory:
      1. install the brew item,
      2. verify the brew install actually placed an .app that matches the
         original by bundle_id (or, failing that, by .app path),
      3. only if verification succeeds, remove the original bundle from disk.
  * If the install or the verification fails, the original bundle is left
    untouched and the candidate is reported as `failed` (with a reason). We
    never delete first.

Removing a bundle uses shutil.rmtree on the bundle directory. We refuse to
remove any path outside the configured scan directories (DEFAULT_SCAN_DIRS) as
a guard against deleting something unexpected.

This module is intentionally easy to test: a fake brew wrapper controls install
behaviour, and the removal step is isolated in `_remove_bundle` so a test can
swap it out.
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

from .brewcheck import Brew
from .brewsearch import AppCandidate
from .inventory import App, DEFAULT_SCAN_DIRS


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
    """True only if app_path resolves inside one of the allowed scan roots.

    This guards against deleting a bundle that lives somewhere unexpected
    (e.g. a path crafted to escape /Applications). We resolve both sides so a
    symlinked /Applications is still treated as allowed.
    """
    target = Path(app_path).resolve(strict=False)
    for root in allowed_roots:
        try:
            root_resolved = Path(root).resolve(strict=False)
            target.relative_to(root_resolved)
            return True
        except ValueError:
            continue
    return False


def _remove_bundle(app_path: str) -> tuple[bool, str]:
    """Delete a .app bundle directory. Returns (success, reason)."""
    path = Path(app_path)
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
    (the old copy) but where brew now manages a cask whose artifact matches the
    same bundle_id or .app basename, and removes ONLY the old bundle (no
    re-install). It is a pure cleanup of a leftover, never a fresh install.

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

    by_path, by_basename = _build_brew_ownership(brew)

    # Map bundle_id -> brew_name for casks that report one, and basename -> name.
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

        # Does brew now manage a cask matching this app's bundle_id or basename?
        # (basename here is fine: it tells us a cask for this app name exists, so
        # the old bundle is a leftover of that cask's install.)
        match_name = ""
        if a.bundle_id and a.bundle_id in by_bundle_id:
            match_name = by_bundle_id[a.bundle_id]
        elif a.name and a.name.lower() in by_basename:
            match_name = by_basename[a.name.lower()][0]

        if not match_name:
            results.append(
                ReplaceResult(app=a, brew_name="", brew_kind="", status="skipped", reason="no brew cask owns this app; not an orphan")
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
        import os
        return os.path.normpath(path)


def _verify_install(
    app: App, brew: Brew, brew_name: str, is_cask: bool
) -> tuple[bool, str]:
    """Check that the brew install actually placed the expected app.

    We re-read `brew info --json=v2 --installed <name>` and look for an installed
    artifact whose bundle_id matches the original app's bundle_id, or whose .app
    filename matches the original. A match means brew now owns an equivalent
    app, so it is safe to remove the old bundle.
    """
    if not app.bundle_id and not app.name:
        return False, "no bundle id or name to verify against"
    info = brew.info_json(brew_name, is_cask=is_cask)
    if not info:
        return False, f"brew info returned nothing for {brew_name}"

    candidates: list[tuple[str, str]] = []
    if is_cask:
        from .brewsearch import _cask_app_artifacts

        candidates = _cask_app_artifacts(info)
    else:
        # Apps come from casks, not formulae. A formula candidate is almost
        # always a mislabelled cask (see the brew-search header bug). We refuse
        # to delete the original bundle for a formula "install", because we
        # cannot confirm a formula placed a matching .app. This keeps the
        # install-before-delete guarantee honest: no verified match, no delete.
        return False, "formula installs are not auto-replaced (apps come from casks)"

    for art_path, bid in candidates:
        if app.bundle_id and bid and bid == app.bundle_id:
            return True, f"bundle id matches: {bid}"
        if Path(art_path).name.lower() == app.name.lower():
            return True, f"app path matches: {art_path}"
    return False, "installed artifact did not match original app"


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
                        f"would `brew install {'--cask ' if cand.brew_kind=='cask' else ''}"
                        f"{cand.brew_name}`, verify, then remove {cand.app.path}"
                    ),
                )
            )
            continue

        # --- apply path ---
        is_cask = cand.brew_kind == "cask"
        install = brew.install(cand.brew_name, is_cask=is_cask)
        if install.returncode != 0:
            results.append(
                ReplaceResult(
                    app=cand.app,
                    brew_name=cand.brew_name,
                    brew_kind=cand.brew_kind,
                    status="failed",
                    reason=f"install failed: {install.stderr.strip()[:200]}",
                )
            )
            continue

        ok, why = _verify_install(cand.app, brew, cand.brew_name, is_cask=is_cask)
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
        print("brew not found on PATH; nothing to replace.", file=sys.stderr)
    unmanaged = [c.app for c in check(inventory(), brew) if not c.brew_managed]
    candidates = search(unmanaged, brew)
    installable = [c for c in candidates if c.installable]
    for res in replace(installable, brew, apply=False):
        print(
            f"{res.status}\t{res.brew_kind}\t{res.brew_name}\t"
            f"{res.app.name}\t{res.reason}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
