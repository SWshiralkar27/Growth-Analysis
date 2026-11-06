"""
test_yf_probe.py
Version: Step 10d, v0.10.2 (2026-11-06).
Tests for yf_probe.py with FAKE Yahoo objects. No network, no yfinance package needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import yf_probe as P
import test_data_fetch as T

QCOLS = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]


def ticker_with(info_over=None, quarterly=None):
    info = dict(T.INFO)
    info.update(info_over or {})
    t = T.FakeTicker(info)
    if quarterly is not None:
        t.quarterly_financials = T.Frame(rows={"Total Revenue": quarterly}, columns=QCOLS)
    return t


class ProbeTests(unittest.TestCase):
    def test_computes_annual_growth_and_ocf_to_ebitda(self):
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": ticker_with()}))
        self.assertTrue(out["found"])
        self.assertEqual(out["symbol"], "XYZ.NS")
        self.assertAlmostEqual(out["computed"]["annual_revenue_growth"], 0.2)
        self.assertAlmostEqual(out["computed"]["ocf_to_ebitda"], 220 / 240)
        self.assertEqual(out["row_names_used"]["Total Revenue"], "Total Revenue")

    def test_reports_when_yahoo_growth_matches_the_annual_figure(self):
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": ticker_with({"revenueGrowth": 0.2})}))
        self.assertIn("matches the annual growth", out["readings"][0])

    def test_reports_when_yahoo_growth_matches_the_latest_quarter(self):
        t = ticker_with({"revenueGrowth": 0.5}, quarterly=[150, 140, 130, 125, 100])
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": t}))
        self.assertAlmostEqual(out["computed"]["latest_quarter_yoy"], 0.5)
        self.assertIn("latest-quarter year-on-year", out["readings"][0])

    def test_reports_when_it_matches_nothing(self):
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": ticker_with({"revenueGrowth": 0.9})}))
        self.assertIn("none of the growth figures", out["readings"][0])

    def test_flags_mismatched_statement_dates(self):
        t = ticker_with()
        t.cashflow = T.Frame(rows={"Operating Cash Flow": [220, 170]}, columns=["2025-03-31", "2024-03-31"])
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": t}))
        self.assertTrue(any("different dates" in line for line in out["readings"]))

    def test_inr_values_are_shown_in_crore(self):
        t = ticker_with({"financialCurrency": "INR"})
        t.financials.rows["Total Revenue"] = [1.2e10, 1.0e10, 9.0e9]
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": t}))
        self.assertEqual(out["unit"], "crore INR")
        self.assertEqual(out["annual"]["rows"]["Total Revenue"][0], 1200.0)

    def test_falls_back_to_the_bse_symbol(self):
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.BO": ticker_with()}))
        self.assertEqual(out["symbol"], "XYZ.BO")
        self.assertTrue(any("XYZ.NS" in e for e in out["errors"]))

    def test_nothing_found_is_data_not_a_crash(self):
        out = P.probe("XYZ", module=T.FakeYF({}))
        self.assertFalse(out["found"])
        self.assertEqual(len(out["errors"]), 2)

    def test_missing_rows_give_none_not_guesses(self):
        t = ticker_with()
        t.financials = T.Frame(rows={"Total Revenue": [1200, 1000]}, columns=["2026-03-31", "2025-03-31"])
        t.cashflow = None
        out = P.probe("XYZ", module=T.FakeYF({"XYZ.NS": t}))
        self.assertIsNone(out["computed"]["ocf_to_ebitda"])
        self.assertTrue(any("cannot be computed" in line for line in out["readings"]))

    def test_not_installed_is_reported(self):
        with mock.patch.dict(sys.modules, {"yfinance": None}):
            out = P.probe("XYZ")
        self.assertFalse(out["installed"])


if __name__ == "__main__":
    unittest.main()