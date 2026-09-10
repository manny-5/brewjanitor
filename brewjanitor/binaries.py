"""Piece 7 — binaries: which command-line tools could Homebrew manage?

This is the formula-side counterpart to the app pipeline, and it is
**report-only by design**. It never installs and never deletes. Nothing in this
module calls `brew install`, `brew uninstall`, or removes a file, and there is
no flag that makes it do so.

Why report-only
---------------
The cask pipeline can safely replace an app because three things line up: an
app has a `CFBundleIdentifier` (a real identity brew records), a cask maps to
exactly one `.app`, and `brew install --cask --adopt` lets brew take ownership
of the bundle already on disk. None of that holds for formulae:

  * A command-line tool has no identity. A file named `python3` is just a name,
    and name matching is precisely what produced the `R.app` -> `r` false match
    that the cask path had to stop doing.
  * A formula owns hundreds of files across bin/, lib/, include/ and share/,
    so "replace this binary" is not a well-defined operation.
  * There is no `--adopt` for formulae. Homebrew installs into its own prefix,
    so a "replacement" would leave both copies on disk with PATH order silently
    deciding which one runs.

So this piece answers the useful question -- *which of my manually installed
tools does Homebrew know about?* -- and stops there. Acting on the answer is a
decision for a human with context this tool does not have.

Strategy
--------
1. List executables in the locations where tools get installed by hand.
2. Discard everything that is already accounted for (see `classify`): Homebrew's
   own files, shims belonging to a GUI app, and anything owned by a version
   manager that is deliberately managing those versions itself.
3. For each survivor, `brew search --formula <name>` and require an **exact**
   name match. `brew search` matches substrings, so "ollama" also returns
   "gollama"; only an exact hit is reported.
4. Print, and optionally write a CSV.
"""

from __future__ import annotations

import csv
import dataclasses
import os
import sys
from pathlib import Path

from .brewcheck import Brew

# Where tools land when installed by hand -- a package's own installer, a
# `make install`, a curl-to-bash script. Deliberately NOT the whole PATH: a
# PATH sweep is dominated by self-contained product trees and language version
# managers, none of which are candidates. Use `all_path=True` to widen it.
DEFAULT_BIN_DIRS = (
    "/usr/local/bin",
    "/usr/local/sbin",
    os.path.expanduser("~/bin"),
    os.path.expanduser("~/.local/bin"),
)

# Directories the OS owns. Never candidates, and never safe to reason about.
SYSTEM_PREFIXES = (
    "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/usr/libexec",
    "/System", "/Library/Apple", "/var/run/com.apple",
)

# Path fragments that mean "a version manager owns this". Replacing one of
# these with a Homebrew formula would break the tool that is managing it.
VERSION_MANAGER_MARKERS = (
    "/.pyenv/", "/.rbenv/", "/.nodenv/", "/.nvm/", "/.asdf/", "/.jenv/",
    "/.cargo/", "/.rustup/", "/.gem/", "/.bun/", "/.deno/", "/.volta/",
    "/miniconda", "/anaconda", "/mambaforge", "/miniforge",
    "/.pyenv-", "/pipx/venvs/", "/.gvm/", "/.sdkman/",
)

# A non-standard directory holding more than this many executables is treated
# as one self-contained product (a scientific suite, a vendored toolchain)
# rather than as that many independent candidates.
PRODUCT_TREE_THRESHOLD = 25


@dataclasses.dataclass(frozen=True)
class Binary:
    """An executable found in a manual-install location.

    Attributes:
        name: the file name, e.g. "ollama".
        path: the path as found, e.g. "/usr/local/bin/ollama".
        resolved: the symlink-resolved target.
        skip_reason: "" if this is a genuine candidate; otherwise why it was
            ruled out. Ruled-out entries are kept (not dropped) so the report
            can explain what it looked at and why it stayed silent.
    """

    name: str
    path: str
    resolved: str
    skip_reason: str


