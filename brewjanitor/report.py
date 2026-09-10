"""Piece 5 — report: write out the "couldn't be replaced" list.

After the pipeline runs, some apps are neither Homebrew-managed nor installable
via Homebrew. Those are left untouched and recorded here so the user has a
durable list of what brewjanitor could not fold into Homebrew.

Output is CSV (stdlib csv module), one row per app:
    name, path, bundle_id, brew_name, brew_kind, reason

The report is append-friendly: if the target path already exists, we overwrite
it with a fresh run (the inventory is the source of truth each run). A summary
count is printed to stdout/stderr.

This piece never mutates apps; it only writes a report file.
"""

from __future__ import annotations

import csv
import dataclasses
import sys
from pathlib import Path

from .inventory import App
from .streams import err as _err


@dataclasses.dataclass(frozen=True)
class ReportEntry:
    """One row of the couldn't-be-replaced report.

    Attributes:
        app: the discovered App that could not be replaced.
        brew_name: the brew name that was considered, or "" if none found.
        brew_kind: "formula" | "cask" | "".
        reason: why it could not be replaced (e.g. "not installable via Homebrew").
    """

    app: App
    brew_name: str
    brew_kind: str
    reason: str


FIELDS = ["name", "path", "bundle_id", "brew_name", "brew_kind", "reason"]


def write_report(
    entries: list[ReportEntry], path: str | Path
) -> int:
    """Write the couldn't-be-replaced list to a CSV file.

    Overwrites `path` if it exists. Creates parent directories as needed so a
    path like `./reports/unreplaced.csv` works without a manual mkdir.

    Returns the number of rows written.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        for entry in entries:
            writer.writerow(
                [
                    entry.app.name,
                    entry.app.path,
                    entry.app.bundle_id,
                    entry.brew_name,
                    entry.brew_kind,
                    entry.reason,
                ]
            )
    return len(entries)


def main() -> int:
    """Entry point for `python -m brewjanitor.report`.

    Runs inventory -> brew check -> brew search and writes the couldn't-be-
    replaced apps to `./brewjanitor-unreplaced.csv`. Prints a summary line.
    """
    from .brewcheck import Brew, check
    from .brewsearch import search
    from .inventory import inventory

    brew = Brew()
    unmanaged = [c.app for c in check(inventory(), brew) if not c.brew_managed]
    cands = search(unmanaged, brew)

    entries: list[ReportEntry] = [
        ReportEntry(
            app=c.app,
            brew_name=c.brew_name,
            brew_kind=c.brew_kind,
            reason="not installable via Homebrew",
        )
        for c in cands
        if not c.installable
    ]

    out_path = "brewjanitor-unreplaced.csv"
    count = write_report(entries, out_path)
    _err(f"wrote {count} unreplaced app(s) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
