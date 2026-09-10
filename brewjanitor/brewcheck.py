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
from .streams import err as _err, out as _out

# How long a single read-only brew call may take before we give up and treat it
# as failed. Keeps a hung tap fetch from hanging the whole tool forever.
BREW_TIMEOUT = 60.0


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
        # Caches for read-only metadata queries so duplicate candidates across
        # many apps don't each pay the brew subprocess + network cost. This is
        # the single biggest speedup for a 40-app scan.
        self._info_any_cache: dict[str, dict | None] = {}
        self._search_cache: dict[str, list[str]] = {}
        self._prerelease: bool | None | str = "unknown"
        self._prefix: str | None = None

    def _env(self, read_only: bool) -> dict[str, str]:
        """Environment for a brew subprocess.

        HOMEBREW_NO_ENV_HINTS everywhere: the hints are chatty, they land on
        stderr, and stderr is where we look for the *actual* error.

        HOMEBREW_NO_AUTO_UPDATE on read-only calls only. A scan makes many brew
        calls in a row, and having one of them silently trigger a full `brew
        update` mid-scan is a long unexplained stall. An install, by contrast,
        genuinely wants fresh cask metadata, so auto-update is left alone there.
        """
        env = dict(os.environ)
        env["HOMEBREW_NO_ENV_HINTS"] = "1"
        if read_only:
            env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        return env

    def prerelease_macos(self) -> bool | None:
        """True if Homebrew considers this macOS a pre-release, None if unknown.

        Asked of Homebrew itself rather than derived from a version table here:
        brew already knows which macOS versions it supports, and that list moves
        every autumn. `brew doctor` is the only ordinary command that surfaces
        the warning, and it is far too slow to run for this, so we query the
        same underlying predicate directly. Cached; roughly half a second once.

        Returns None (not False) when the question cannot be answered, so a
        caller can distinguish "supported" from "could not tell".
        """
        if self._prerelease != "unknown":
            return self._prerelease  # type: ignore[return-value]
        self._prerelease = None
        if self.available:
            result = self._run(["ruby", "-e", "puts OS::Mac.version.prerelease?"], timeout=30.0)
            if result.returncode == 0:
                answer = result.stdout.strip().splitlines()
                if answer and answer[-1].strip() in ("true", "false"):
                    self._prerelease = answer[-1].strip() == "true"
        return self._prerelease  # type: ignore[return-value]

    @property
    def available(self) -> bool:
        """True if a `brew` executable was found on PATH."""
        return bool(self._brew)

    def _run(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        """Run a brew command, capturing output. Never raises; failures (including
        a timeout) map to a non-zero return code the caller inspects.

        The timeout keeps a single hung brew call (e.g. a tap fetch stuck on a
        flaky network) from hanging the whole tool forever. A timed-out call
        returns a synthetic failure with a stderr note instead of raising.

        Every failure mode is a return code, never an exception: a missing brew
        binary, a permissions problem, or any other OSError would otherwise take
        down a whole scan from inside a loop.
        """
        try:
            return subprocess.run(
                [self._brew, *args],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                env=self._env(read_only=True),
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                args=[self._brew, *args],
                returncode=124,
                stdout="",
                stderr=f"brew timed out after {timeout}s: {' '.join(args)}",
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=[self._brew, *args],
                returncode=127,
                stdout="",
                stderr=f"could not run brew: {exc}",
            )

    def _run_streaming(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a mutating brew command with its output VISIBLE to the user.

        Installs are the one place where hiding brew's output is actively
        harmful. Capturing both streams meant:

          * a cask whose installer needs a password appeared to hang forever --
            the prompt went into a pipe nobody displayed, while brew blocked on
            stdin waiting for an answer;
          * multi-minute downloads showed no progress at all;
          * warnings brew emits on stderr (notably the pre-release macOS notice)
            were never seen.

        So stdout and stdin are inherited -- brew talks to the terminal directly
        -- while stderr is *teed*: echoed through as it arrives and captured, so
        a failure reason can still be parsed out of it afterwards.
        """
        argv = [self._brew, *args]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=None,  # inherit: progress goes straight to the terminal
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._env(read_only=False),
            )
        except OSError as exc:
            return subprocess.CompletedProcess(
                args=argv, returncode=127, stdout="", stderr=f"could not run brew: {exc}"
            )

        captured: list[str] = []
        if proc.stderr is not None:
            for line in proc.stderr:
                captured.append(line)
                sys.stderr.write(line)
                sys.stderr.flush()
        proc.wait()
        return subprocess.CompletedProcess(
            args=argv, returncode=proc.returncode, stdout="", stderr="".join(captured)
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
        """Return the parsed `brew info --json=v2 --installed` payload for one item.

        `--installed` keeps this read-only and fast: it only describes what is
        already on disk rather than contacting taps. Use info_json_any when you
        need information about items that may not be installed yet (e.g. search
        candidates).
        """
        if not self.available:
            return None
        scope = "--cask" if is_cask else "--formula"
        result = self._run(
            ["info", scope, "--json=v2", "--installed", name], timeout=BREW_TIMEOUT
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return _loads_json(result.stdout)

    def info_installed_all(self, is_cask: bool) -> list[dict]:
        """Return the parsed `brew info --json=v2 --installed` payloads for ALL
        installed items of one kind in a SINGLE brew call.

        Calling info_json once per cask is O(n) slow subprocess calls; this is
        O(1). Returns the list of per-item dicts (each shaped like the value of
        info_json's `casks`/`formulae` key). Empty list on any failure. This is
        the single biggest speedup for the brew check on a machine with many
        installed casks.
        """
        if not self.available:
            return []
        scope = "--cask" if is_cask else "--formula"
        result = self._run(
            ["info", scope, "--json=v2", "--installed"], timeout=BREW_TIMEOUT
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = _loads_json(result.stdout)
        if payload is None:
            return []
        key = "casks" if is_cask else "formulae"
        items = payload.get(key, [])
        return items if isinstance(items, list) else []

    def info_json_any(self, name: str) -> dict | None:
        """Return the parsed `brew info --json=v2` payload for any brew item.

        Unlike info_json, this does not require the item to be installed: it
        contacts the taps to describe whatever brew knows about `name` (formula
        or cask). Still read-only; it only reads metadata. Results are cached per
        name so the same candidate is not re-queried across many apps. Returns
        None on any failure or when brew cannot resolve the name.
        """
        if not self.available:
            return None
        if name in self._info_any_cache:
            return self._info_any_cache[name]
        result = self._run(["info", "--json=v2", name], timeout=BREW_TIMEOUT)
        if result.returncode != 0 or not result.stdout.strip():
            self._info_any_cache[name] = None
            return None
        payload = _loads_json(result.stdout)
        if payload is None:
            self._info_any_cache[name] = None
            return None
        self._info_any_cache[name] = payload
        return payload

    def _search_scoped(self, term: str, scope: str) -> list[str]:
        """Return `brew search <scope> <term>` results as bare names.

        Scope is "--casks" or "--formula". Scoping the search is what makes the
        result unambiguous: brew prints one bare name per line with NO section
        headers, so there is nothing to parse and no way to confuse a cask for a
        formula. (The previous implementation parsed `==> Casks` headers out of
        an unscoped `brew search`, but brew only emits those headers when stdout
        is a TTY -- and we always capture through a pipe. Every cask therefore
        looked like a formula. See search_casks/search_formulae.)

        Results are cached per (term, scope). Empty if brew is unavailable or
        the search fails -- note that brew exits non-zero when a search simply
        has no matches, which is correctly reported here as "no candidates".
        """
        if not self.available:
            return []
        key = f"{scope}\x00{term}"
        if key in self._search_cache:
            return self._search_cache[key]
        result = self._run(["search", scope, term], timeout=BREW_TIMEOUT)
        if result.returncode != 0:
            self._search_cache[key] = []
            return []
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        self._search_cache[key] = names
        return names

    def prefix(self) -> str:
        """Homebrew's install prefix (e.g. /opt/homebrew), or "" if unknown.

        Used to recognise files Homebrew already owns. Cached for the life of
        the wrapper; the prefix cannot change mid-run.
        """
        if self._prefix is None:
            self._prefix = ""
            if self.available:
                result = self._run(["--prefix"], timeout=BREW_TIMEOUT)
                if result.returncode == 0:
                    self._prefix = result.stdout.strip()
        return self._prefix

    def search_casks(self, term: str) -> list[str]:
        """Cask names matching `term`, one per line, no headers. Cached."""
        return self._search_scoped(term, "--casks")

    def search_formulae(self, term: str) -> list[str]:
        """Formula names matching `term`, one per line, no headers. Cached."""
        return self._search_scoped(term, "--formula")

    # ------------------------------------------------------------------
    # Mutating operations. Unlike the read-only methods above, these change
    # the system. They MUST be gated behind an explicit user opt-in (the
    # --apply flag) by callers; the wrapper itself does not enforce that.
    # ------------------------------------------------------------------
    def install(
        self, name: str, is_cask: bool, adopt: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Install a formula (`brew install`) or cask (`brew install --cask`).

        `adopt` adds `--adopt`, which tells Homebrew to take ownership of an
        .app that is ALREADY at the cask's install location instead of refusing
        to overwrite it. This is essential for brewjanitor's whole purpose: an
        unmanaged app in /Applications occupies exactly the path the cask wants,
        so a plain `brew install --cask` always fails with "It seems there is
        already an App at ...". With --adopt, brew verifies the existing bundle
        matches the cask version and adopts it in place -- no delete required.

        This is a mutating operation: callers are responsible for only invoking
        it when the user has explicitly opted in (e.g. --apply). It returns the
        raw completed process so callers can inspect the return code and stderr
        to decide whether the install actually succeeded.
        """
        args = ["install", "--cask", name] if is_cask else ["install", name]
        if adopt and is_cask:
            args.insert(1, "--adopt")
        return self._run_streaming(args)

    def uninstall(self, name: str, is_cask: bool) -> subprocess.CompletedProcess[str]:
        """Uninstall a formula or cask. Mutating; caller-gated like install().

        Note: brewjanitor's replace step intentionally does NOT call this for
        the original app bundle, because the bundle is not a brew item to begin
        with -- it is removed from disk directly. This method is provided for
        completeness and rollback scenarios.
        """
        args = ["uninstall", "--cask", name] if is_cask else ["uninstall", name]
        return self._run_streaming(args)


def _loads_json(text: str) -> dict | None:
    """Parse brew's JSON, tolerating a non-JSON preamble on stdout.

    brew writes warnings to stderr today -- verified on this machine, including
    the pre-release macOS notice -- so plain json.loads is the normal path. But
    a pre-release OS is precisely where brew grows new output, and one stray
    line prepended to stdout would otherwise turn a whole payload into "brew
    returned nothing". Falling back to the first `{` costs nothing and fails
    closed.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start > 0:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None
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


def _formula_keg_paths(formula: dict, brew_prefix: str) -> list[str]:
    """Absolute paths an installed formula owns: its kegs and its opt link.

    Homebrew's JSON does NOT report a path for an installed formula. The
    `installed[]` entries carry only version/bottle metadata -- the keys the
    previous implementation read (`installed_as_dependency_path`,
    `installed_on`) do not exist, so formula ownership silently never matched
    anything. The path has to be constructed from brew's own layout:

        <prefix>/Cellar/<name>/<version>   one per installed version
        <prefix>/opt/<name>                the stable symlink to the linked keg

    Both matter. An app symlinked out of a keg into /Applications resolves under
    one or the other depending on how the link was made.
    """
    if not brew_prefix:
        return []
    name = formula.get("full_name") or formula.get("name") or ""
    if not isinstance(name, str) or not name:
        return []
    # A tapped formula's full_name ("someone/tap/foo") is not the Cellar
    # directory; the Cellar always uses the bare name.
    bare = formula.get("name") or name.rsplit("/", 1)[-1]
    paths = [os.path.join(brew_prefix, "opt", bare)]
    for keg in formula.get("installed", []) or []:
        version = keg.get("version") if isinstance(keg, dict) else None
        if isinstance(version, str) and version:
            paths.append(os.path.join(brew_prefix, "Cellar", bare, version))
    return paths


def _resolve_artifact(raw: str, appdir: str) -> str:
    """Turn a cask artifact name into an absolute path for matching.

    brew info reports app artifacts as the install name (e.g. "Firefox.app")
    which lives in the cask's appdir. Resolving a relative name directly would
    anchor it against the *current working directory* (wrong), so we anchor it
    against appdir. Absolute artifacts are passed through unchanged.
    """
    if os.path.isabs(raw):
        return raw
    return os.path.join(appdir, raw)


def _cask_artifact_names(cask: dict) -> list[str]:
    """The .app install names for ONE cask dict (an element of the casks[] list)."""
    names: list[str] = []
    for artifact in cask.get("artifacts", []) or []:
        if isinstance(artifact, dict):
            apps = artifact.get("app", [])
        elif isinstance(artifact, list):
            apps = artifact
        else:
            apps = []
        for item in apps:
            if isinstance(item, str) and item.endswith(".app"):
                names.append(item)
    return names


def _cask_appdir_one(cask: dict) -> str:
    """The install appdir for ONE cask dict, defaulting to /Applications."""
    for artifact in cask.get("artifacts", []) or []:
        if isinstance(artifact, dict):
            app = artifact.get("app")
            if isinstance(app, dict):
                target = app.get("target")
                if isinstance(target, str) and target:
                    return target
    return "/Applications"


def _build_brew_ownership(
    brew: Brew,
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]], list[tuple[str, str]]]:
    """Build two indexes of everything Homebrew owns.

    Returns (by_path, by_basename, keg_prefixes):
      by_path: normalized absolute bundle path -> (brew_name, kind). Preferred.
      by_basename: lowercase .app filename -> (brew_name, kind). Fallback used
      when the cask only reports a relative artifact name we cannot anchor with
      full confidence (e.g. an appdir we did not detect). Matching by basename is
      less precise but catches the common case where an app sits in
      /Applications under the same name the cask installed.
      keg_prefixes: (normalized keg directory, formula name) pairs. A formula
      owns a whole directory tree rather than one bundle, so these are matched
      by containment, not equality -- an .app symlinked out of a keg into
      /Applications resolves to a path *under* the keg.

    Efficiency: this uses ONE `brew info --json=v2 --installed --cask` and ONE
    `... --formula` call to describe all installed items at once, instead of one
    brew call per item. If that batched call fails we fall back to per-item
    info_json so a partial brew state still yields correct (if slower) results.
    """
    by_path: dict[str, tuple[str, str]] = {}
    by_basename: dict[str, tuple[str, str]] = {}
    keg_prefixes: list[tuple[str, str]] = []

    casks = brew.info_installed_all(is_cask=True)
    if not casks:
        # Fallback to per-cask calls if the single batched call failed.
        for name in brew.list_casks():
            info = brew.info_json(name, is_cask=True)
            if not info:
                continue
            for cask in info.get("casks", []):
                _index_one_cask(cask, by_path, by_basename)
    else:
        for cask in casks:
            _index_one_cask(cask, by_path, by_basename)

    brew_prefix = brew.prefix()
    formulae = brew.info_installed_all(is_cask=False)
    if not formulae:
        formulae = []
        for name in brew.list_formulae():
            info = brew.info_json(name, is_cask=False)
            if info:
                formulae.extend(info.get("formulae", []))
    for formula in formulae:
        fname = formula.get("full_name") or formula.get("name") or ""
        for raw in _formula_keg_paths(formula, brew_prefix):
            keg_prefixes.append((_normalize(raw), fname))

    return by_path, by_basename, keg_prefixes


def _index_one_cask(cask: dict, by_path: dict[str, tuple[str, str]], by_basename: dict[str, tuple[str, str]]) -> None:
    """Add one cask dict's installed apps to both ownership indexes."""
    name = cask.get("token") or cask.get("full_name") or cask.get("name") or ""
    if not isinstance(name, str) or not name:
        return
    appdir = _cask_appdir_one(cask)
    for raw in _cask_artifact_names(cask):
        absolute = _resolve_artifact(raw, appdir)
        by_path.setdefault(_normalize(absolute), (name, "cask"))
        by_basename.setdefault(os.path.basename(raw).lower(), (name, "cask"))


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

    by_path, by_basename, keg_prefixes = _build_brew_ownership(brew)

    checked: list[CheckedApp] = []
    for a in apps:
        # Preferred: match on the full resolved path. This is exact and handles
        # cask symlinks into the Caskroom.
        match = by_path.get(_normalize(a.path))
        # Fallback: match on the .app basename. This catches the common case
        # where the cask reported only a relative name ("Firefox.app") and our
        # appdir detection did not yield the exact install location. The
        # basename of an installed app is usually unique enough to be safe here.
        if match is None and a.name:
            match = by_basename.get(a.name.lower())
        # Last: does the app live inside a formula's keg? A formula owns a
        # directory tree, so this is a containment test. Catches the handful of
        # formulae that ship a .app and symlink it into /Applications.
        if match is None:
            resolved = _normalize(a.path)
            for keg, fname in keg_prefixes:
                if resolved == keg or resolved.startswith(keg.rstrip("/") + os.sep):
                    match = (fname, "formula")
                    break
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
        _err("brew not found on PATH; nothing is managed.")
    for item in check(inventory(), brew):
        flag = "managed" if item.brew_managed else "unmanaged"
        _out(
            f"{flag}\t{item.brew_kind}\t{item.brew_name}\t"
            f"{item.app.name}\t{item.app.path}\t{item.app.bundle_id}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
