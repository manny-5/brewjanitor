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
    """Build a `brew info --json=v2` payload for a single cask.

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
        install_result=None,
    ):
        self.available = available
        self._casks_by_term = casks_by_term or {}
        self._formulae_by_term = formulae_by_term or {}
        self._info_by_name = info_by_name or {}
        self._installed_casks = list(installed_casks)
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
        return list(self._installed_casks) if is_cask else []

    def list_casks(self):
        return [c.get("token", "") for c in self._installed_casks]

    def list_formulae(self):
        return []

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
