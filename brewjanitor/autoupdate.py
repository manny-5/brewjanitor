"""Scheduled upgrades via macOS launchd (user-level, no sudo).

`brewjanitor autoupdate` installs, inspects, or removes a launchd job that runs
`brew update && brew upgrade` on a schedule you choose. Everything here is
explicit and user-scoped:

  * The job lives in ~/Library/LaunchAgents/ (your own home folder), NOT the
    system /Library/LaunchAgents. It runs as *you* with *your* permissions and
    can never touch system files. No `sudo` is ever used.
  * The .plist is plain XML. You can read the whole file before loading it.
  * The only command the job runs is `brew` (update, upgrade, optionally
    --greedy and/or --cleanup). Nothing is downloaded or executed besides brew.
  * The brew path baked into the plist is resolved from your actual PATH at
    install time, so the job runs the same `brew` you use interactively.

This module only manages the scheduled job. It does not itself run upgrades.
"""

from __future__ import annotations

import argparse
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .streams import err as _err, out as _out

LABEL = "com.manny.brewjanitor.autoupdate"


def _launch_agents_dir() -> Path:
    """The user-level LaunchAgents directory. Never a system path."""
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _launch_agents_dir() / f"{LABEL}.plist"


def _resolve_brew() -> str | None:
    """Return the absolute path to brew on PATH, or None.

    We bake the absolute path into the plist so the scheduled job runs the exact
    brew you use, regardless of the launchd environment's PATH.
    """
    return shutil.which("brew")


def _command_line(brew_path: str, greedy: bool, cleanup: bool) -> str:
    """The exact shell command the scheduled job runs.

    Shared by the plist and by what `--install` prints, so the two cannot
    drift: the line you are shown is built from the same code that goes into
    the job, not reconstructed alongside it.
    """
    command = shlex.quote(brew_path)
    upgrade = f"{command} upgrade" + (" --greedy" if greedy else "")
    steps = [f"{command} update", upgrade]
    if cleanup:
        steps.append(f"{command} cleanup")
    return " && ".join(steps)


def _build_plist(
    brew_path: str,
    hour: int,
    minute: int,
    greedy: bool,
    cleanup: bool,
) -> bytes:
    """Build the launchd plist as XML bytes for the autoupdate job.

    The job runs `brew update` then `brew upgrade` (with --greedy/--cleanup if
    requested). StartCalendarInterval fires it once a day at hour:minute. We do
    NOT set RunAtLoad (so installing it never runs an upgrade immediately) and
    we keep StandardErrorPath so you can see why a run failed.
    """
    program: list[str] = ["/bin/bash", "-c", _command_line(brew_path, greedy, cleanup)]

    log_dir = Path.home() / "Library" / "Logs" / "brewjanitor"
    log_dir.mkdir(parents=True, exist_ok=True)

    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": program,
            # Fire once a day at hour:minute. launchd retries a missed run once
            # the machine wakes up, so an upgrade isn't skipped if you were
            # asleep/off at the scheduled time.
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            # Do NOT run an upgrade the moment the job is loaded.
            "RunAtLoad": False,
            "StandardOutPath": str(log_dir / "autoupdate.log"),
            "StandardErrorPath": str(log_dir / "autoupdate.log"),
        },
        fmt=plistlib.FMT_XML,
    )


