"""Piece 6 — CLI: wire the pipeline together behind the `brewjanitor` command.

Usage:
    brewjanitor                 # dry-run: print the plan, change nothing
    brewjanitor --apply         # install via brew, verify, remove old bundles
    brewjanitor --report PATH   # also write the couldn't-be-replaced CSV to PATH

Safety by default: with no flags, brewjanitor only inspects and prints. The
`--apply` flag is the single explicit opt-in required for any destructive
action (install + bundle removal).
"""

from __future__ import annotations

import argparse
import sys

from .brewcheck import Brew, check
from .brewsearch import search
from .inventory import inventory
from .replace import replace
from .report import ReportEntry, write_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brewjanitor",
        description=(
            "Bring the software on your Mac into the fold of Homebrew. Scans "
            "installed apps, checks which Homebrew manages, and (with --apply) "
            "replaces the rest with brew-managed installs where possible."
        ),
    )
    # The scan is the default; its flags live directly on the top-level parser so
    # `brewjanitor --apply` keeps working without a subcommand.
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform installs and remove old bundles. Without this "
        "flag, brewjanitor only prints the plan (dry-run).",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help="Write the couldn't-be-replaced apps to this CSV path. Only written "
        "under --apply (default path ./brewjanitor-unreplaced.csv) or when you pass "
        "this flag explicitly; a plain dry-run writes no files.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip the `brew info` verification calls even under --apply, matching casks "
        "by name only. Dry runs are already name-only and fast; this flag only "
        "affects --apply by leaving candidates unverified.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-app progress as the search step runs, so a long scan shows "
        "it is moving instead of appearing stuck.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Instead of the normal scan, detect and clean up leftover old bundles "
        "from an interrupted --apply run (where brew installed the cask but the "
        "old bundle was not removed). With --apply it removes the orphan; without "
        "it just reports what it would remove. No re-install happens.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    autoupdate_desc = (
        "Manage a user-level launchd job that runs `brew update && brew upgrade` "
        "daily. No sudo; the job runs as you from your own ~/Library/LaunchAgents "
        "and only ever calls brew. See `brewjanitor autoupdate --help`."
    )
    au = sub.add_parser("autoupdate", help=autoupdate_desc, description=autoupdate_desc)
    au.add_argument("--install", action="store_true", help="Install or replace the job.")
    au.add_argument("--remove", action="store_true", help="Remove the scheduled job.")
    au.add_argument("--status", action="store_true", help="Show the current job.")
    au.add_argument("--hour", type=int, default=8, help="Hour 0-23 to run (default 8).")
    au.add_argument("--minute", type=int, default=0, help="Minute 0-59 (default 0).")
    au.add_argument(
        "--greedy",
        action="store_true",
        help="Also upgrade casks that auto-update themselves (may quit running apps).",
    )
    au.add_argument("--cleanup", action="store_true", help="Also run `brew cleanup`.")
    return parser


def _note_prerelease_macos(brew: Brew, apply: bool) -> None:
    """Say once, up front, that Homebrew considers this macOS unsupported.

    Worth stating plainly because the consequences are narrower than the
    warning sounds, and silence would leave brew's own warning (which only
    appears on install, and only on stderr) to arrive unexplained mid-run.

    Casks are unaffected: they ship prebuilt applications that do not depend on
    the OS build, and `brew search --casks`, `brew info --json` and cask
    installs were all verified to behave identically here. What a pre-release
    macOS does change is *formulae*, which are built per-OS -- and brewjanitor
    does not install formulae.
    """
    if brew.prerelease_macos() is not True:
        return
    print(
        "Note: Homebrew considers this macOS a pre-release and does not support it.\n"
        "      This does not affect brewjanitor: it installs casks, which are\n"
        "      prebuilt apps and OS-independent. (Formulae are built per-OS and\n"
        "      would be the part affected -- brewjanitor installs none.)",
        file=sys.stderr,
    )
    if apply:
        print(
            "      brew may print its own pre-release warning during --apply; that is\n"
            "      expected, and brew's output is shown to you in full.",
            file=sys.stderr,
        )


