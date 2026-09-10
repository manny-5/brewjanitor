"""Tests for piece 5 — report: the couldn't-be-replaced CSV."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from brewjanitor.inventory import App
from brewjanitor.report import FIELDS, ReportEntry, write_report


def entry(name="Thing.app", reason="not installable via Homebrew"):
    return ReportEntry(
        app=App(name=name, path=f"/Applications/{name}", bundle_id="com.example.thing"),
        brew_name="", brew_kind="", reason=reason,
    )


class TestWriteReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _read(self, path):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.reader(fh))

    def test_writes_a_header_and_one_row_per_entry(self):
        out = self.root / "r.csv"
        count = write_report([entry("A.app"), entry("B.app")], out)
        rows = self._read(out)
        self.assertEqual(count, 2)
        self.assertEqual(rows[0], FIELDS)
        self.assertEqual([r[0] for r in rows[1:]], ["A.app", "B.app"])

    def test_an_empty_report_still_writes_the_header(self):
        out = self.root / "empty.csv"
        self.assertEqual(write_report([], out), 0)
        self.assertEqual(self._read(out), [FIELDS])

    def test_creates_missing_parent_directories(self):
        out = self.root / "nested" / "deeper" / "r.csv"
        write_report([entry()], out)
        self.assertTrue(out.exists())

    def test_overwrites_rather_than_appending(self):
        out = self.root / "r.csv"
        write_report([entry("A.app"), entry("B.app")], out)
        write_report([entry("C.app")], out)
        rows = self._read(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], "C.app")

    def test_fields_containing_commas_and_quotes_survive_a_round_trip(self):
        messy = ReportEntry(
            app=App(name='Wei"rd, App.app', path="/Applications/w.app", bundle_id="a,b"),
            brew_name="", brew_kind="", reason='has "quotes", and commas',
        )
        out = self.root / "messy.csv"
        write_report([messy], out)
        row = self._read(out)[1]
        self.assertEqual(row[0], 'Wei"rd, App.app')
        self.assertEqual(row[5], 'has "quotes", and commas')

    def test_accepts_a_string_path(self):
        out = self.root / "s.csv"
        self.assertEqual(write_report([entry()], str(out)), 1)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
