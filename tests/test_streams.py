"""Tests for ordered stdout/stderr writes.

The split (results on stdout, progress on stderr) is deliberate so that
`brewjanitor | grep ...` works. The bug was ordering: the two streams buffer
differently, so when both land in the same place the lines arrived scrambled --
a summary printed above the results it summarised.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest


PROGRAM = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from brewjanitor.streams import err, out
    err("first: progress")
    out("second: a result")
    err("third: summary")
    out("fourth: another result")
    """
)

NAIVE = textwrap.dedent(
    """
    import sys
    print("first: progress", file=sys.stderr)
    print("second: a result")
    print("third: summary", file=sys.stderr)
    print("fourth: another result")
    """
)

EXPECTED = ["first: progress", "second: a result", "third: summary", "fourth: another result"]


def _merged(source: str) -> list[str]:
    """Run source with stdout and stderr merged into one pipe, like `2>&1 | cat`."""
    import os
    proc = subprocess.run(
        [sys.executable, "-c", source],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return [l for l in proc.stdout.splitlines() if l.strip()]


class TestOrdering(unittest.TestCase):
    def setUp(self):
        import pathlib
        self.root = str(pathlib.Path(__file__).resolve().parent.parent)

    def test_interleaved_order_matches_program_order_through_a_pipe(self):
        self.assertEqual(_merged(PROGRAM.format(root=self.root)), EXPECTED)

    def test_the_naive_version_really_does_scramble(self):
        # Guards the premise: if plain print() ever starts ordering correctly
        # through a pipe, this fix is obsolete and should be reconsidered
        # rather than left in place as cargo.
        self.assertNotEqual(_merged(NAIVE), EXPECTED)

    def test_results_still_go_to_stdout_only(self):
        # The split has to survive the fix, or piping breaks.
        proc = subprocess.run(
            [sys.executable, "-c", PROGRAM.format(root=self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.stdout.split(), "second: a result fourth: another result".split())
        self.assertIn("first: progress", proc.stderr)
        self.assertNotIn("progress", proc.stdout)


SUBPROCESS_PROGRAM = textwrap.dedent(
    """
    import subprocess, sys
    sys.path.insert(0, {root!r})
    from brewjanitor.streams import out
    out("before the child")
    subprocess.run(["echo", "from the child"])
    out("after the child")
    """
)


class TestOrderingAgainstAChildProcess(unittest.TestCase):
    """The case that makes out()'s own flush necessary.

    Under --apply, brew's output goes straight to the inherited file
    descriptor, bypassing Python's buffer entirely. Without flushing after each
    write, our buffered lines surface *after* output from a child that ran
    later.
    """

    def setUp(self):
        import pathlib
        self.root = str(pathlib.Path(__file__).resolve().parent.parent)

    def test_our_output_stays_ordered_around_a_child_writing_to_the_same_fd(self):
        self.assertEqual(
            _merged(SUBPROCESS_PROGRAM.format(root=self.root)),
            ["before the child", "from the child", "after the child"],
        )


MIXED_PROGRAM = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from brewjanitor.streams import err
    print("bare print to stdout")
    err("then a stderr line")
    print("bare print again")
    """
)


class TestOrderingAgainstBarePrints(unittest.TestCase):
    """Why err() flushes stdout even though out() already does.

    Not every stdout write in the package goes through out() -- autoupdate.py
    prints directly, and any future code might too. err() flushing stdout first
    keeps those ordered as well, so the ordering guarantee does not depend on
    every caller remembering to use the helper.
    """

    def setUp(self):
        import pathlib
        self.root = str(pathlib.Path(__file__).resolve().parent.parent)

    def test_a_bare_print_is_still_ordered_against_a_following_err(self):
        self.assertEqual(
            _merged(MIXED_PROGRAM.format(root=self.root)),
            ["bare print to stdout", "then a stderr line", "bare print again"],
        )
