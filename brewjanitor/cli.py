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
        help="Write the couldn't-be-replaced apps to this CSV path. "
        "Defaults to ./brewjanitor-unreplaced.csv when --apply is used; in "
        "dry-run the report is still written to that default path.",
    )
    return parser


def run(apply: bool, report_path: str | None) -> int:
    """Run the full pipeline. Returns a process exit code."""
    brew = Brew()
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

    candidates = search(unmanaged_apps, brew)
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

    # Report: every unmanaged-not-installable app.
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

    # Exit non-zero if any apply step failed, so scripts/CIs can detect it.
    if apply and any(r.status == "failed" for r in results):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """`brewjanitor` entry point (see pyproject [project.scripts])."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return run(apply=args.apply, report_path=args.report)


if __name__ == "__main__":
    sys.exit(main())
