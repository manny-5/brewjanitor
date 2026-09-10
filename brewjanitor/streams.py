"""Ordered writes to stdout and stderr.

brewjanitor splits its output: results go to stdout so `brewjanitor | grep ...`
works, while progress and summaries go to stderr so they do not pollute that
pipe. The split is worth keeping. The problem is that the two streams buffer
differently -- stdout block-buffers when it is not a terminal, stderr does not
-- so whenever both land in the same place the lines arrive out of order. In a
real run the "N app(s) could not be replaced" summary appeared *above* the
results it was summarising.

Flushing the other stream before writing, and this one after, makes the
interleaved order match program order wherever either stream is pointed. The
cost is a flush per line, which is irrelevant at these volumes.
"""

from __future__ import annotations

import sys


def out(message: str = "") -> None:
    """Write a result line to stdout, ordered against stderr."""
    sys.stderr.flush()
    print(message)
    sys.stdout.flush()


def err(message: str = "") -> None:
    """Write progress or summary text to stderr, ordered against stdout."""
    sys.stdout.flush()
    print(message, file=sys.stderr)
    sys.stderr.flush()