@dataclasses.dataclass(frozen=True)
class BinaryCandidate:
    """A binary plus the formula Homebrew has for it, if any.

    Attributes:
        binary: the discovered Binary.
        formula: the exactly-matching formula name, or "".
        available: True if Homebrew has a formula of exactly this name.
    """

    binary: Binary
    formula: str
    available: bool


def classify(path: str, resolved: str, brew_prefix: str) -> str:
    """Return "" if `path` is a real candidate, else the reason it is not.

    The filtering is the whole value of this module. On a normal machine a raw
    listing is overwhelmingly things that are already managed; without these
    rules the report is noise and gets ignored.
    """
    if brew_prefix and (resolved.startswith(brew_prefix.rstrip("/") + "/")):
        return "already managed by Homebrew"
    for prefix in SYSTEM_PREFIXES:
        if resolved.startswith(prefix + "/") or resolved == prefix:
            return "part of macOS"
    # A shim pointing into an .app or .framework belongs to a GUI application.
    # That application is the app pipeline's business; swapping its CLI helper
    # for a formula would break it.
    if ".app/" in resolved or ".framework/" in resolved:
        return "belongs to an installed application"
    for marker in VERSION_MANAGER_MARKERS:
        if marker in resolved or marker in path:
            return "managed by a version manager"
    if not os.path.exists(resolved):
        return "broken symlink"
    return ""


def scan_bin_dir(directory: str, brew_prefix: str) -> list[Binary]:
    """Return every executable file directly inside `directory`.

    Read-only: lists names, resolves symlinks, checks the executable bit.
    A directory that does not exist simply yields nothing.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    found: list[Binary] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for entry in entries:
        path = os.path.join(str(root), entry)
        try:
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            resolved = os.path.realpath(path)
        except OSError:
            continue
        found.append(
            Binary(
                name=entry,
                path=path,
                resolved=resolved,
                skip_reason=classify(path, resolved, brew_prefix),
            )
        )
    return found


def _path_dirs() -> list[str]:
    """Directories on PATH, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part and os.path.isdir(part):
            seen.setdefault(os.path.abspath(part), None)
    return list(seen)


def binaries(
    directories: tuple[str, ...] | None = None,
    brew_prefix: str = "",
    all_path: bool = False,
) -> tuple[list[Binary], list[tuple[str, int]]]:
    """Find candidate executables.

    Returns (binaries, collapsed) where `collapsed` lists
    (directory, count) pairs for non-standard directories that held so many
    executables they are treated as a single product tree rather than as
    individual candidates -- see PRODUCT_TREE_THRESHOLD. Reporting those as one
    line each is the difference between a usable report and hundreds of rows
    for what is really one installed suite.
    """
    if directories is not None:
        dirs = list(directories)
    elif all_path:
        dirs = _path_dirs()
    else:
        dirs = list(DEFAULT_BIN_DIRS)

    standard = {os.path.abspath(d) for d in DEFAULT_BIN_DIRS}
    out: dict[str, Binary] = {}
    collapsed: list[tuple[str, int]] = []
    for directory in dirs:
        found = scan_bin_dir(directory, brew_prefix)
        candidates = [b for b in found if not b.skip_reason]
        if (
            os.path.abspath(directory) not in standard
            and len(candidates) > PRODUCT_TREE_THRESHOLD
        ):
            collapsed.append((directory, len(candidates)))
            continue
        for b in found:
            out.setdefault(b.path, b)
    return sorted(out.values(), key=lambda b: b.path), collapsed


def survey(
    found: list[Binary], brew: Brew | None = None, progress: bool = False
) -> list[BinaryCandidate]:
    """For each candidate binary, ask whether Homebrew has a formula for it.

    Exact name matches only. `brew search` matches substrings, so a loose
    comparison would claim "gollama" for "ollama". Even an exact match is only
    a *name* match -- Homebrew exposes no file list for an uninstalled formula,
    so there is no way to confirm it provides this same tool. That uncertainty
    is exactly why this module reports instead of acting.

    Never raises. When brew is unavailable, nothing is reported as available.
    """
    brew = brew if brew is not None else Brew()
    results: list[BinaryCandidate] = []
    candidates = [b for b in found if not b.skip_reason]
    total = len(candidates)
    index = 0
    for b in found:
        if b.skip_reason:
            results.append(BinaryCandidate(binary=b, formula="", available=False))
            continue
        index += 1
        if progress:
            print(f"checking [{index}/{total}] {b.name} ...", file=sys.stderr, flush=True)
        formula = ""
        if brew.available:
            for name in brew.search_formulae(b.name):
                if name.strip() == b.name:
                    formula = name.strip()
                    break
        results.append(
            BinaryCandidate(binary=b, formula=formula, available=bool(formula))
        )
    return results


