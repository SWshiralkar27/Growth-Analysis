"""
test_shareholding_nse.py
Version: Step 10c, v0.10.1 (2026-11-06).
Tests for the NSE shareholding-pattern parser in data_fetch.py. The rows copy the real shape
seen in the Diagnostics tab for BAJAJ-AUTO on 2026-11-06 (trimmed to the fields that matter).
No network, no yfinance, no dalal and no pandas are needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import data_fetch as D
import score_card as sc
import test_data_fetch as T


def nse_row(date_, pct, broadcast, public="44.05"):
    return {"broadcastDate": broadcast, "cgTimeStamp": None, "countString": None, "date": date_,
            "desc": "NEW_1", "employeeTrusts": "0", "isin": "INE917I01010", "name": "Bajaj Auto Limited",
            "pr_and_prgrp": pct, "promoterDematNotes": "", "public_val": public, "revisedData": "N",
            "symbol": "BAJAJ-AUTO", "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/SHP_x_WEB.xml",
            "xbrlFileSize": "593.29 KB"}


REAL = [nse_row("30-SEP-2026", "55.95", "15-OCT-2026 15:38:22"),
        nse_row("30-JUN-2026", "55.01", "20-JUL-2026 09:00:46", "44.99")]


class NseParserTests(unittest.TestCase):
    def test_real_rows_give_promoter_holding_and_change(self):
        warnings = []
        out = D._shareholding_metrics(REAL, warnings)
        self.assertEqual(out["promoter_holding"], 55.95)
        self.assertAlmostEqual(out["promoter_holding_change"], 0.94, places=4)

    def test_pledge_and_institutional_stay_blank_and_a_warning_says_why(self):
        warnings = []
        out = D._shareholding_metrics(REAL, warnings)
        for key in ("promoter_pledge_pct", "promoter_pledge_change", "institutional_holding",
                    "institutional_holding_change"):
            self.assertIsNone(out[key], key)
        self.assertTrue(any("no pledge figure" in w for w in warnings))

    def test_row_order_does_not_matter(self):
        out = D._shareholding_metrics(list(reversed(REAL)), [])
        self.assertEqual(out["promoter_holding"], 55.95)
        self.assertAlmostEqual(out["promoter_holding_change"], 0.94, places=4)

    def test_a_revised_filing_for_the_same_quarter_wins_when_broadcast_later(self):
        revised = nse_row("30-SEP-2026", "56.20", "30-OCT-2026 10:00:00")
        out = D._shareholding_metrics(REAL + [revised], [])
        self.assertEqual(out["promoter_holding"], 56.2)
        self.assertAlmostEqual(out["promoter_holding_change"], 1.19, places=4)

    def test_an_older_duplicate_does_not_override_the_newer_filing(self):
        older = nse_row("30-SEP-2026", "50.00", "01-OCT-2026 10:00:00")
        out = D._shareholding_metrics(REAL + [older], [])
        self.assertEqual(out["promoter_holding"], 55.95)

    def test_one_quarter_gives_holding_but_no_change_and_a_warning(self):
        warnings = []
        out = D._shareholding_metrics(REAL[:1], warnings)
        self.assertEqual(out["promoter_holding"], 55.95)
        self.assertIsNone(out["promoter_holding_change"])
        self.assertTrue(any("only one quarter" in w for w in warnings))

    def test_unusable_rows_are_skipped_not_guessed(self):
        rows = [nse_row("30-SEP-2026", "-", "15-OCT-2026 15:38:22"),
                nse_row("30-JUN-2026", "155.0", "20-JUL-2026 09:00:46"),
                nse_row("not a date", "55.0", "20-JUL-2026 09:00:46")]
        warnings = []
        out = D._shareholding_metrics(rows, warnings)
        self.assertIsNone(out["promoter_holding"])
        self.assertTrue(any("no usable promoter holding" in w for w in warnings))

    def test_the_old_row_shape_still_uses_the_tolerant_parser(self):
        rows = [{"date": "2026-03-31", "promoter_holding": 50.0, "promoter_pledge": 2.0, "fii": 20.0, "dii": 10.0},
                {"date": "2025-12-31", "promoter_holding": 51.0, "promoter_pledge": 1.0, "fii": 18.0, "dii": 9.0}]
        out = D._shareholding_metrics(rows, [])
        self.assertEqual(out["promoter_holding"], 50.0)
        self.assertAlmostEqual(out["promoter_holding_change"], -1.0)
        self.assertEqual(out["promoter_pledge_pct"], 2.0)


class EndToEndTests(T.FetchCase):
    def setUp(self):
        super().setUp()
        D.dalal = T.fake_dalal(shareholding=lambda t: REAL)

    def test_shareholding_now_counts_as_a_governance_feed(self):
        rec = D.get_financial_data("TCS")
        self.assertEqual(rec["promoter_holding"], 55.95)
        self.assertIsNone(rec["promoter_pledge_pct"])
        self.assertEqual(sorted(rec["governance_sources"]), ["dalal.announcements", "dalal.shareholding"])

    def test_both_feeds_lift_the_unverified_governance_cap_in_the_scorer(self):
        rec = D.get_financial_data("TCS")
        names = ["Business Catalysts", "Market Expansion", "Order Book Growth", "Competitive Advantage"]
        llm = {"factors": [{"factor": n, "score": 100, "confidence_applied": "high"} for n in names],
               "governance": [], "documents_used": ["TCS_2026_05_12_Concall_Transcript.pdf"],
               "pages_included": 10}
        notes = " | ".join(sc.compute_score(rec, llm)["breakdown"]["governance_notes"])
        self.assertNotIn("Governance unchecked", notes)


if __name__ == "__main__":
    unittest.main()