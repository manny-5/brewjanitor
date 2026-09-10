"""Test doubles for the `brew` command line.

`FakeBrew` mimics the surface of `brewcheck.Brew` without ever spawning a
subprocess, so the whole pipeline -- including the destructive `--apply` path --
can be exercised with no Homebrew installed and nothing on disk touched. It
records every mutating call so tests can assert on the exact brew invocation
(e.g. that `--adopt` was requested).
"""

from __future__ import annotations

import subprocess


def cask_info(token, bundle_id="", apps=(), appdir=None):
    """Build a `brew info --json=v2` payload for a single app cask.

    Mirrors the real shape: casks[].artifacts[].app[] holds bundle *install
    names* ("Firefox.app"), not absolute paths, which is exactly the detail the
    ownership code has to anchor against an appdir.
    """
    artifacts = [{"app": list(apps)}]
    if appdir is not None:
        artifacts.append({"app": {"target": appdir}})
    return {
        "casks": [
            {
                "token": token,
                "full_name": token,
                "bundle_identifier": bundle_id,
                "artifacts": artifacts,
            }
        ],
        "formulae": [],
    }


def cask_info_pkg(token, bundle_ids=(), pkg="Installer.pkg", uninstall=None, zap=None):
    """Build a `brew info --json=v2` payload for a single pkg cask.

    Mirrors the real shape for a cask that ships a .pkg installer instead of an
    .app bundle (Malwarebytes, the Microsoft Office suite, NordVPN). Such a
    cask has NO `artifacts[].app[]` entry -- so the ownership/verify code cannot
    look for a .app path -- and typically an empty `bundle_identifier`. The
    app's identity lives in the uninstall/zap directives: `quit` (a bundle id
    or list of them), `login_item`, `pkgutil`, and `trash` (paths whose leaf
    is a bundle id). `bundle_ids` is the convenience list the test author means
    to expose via `quit`; pass a richer `uninstall`/`zap` for cases that need
    the other directives.
    """
    if uninstall is None:
        uninstall = [{"quit": list(bundle_ids)}]
    artifacts = [{"pkg": pkg}, {"uninstall": uninstall}]
    if zap is not None:
        artifacts.append({"zap": zap})
    return {
        "casks": [
            {
                "token": token,
                "full_name": token,
                "bundle_identifier": "",
                "artifacts": artifacts,
            }
        ],
        "formulae": [],
    }


def ok(stdout=""):
    return subprocess.CompletedProcess(args=["brew"], returncode=0, stdout=stdout, stderr="")


def fail(stderr="boom", returncode=1):
    return subprocess.CompletedProcess(args=["brew"], returncode=returncode, stdout="", stderr=stderr)


class FakeBrew:
    """A stand-in for Brew with scripted responses and recorded calls."""

    def __init__(
        self,
        available=True,
        casks_by_term=None,
        formulae_by_term=None,
        info_by_name=None,
        installed_casks=(),
        installed_formulae=(),
        install_result=None,
        prefix="/opt/homebrew",
    ):
        self.available = available
        self._casks_by_term = casks_by_term or {}
        self._formulae_by_term = formulae_by_term or {}
        self._info_by_name = info_by_name or {}
        self._installed_casks = list(installed_casks)
        self._installed_formulae = list(installed_formulae)
        self._prefix = prefix
        self._install_result = install_result or ok()
        # Recorded calls, for assertions.
        self.install_calls: list[tuple[str, bool, bool]] = []
        self.uninstall_calls: list[tuple[str, bool]] = []
        self.search_calls: list[tuple[str, str]] = []

    # --- read-only surface -------------------------------------------------
    def search_casks(self, term):
        self.search_calls.append((term, "--casks"))
        return list(self._casks_by_term.get(term, []))

    def search_formulae(self, term):
        self.search_calls.append((term, "--formula"))
        return list(self._formulae_by_term.get(term, []))

    def info_json(self, name, is_cask):
        return self._info_by_name.get(name)

    def info_json_any(self, name):
        return self._info_by_name.get(name)

    def info_installed_all(self, is_cask):
        return list(self._installed_casks) if is_cask else list(self._installed_formulae)

    def prefix(self):
        return self._prefix

    def list_casks(self):
        return [c.get("token", "") for c in self._installed_casks]

    def list_formulae(self):
        return [f.get("name", "") for f in self._installed_formulae]

    # --- mutating surface --------------------------------------------------
    def install(self, name, is_cask, adopt=False):
        self.install_calls.append((name, is_cask, adopt))
        return self._install_result

    def uninstall(self, name, is_cask):
        self.uninstall_calls.append((name, is_cask))
        return ok()


class RecordingRemover:
    """Stands in for replace._remove_bundle; records without deleting."""

    def __init__(self, succeed=True, reason=""):
        self.calls: list[str] = []
        self._succeed = succeed
        self._reason = reason

    def __call__(self, app_path):
        self.calls.append(app_path)
        return self._succeed, self._reason