FIELDS = ["name", "path", "resolved", "formula", "status"]


def write_report(candidates: list[BinaryCandidate], path: str | Path) -> int:
    """Write the surveyed binaries to a CSV. Returns the number of rows.

    Only rows Homebrew has a formula for are written; the point of the file is
    the actionable list. Overwrites an existing file and creates parent
    directories, matching report.write_report.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [c for c in candidates if c.available]
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for c in rows:
            writer.writerow([
                c.binary.name, c.binary.path, c.binary.resolved,
                c.formula, "formula available",
            ])
    return len(rows)


def report(
    brew: Brew | None = None,
    directories: tuple[str, ...] | None = None,
    all_path: bool = False,
    report_path: str | None = None,
    progress: bool = False,
) -> int:
    """Print the survey. Returns a process exit code (always 0: nothing failed).

    Read-only from top to bottom. This function is the entire public surface of
    the formula side, and it has no `apply` parameter by design.
    """
    brew = brew if brew is not None else Brew()
    if not brew.available:
        print("brew not found on PATH; cannot check formulae.", file=sys.stderr)
        return 1

    found, collapsed = binaries(directories, brew.prefix(), all_path)
    candidates = survey(found, brew, progress=progress)

    available = [c for c in candidates if c.available]
    unmatched = [c for c in candidates if not c.available and not c.binary.skip_reason]
    skipped = [c for c in candidates if c.binary.skip_reason]

    scanned = list(directories) if directories else (
        _path_dirs() if all_path else list(DEFAULT_BIN_DIRS)
    )
    print(f"Scanned {len(scanned)} location(s) for command-line tools.", file=sys.stderr)

    if available:
        print(
            f"\n{len(available)} tool(s) Homebrew has a formula for:",
            file=sys.stderr,
        )
        for c in available:
            print(f"  {c.binary.name}  ({c.binary.path})  ->  brew formula `{c.formula}`")
        print(
            "\nThis is a report only. brewjanitor will not install or remove "
            "command-line tools:\nHomebrew installs into its own prefix, so a "
            "'replacement' would leave both\ncopies on disk with PATH order "
            "deciding which one runs. Review each one and\ninstall it yourself "
            "if you want it managed:",
            file=sys.stderr,
        )
        for c in available:
            print(f"    brew install {c.formula}", file=sys.stderr)
    else:
        print("\nNo manually installed tools have a matching Homebrew formula.", file=sys.stderr)

    if unmatched:
        print(
            f"\n{len(unmatched)} tool(s) with no matching formula: "
            + ", ".join(sorted(c.binary.name for c in unmatched)),
            file=sys.stderr,
        )

    if skipped:
        reasons: dict[str, int] = {}
        for c in skipped:
            reasons[c.binary.skip_reason] = reasons.get(c.binary.skip_reason, 0) + 1
        print("\nAlready accounted for:", file=sys.stderr)
        for reason, count in sorted(reasons.items()):
            print(f"  {count:4d}  {reason}", file=sys.stderr)

    for directory, count in collapsed:
        print(
            f"\nSkipped {directory}: {count} executables, which looks like one "
            "self-contained\n  product rather than that many separate tools.",
            file=sys.stderr,
        )

    if report_path is not None:
        count = write_report(candidates, report_path)
        print(f"\nWrote {count} row(s) to {report_path}", file=sys.stderr)

    return 0


def main() -> int:
    """Entry point for `python -m brewjanitor.binaries`."""
    return report()


if __name__ == "__main__":
    sys.exit(main())
