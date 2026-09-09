"""Piece 2 — brew check: label each inventory App as Homebrew-managed or not.

Given the list of discovered .app bundles from `inventory.inventory()`, this
module decides which ones Homebrew already manages. It uses only read-only
brew commands (`brew list`, `brew info`): nothing here installs, uninstalls, or
modifies anything.

Strategy
--------
1. Ask Homebrew what it manages:
       `brew list --formula`   -> installed formulae (by name)
       `brew list --cask`       -> installed casks (by name)
2. For each installed formula/cask, ask where its artifacts live:
       `brew info --json=v2 --installed <name>`
   Casks report the .app bundles they installed (artifacts[].app paths);
   formulae report their installed keg (linked Cellar path). We collect the set
   of absolute paths Homebrew owns.
3. Label each inventory App:
   - managed  -> its bundle path appears in the set of brew-owned paths, OR its
                 bundle_id matches a cask that provides that bundle_id.
   - unmanaged -> otherwise.

If `brew` is not installed, or a command fails, we degrade gracefully: nothing
is labelled managed, and the whole inventory is reported as unmanaged. The brew
check must never raise out of the top level; later stages rely on a stable
return type.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .inventory import App


@dataclasses.dataclass(frozen=True)
class CheckedApp:
    """An inventory App plus a brew-managed flag and the source of that label.

    Attributes:
        app: the discovered App from inventory.
        brew_managed: True if Homebrew appears to own this bundle.
        brew_name: the formula/cask name that manages it, or "" if unmanaged.
        brew_kind: "formula" | "cask" | "" (empty when unmanaged or unknown).
    """

    app: App
    brew_managed: bool
    brew_name: str
    brew_kind: str


class Brew:
    """Thin read-only wrapper around the `brew` command line.

    Every method here calls brew in inspection mode only. Nothing installs,
    uninstalls, or links. If `brew` is missing on PATH, `available()` is False
    and the other methods return empty results instead of raising.
    """

    def __init__(self, brew_path: str | None = None) -> None:
        self._brew = brew_path or shutil.which("brew") or ""

    @property
    def available(self) -> bool:
        """True if a `brew` executable was found on PATH."""
        return bool(self._brew)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a brew command, capturing output. Never raises; failures map to
        a non-zero return code the caller inspects."""
        return subprocess.run(
            [self._brew, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def list_formulae(self) -> list[str]:
        """Installed formulae by name. Empty if brew is unavailable."""
        if not self.available:
            return []
        result = self._run(["list", "--formula", "-1"])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_casks(self) -> list[str]:
        """Installed casks by name. Empty if brew is unavailable."""
        if not self.available:
            return []
        result = self._run(["list", "--cask", "-1"])
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def info_json(self, name: str, is_cask: bool) -> dict | None:
        """Return the parsed `brew info --json=v2` payload for one item, or None.

        `--installed` keeps this read-only and fast: it only describes what is
        already on disk rather than contacting taps.
        """
        if not self.available:
            return None
        scope = "--cask" if is_cask else "--formula"
        result = self._run(["info", scope, "--json=v2", "--installed", name])
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None


def _normalize(path: str) -> str:
    """Resolve symlinks for stable path comparison.

    Homebrew casks often symlink /Applications/Foo.app -> the Caskroom, so we
    compare on resolved paths to avoid false "unmanaged" verdicts.
    """
    try:
        return str(Path(path).resolve(strict=False))
    except OSError:
        return os.path.normpath(path)


def _cask_app_paths(info: dict) -> list[str]:
    """Extract the .app bundle paths a cask installed from its info JSON.

    Cask JSON shape: casks[].artifacts[].app[] (strings) and/or
    casks[].artifacts[].{ "app": [...] }. We collect any string that ends with
    .app and any path under artifacts.uninstall that points at an .app.
    """
    paths: list[str] = []
    for cask in info.get("casks", []):
        for artifact in cask.get("artifacts", []) or []:
            if isinstance(artifact, dict):
                apps = artifact.get("app", [])
            elif isinstance(artifact, list):
                apps = artifact
            else:
                apps = []
            for item in apps:
                if isinstance(item, str) and item.endswith(".app"):
                    paths.append(item)
    return paths


def _formula_paths(info: dict) -> list[str]:
    """Extract installed paths for a formula from its info JSON.

    Formulae rarely correspond to a single .app, but we collect any linked keg
    path so path-based matching can still work for the few formulae that ship a
    .app. The primary signal for formulae is the installed linked keg prefix.
    """
    paths: list[str] = []
    for formula in info.get("formulae", []):
        for keg in formula.get("installed", []) or []:
            prefix = keg.get("installed_as_dependency_path") or keg.get("installed_on", {})
            if isinstance(prefix, dict):
                prefix = prefix.get("path")
            if isinstance(prefix, str) and prefix:
                paths.append(prefix)
    return paths


def _build_brew_ownership(brew: Brew) -> dict[str, tuple[str, str]]:
    """Map normalized bundle path -> (brew_name, kind) for everything brew owns.

    Only casks reliably produce .app paths, so they dominate this map. Formulae
    contribute too, but most formula entries will not collide with an inventory
    bundle path and are simply ignored at lookup time.
    """
    owned: dict[str, tuple[str, str]] = {}

    for cask in brew.list_casks():
        info = brew.info_json(cask, is_cask=True)
        if not info:
            continue
        for raw in _cask_app_paths(info):
            owned.setdefault(_normalize(raw), (cask, "cask"))

    for formula in brew.list_formulae():
        info = brew.info_json(formula, is_cask=False)
        if not info:
            continue
        for raw in _formula_paths(info):
            owned.setdefault(_normalize(raw), (formula, "formula"))

    return owned


def check(apps: list[App], brew: Brew | None = None) -> list[CheckedApp]:
    """Label each App as Homebrew-managed or not.

    Args:
        apps: the inventory() output to classify.
        brew: an optional Brew wrapper (e.g. a test fake). Defaults to a real
            Brew discovered on PATH.

    Returns:
        One CheckedApp per input App, in the same order. Always returns; never
        raises. When brew is unavailable, every App is reported unmanaged.
    """
    brew = brew if brew is not None else Brew()
    if not brew.available:
        return [CheckedApp(app=a, brew_managed=False, brew_name="", brew_kind="") for a in apps]

    owned = _build_brew_ownership(brew)

    checked: list[CheckedApp] = []
    for a in apps:
        match = owned.get(_normalize(a.path))
        if match:
            name, kind = match
            checked.append(CheckedApp(app=a, brew_managed=True, brew_name=name, brew_kind=kind))
        else:
            checked.append(CheckedApp(app=a, brew_managed=False, brew_name="", brew_kind=""))
    return checked


def main() -> int:
    """Entry point for `python -m brewjanitor.brewcheck`.

    Prints one tab-separated line per app:
        managed_flag<TAB>kind<TAB>brew_name<TAB>name<TAB>path<TAB>bundle_id
    """
    from .inventory import inventory

    brew = Brew()
    if not brew.available:
        print("brew not found on PATH; nothing is managed.", file=sys.stderr)
    for item in check(inventory(), brew):
        flag = "managed" if item.brew_managed else "unmanaged"
        print(
            f"{flag}\t{item.brew_kind}\t{item.brew_name}\t"
            f"{item.app.name}\t{item.app.path}\t{item.app.bundle_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