def install(hour: int, minute: int, greedy: bool, cleanup: bool) -> int:
    """Install (or replace) the autoupdate launchd job and load it.

    No sudo. Writes to ~/Library/LaunchAgents only. Prints the resolved brew
    path and the plist location so you can read it before trusting it.
    """
    brew_path = _resolve_brew()
    if not brew_path:
        _err("brew not found on PATH; install Homebrew first.")
        return 1

    plist_dir = _launch_agents_dir()
    plist_dir.mkdir(parents=True, exist_ok=True)
    target = _plist_path()

    # If a job is already loaded, unload it first so we replace cleanly.
    unload(silent=True)

    data = _build_plist(brew_path, hour, minute, greedy, cleanup)
    target.write_bytes(data)

    _out(f"Resolved brew: {brew_path}")
    _out(f"Wrote plist: {target}")
    _out("The job runs ONLY these commands:")
    _out(f"    {_command_line(brew_path, greedy, cleanup)}")
    _out(f"at {hour:02d}:{minute:02d} every day.")
    _out("Read the plist before trusting it: it is plain XML.")
    _out()

    load_result = subprocess.run(
        ["launchctl", "load", str(target)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if load_result.returncode != 0:
        _err("launchctl load failed:\n" + (load_result.stderr or load_result.stdout))
        return 1
    _out("Loaded. Daily upgrade is scheduled (no sudo used).")
    _out("To stop: brewjanitor autoupdate --remove")
    return 0


def unload(silent: bool = False) -> int:
    """Unload the launchd job if it is loaded. No-op if not loaded."""
    target = _plist_path()
    if not target.exists() and not silent:
        _out("No autoupdate job installed.")
        return 0
    subprocess.run(
        ["launchctl", "unload", str(target)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return 0


def remove() -> int:
    """Unload and delete the autoupdate job. No sudo."""
    unload(silent=True)
    target = _plist_path()
    if target.exists():
        target.unlink()
        _out(f"Removed {target}")
    else:
        _out("No autoupdate job installed.")
    return 0


def _describe_schedule(sched: object) -> str:
    """Render StartCalendarInterval for humans, whatever is actually in there.

    `status` reads a file on disk that this tool does not exclusively own: it
    can be hand-edited, written by an older version, or belong to a different
    job entirely. A missing Hour used to reach an f-string `:02d` and raise
    ValueError, so inspecting a malformed job crashed instead of describing it
    -- the one moment you most want a readable answer.
    """
    if not isinstance(sched, dict):
        return "unknown (no StartCalendarInterval in the plist)"
    hour, minute = sched.get("Hour"), sched.get("Minute", 0)
    if not isinstance(hour, int) or not isinstance(minute, int):
        return f"unknown (malformed StartCalendarInterval: {sched!r})"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return f"out of range (Hour={hour}, Minute={minute})"
    return f"daily at {hour:02d}:{minute:02d}"


def _describe_command(args: object) -> str:
    """Render ProgramArguments, coercing whatever the plist holds to text."""
    if not isinstance(args, list) or not args:
        return "unknown (no ProgramArguments in the plist)"
    return " ".join(str(a) for a in args)


def status() -> int:
    """Show whether the job is installed and its current schedule."""
    target = _plist_path()
    if not target.exists():
        _out("No autoupdate job installed.")
        _out("Install with: brewjanitor autoupdate --install")
        return 0
    try:
        plist = plistlib.loads(target.read_bytes())
    except Exception as exc:
        _err(f"Could not parse {target}: {exc}")
        return 1
    _out(f"Installed: {target}")
    _out(f"Schedule: {_describe_schedule(plist.get('StartCalendarInterval'))}")
    _out(f"Command: {_describe_command(plist.get('ProgramArguments'))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for `python -m brewjanitor.autoupdate` and the subcommand."""
    parser = argparse.ArgumentParser(
        prog="brewjanitor autoupdate",
        description=(
            "Manage a user-level launchd job that runs `brew update && brew "
            "upgrade` on a schedule. No sudo; the job runs as you, from your "
            "own ~/Library/LaunchAgents folder, and only ever calls brew."
        ),
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--install",
        action="store_true",
        help="Install (or replace) the scheduled autoupdate job.",
    )
    g.add_argument("--remove", action="store_true", help="Remove the scheduled job.")
    g.add_argument("--status", action="store_true", help="Show the current job.")
    parser.add_argument(
        "--hour",
        type=int,
        default=8,
        help="Hour (0-23) to run the daily upgrade. Default 8 (08:00).",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=0,
        help="Minute (0-59) to run. Default 0.",
    )
    parser.add_argument(
        "--greedy",
        action="store_true",
        help="Also upgrade casks that auto-update themselves (e.g. browsers). "
        "More thorough but may quit running apps to replace them.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also run `brew cleanup` to remove stale downloads.",
    )
    args = parser.parse_args(argv)

    if args.remove:
        return remove()
    if args.status:
        return status()
    if args.install:
        if not 0 <= args.hour <= 23:
            _err("--hour must be 0-23")
            return 1
        if not 0 <= args.minute <= 59:
            _err("--minute must be 0-59")
            return 1
        return install(args.hour, args.minute, args.greedy, args.cleanup)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
