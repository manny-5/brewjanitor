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
from pathlib import Path


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


PARTIAL_STDERR = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})
    from brewjanitor.streams import out
    # A partial line (no newline) sits in stderr's buffer. Anything that is not
    # our own helpers can leave one there: a library, warnings, a progress
    # indicator. out() has to flush stderr before writing, or its line jumps
    # ahead of text that was produced first.
    sys.stderr.write("first: partial progress")
    out("second: a result")
    sys.stderr.write("\\n")
    """
)


class TestOutFlushesStderrFirst(unittest.TestCase):
    """Isolates out()'s leading stderr flush.

    err() already flushes stderr on the way out, so back-to-back helper calls
    would stay ordered without it. The flush earns its place only when stderr
    holds text that something else left unflushed -- which is exactly when
    getting it wrong is hardest to debug.
    """

    def setUp(self):
        import pathlib
        self.root = str(pathlib.Path(__file__).resolve().parent.parent)

    def test_unflushed_stderr_text_still_comes_out_first(self):
        # Compared by position in the raw byte stream, not by line: the partial
        # write has no newline, so correct ordering puts the two on one line.
        proc = subprocess.run(
            [sys.executable, "-c", PARTIAL_STDERR.format(root=self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        raw = proc.stdout
        self.assertIn("first: partial progress", raw)
        self.assertIn("second: a result", raw)
        self.assertLess(
            raw.index("first: partial progress"), raw.index("second: a result"),
            f"stdout jumped ahead of unflushed stderr text: {raw!r}",
        )


class TestWholePackageUsesOrderedStreams(unittest.TestCase):
    """Item 3 is only fixed if nothing bypasses the helpers.

    One stray `print()` in a module that also writes to the other stream is
    enough to bring the scrambling back, and it would not show up in any
    behavioural test that captures only one stream.
    """

    def _package_modules(self):
        pkg = Path(__import__("brewjanitor").__file__).parent
        return [p for p in sorted(pkg.glob("*.py")) if p.name != "streams.py"]

    def test_no_module_calls_print_directly(self):
        import ast
        offenders = []
        for path in self._package_modules():
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "print":
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [], f"raw print() found at: {offenders}")

    def test_modules_that_produce_output_import_the_helpers(self):
        for path in self._package_modules():
            src = path.read_text()
            if "_out(" in src or "_err(" in src:
                self.assertIn("from .streams import", src, f"{path.name}")