def run(apply: bool, report_path: str | None, offline: bool = False, verbose: bool = False) -> int:
    """Run the full pipeline. Returns a process exit code."""
    brew = Brew()
    _note_prerelease_macos(brew, apply)
    if not brew.available:
        print(
            "brew not found on PATH; install Homebrew first (https://brew.sh).",
            file=sys.stderr,
        )
        # We can still inventory and report; we just won't manage or replace.
        print("Inventory only (no brew to check against):", file=sys.stderr)

    apps = inventory()
    print(f"Found {len(apps)} app(s).", file=sys.stderr)

    checked = check(apps, brew)
    managed = [c for c in checked if c.brew_managed]
    unmanaged_apps = [c.app for c in checked if not c.brew_managed]
    print(
        f"Homebrew manages {len(managed)}; {len(unmanaged_apps)} are unmanaged.",
        file=sys.stderr,
    )

    candidates = search(
        unmanaged_apps, brew, offline=offline, verify=apply, progress=verbose
    )
    installable = [c for c in candidates if c.installable]
    not_installable = [c for c in candidates if not c.installable]
    print(
        f"Of the unmanaged, {len(installable)} could be installed via Homebrew; "
        f"{len(not_installable)} could not.",
        file=sys.stderr,
    )

    # Replace step: only installable candidates are acted on. Non-installable
    # ones go to the report instead.
    if apply and brew.available:
        print("--apply is set: installing and replacing (this changes your system).", file=sys.stderr)
    else:
        print("Dry-run (no --apply): printing the plan only.", file=sys.stderr)

    results = replace(installable, brew, apply=apply)

    print("\nResults:", file=sys.stderr)
    for res in results:
        print(
            f"  [{res.status}] {res.app.name} "
            f"({res.brew_kind} {res.brew_name}) -> {res.reason}"
        )

    # Report: every unmanaged-not-installable app. A dry-run does NOT write a
    # file (it should not touch the user's working directory); the report is
    # only written under --apply, or whenever the user passes --report PATH to
    # opt into a file even in dry-run.
    if apply or report_path is not None:
        entries = [
            ReportEntry(
                app=c.app,
                brew_name=c.brew_name,
                brew_kind=c.brew_kind,
                reason="not installable via Homebrew",
            )
            for c in not_installable
        ]
        out_path = report_path if report_path is not None else "brewjanitor-unreplaced.csv"
        count = write_report(entries, out_path)
        print(f"\nWrote {count} unreplaced app(s) to {out_path}", file=sys.stderr)
    else:
        if not_installable:
            print(
                f"\n{len(not_installable)} app(s) could not be replaced; re-run with "
                "--report PATH.csv (or --apply) to write the list to a file.",
                file=sys.stderr,
            )

    # Exit non-zero if any apply step failed, so scripts/CIs can detect it.
    if apply and any(r.status == "failed" for r in results):
        return 1
    return 0


def _run_reconcile(apply: bool, verbose: bool) -> int:
    """Detect and clean up leftover old bundles from an interrupted --apply.

    Runs over the full inventory (so we catch orphans whether or not they'd
    show as "unmanaged"). Uses replace.reconcile, which only removes the old
    bundle when brew now manages a matching cask -- never re-installs.
    """
    from .replace import reconcile

    brew = Brew()
    if not brew.available:
        print("brew not found on PATH; cannot reconcile.", file=sys.stderr)
        return 1
    apps = inventory()
    print(f"Checking {len(apps)} app(s) for leftover old bundles ...", file=sys.stderr)
    results = reconcile(apps, brew, apply=apply)
    print("\nReconcile results:", file=sys.stderr)
    for res in results:
        if res.status == "skipped":
            continue
        print(
            f"  [{res.status}] {res.app.name} "
            f"({res.brew_kind} {res.brew_name}) -> {res.reason}"
        )
    leftovers = [r for r in results if r.status in ("dry-run", "replaced", "failed")]
    if not leftovers:
        print("No leftover old bundles found; nothing to clean up.", file=sys.stderr)
    elif not apply:
        print(
            f"\n{len(leftovers)} leftover bundle(s) found. Re-run with --apply to remove them.",
            file=sys.stderr,
        )
    return 0


def _run_autoupdate(args: argparse.Namespace) -> int:
    """Dispatch the `autoupdate` subcommand to its handlers (no re-parsing)."""
    from . import autoupdate as _au

    if args.remove:
        return _au.remove()
    if args.status:
        return _au.status()
    if args.install:
        if not 0 <= args.hour <= 23:
            print("--hour must be 0-23", file=sys.stderr)
            return 1
        if not 0 <= args.minute <= 59:
            print("--minute must be 0-59", file=sys.stderr)
            return 1
        return _au.install(args.hour, args.minute, args.greedy, args.cleanup)
    # No action given: print the autoupdate help.
    from . import autoupdate as _au2

    _au2.main(["--help"])
    return 0


def main(argv: list[str] | None = None) -> int:
    """`brewjanitor` entry point (see pyproject [project.scripts])."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) == "autoupdate":
        return _run_autoupdate(args)
    if args.reconcile:
        return _run_reconcile(apply=args.apply, verbose=args.verbose)
    return run(apply=args.apply, report_path=args.report, offline=args.offline, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
