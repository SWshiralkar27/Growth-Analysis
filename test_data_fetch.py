"""
test_data_fetch.py
Version: Step 8, v0.8.0 (2026-11-04).
Tests for data_fetch.py (the recovered collector) with FAKE yfinance and dalal modules, and
for the pipeline's real fetch stage. The announcement-window tests are ported from the old
test_patches.py. No network, no yfinance, no dalal and no pandas are needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
The live providers (Yahoo, NSE, BSE, and dalal's real response shapes) can only be proved
in the running app.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import data_fetch as D
import pipeline
from errors import PermanentError


# ------------------------------------------------------------ fake providers
class _Row:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


class _Loc:
    def __init__(self, frame):
        self.frame = frame

    def __getitem__(self, key):
        if isinstance(key, tuple):
            return self.frame.cells[key]
        return _Row(self.frame.rows[key])


class Frame:
    """Just enough of a pandas DataFrame for the collector (newest column first)."""

    def __init__(self, rows=None, cells=None, columns=None):
        self.rows = rows or {}
        self.cells = cells or {}
        self.index = list(self.rows) or sorted({p for p, _ in self.cells})
        self.columns = columns or sorted({c for _, c in self.cells})
        self.empty = not (self.rows or self.cells)
        self.loc = _Loc(self)


INFO = {"marketCap": 1.0e12, "revenueGrowth": 0.12, "ebitdaMargins": 0.20, "operatingMargins": 0.18,
        "debtToEquity": 45.2, "returnOnEquity": 0.16, "currentRatio": 1.5, "trailingPE": 20.0,
        "forwardPE": 18.0, "sector": "Industrials", "industry": "Aerospace & Defense"}
COLS = ["2026-03-31", "2025-03-31", "2024-03-31"]


class FakeTicker:
    def __init__(self, info=None, broken=False):
        self._info, self._broken = info, broken
        self.financials = Frame(rows={
            "Total Revenue": [1200, 1000, 900], "EBITDA": [240, 180, 150], "EBIT": [200, 150, 120],
            "Net Income": [150, 100, 80], "Interest Expense": [10, 12, 15],
            "Cost Of Revenue": [700, 600, 540]}, columns=COLS)
        self.balance_sheet = Frame(rows={
            "Accounts Receivable": [200, 150], "Inventory": [100, 80],
            "Accounts Payable": [90, 70], "Total Debt": [300, 350]}, columns=COLS[:2])
        self.cashflow = Frame(rows={"Operating Cash Flow": [220, 170]}, columns=COLS[:2])
        self.eps_trend = Frame(cells={("0y", "current"): 12.0, ("0y", "90daysAgo"): 10.0})
        self.eps_revisions = Frame(cells={("0y", "upLast30days"): 4, ("0y", "downLast30days"): 1})
        if broken:      # an unknown symbol gives nothing at all, not even statements
            self.financials = self.balance_sheet = self.cashflow = None
            self.eps_trend = self.eps_revisions = None

    @property
    def info(self):
        if self._broken:
            raise RuntimeError("404 not found")
        return self._info


class FakeYF:
    def __init__(self, tickers):
        self.tickers, self.calls = tickers, []

    def Ticker(self, symbol):
        self.calls.append(symbol)
        return self.tickers.get(symbol, FakeTicker(broken=True))


TODAY = date(2026, 10, 24)


def ann_row(days_ago, desc, url="https://nsearchives.nseindia.com/corporate/XYZ_00000000_file.pdf", size="1.2 MB"):
    d = TODAY - timedelta(days=days_ago)
    return {"an_dt": d.strftime("%d-%b-%Y 11:20:56"), "size": size, "attchmnt": url, "desc": desc}


def fake_dalal(**over):
    ns = types.SimpleNamespace(
        lookup=lambda t: [{"scripcode": "532977"}],
        fundamentals=lambda code: {"periods": [{"period": "FY24-25", "revenue": 1000, "opm_pct": 15},
                                               {"period": "FY23-24", "revenue": 800, "opm_pct": 14}]},
        meta=lambda code: {"roe": 16.0, "pe": 20.0, "market_cap": 1.0e12, "sector": "Industrials"},
        shareholding=lambda t: [
            {"date": "2026-03-31", "promoter_holding": 50.0, "promoter_pledge": 2.0, "fii": 20.0, "dii": 10.0},
            {"date": "2025-12-31", "promoter_holding": 51.0, "promoter_pledge": 1.0, "fii": 18.0, "dii": 9.0}],
        announcements=lambda t: [ann_row(20, "Intimation of resignation of Chief Financial Officer of the Company")],
        block_deals=lambda: [{"symbol": "XYZ", "qty": 1}, {"symbol": "TCS", "qty": 5}],
    )
    for k, v in over.items():
        if v is None:
            delattr(ns, k)
        else:
            setattr(ns, k, v)
    return ns


class FetchCase(unittest.TestCase):
    """Patches the providers, the cache folder and the sleeps for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for name, value in (("FIN_CACHE_DIR", os.path.join(self._tmp.name, "fin")),
                            ("PROVIDER_RETRY_DELAY", 0.0),
                            ("BSE_CODE_FILE", os.path.join(self._tmp.name, "none.csv"))):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("yf", FakeYF({"TCS.NS": FakeTicker(INFO)})), ("dalal", fake_dalal()),
                              ("bfinance", None)):
            p = mock.patch.object(D, target, value)
            p.start()
            self.addCleanup(p.stop)


# --------------------------------------------------------------- helpers
class HelperTests(unittest.TestCase):
    def test_clean_ticker_and_numbers(self):
        self.assertEqual(D._clean_ticker(" tcs.ns "), "TCS")
        self.assertEqual(D._clean_ticker("bajaj-auto"), "BAJAJ-AUTO")
        for bad in (None, "", "nan", float("nan"), True, "N/A", "abc"):
            self.assertIsNone(D._num(bad), bad)
        self.assertEqual(D._num("1,234.5"), 1234.5)

    def test_debt_to_equity_units(self):
        self.assertAlmostEqual(D._de_from_percent(45.2), 0.452)
        self.assertIsNone(D._de_from_percent(-10))
        warnings = []
        self.assertIsNone(D._de_from_dalal(0.4, warnings))          # unit unknown: ignored, with a warning
        self.assertTrue(any("DALAL_DE_UNIT" in w for w in warnings))
        with mock.patch.object(config, "DALAL_DE_UNIT", "percent"):
            self.assertAlmostEqual(D._de_from_dalal(40, []), 0.4)
        with mock.patch.object(config, "DALAL_DE_UNIT", "ratio"):
            self.assertAlmostEqual(D._de_from_dalal(0.4, []), 0.4)

    def test_jsonable_makes_records_storable(self):
        class NpLike:
            def __float__(self):
                return 2.5
        out = D._jsonable({"d": date(2026, 1, 2), "n": NpLike(), "nan": float("nan"), "s": {1, 2},
                           "inf": float("inf"), "t": (1, 2), "k": None, "b": True})
        json.dumps(out, allow_nan=False)
        self.assertEqual(out["d"], "2026-01-02")
        self.assertEqual(out["n"], 2.5)
        self.assertIsNone(out["nan"])
        self.assertIsNone(out["inf"])
        self.assertEqual(out["t"], [1, 2])
        self.assertIs(out["b"], True)


class PeriodTests(unittest.TestCase):
    def test_parse_periods(self):
        self.assertEqual(D._parse_period("FY24-25"), ("FY", 2025))
        self.assertEqual(D._parse_period("FY 2024-2025"), ("FY", 2025))
        self.assertEqual(D._parse_period("Dec-25"), ("M", 12, 2025))
        self.assertIsNone(D._parse_period("whatever"))
        self.assertIsNone(D._parse_period(None))

    def test_revenue_growth_prefers_annual_then_same_quarter(self):
        annual = [{"period": "FY24-25", "revenue": 1000}, {"period": "FY23-24", "revenue": 800}]
        self.assertAlmostEqual(D._compute_revenue_growth(annual), 0.25)
        quarters = [{"period": "Dec-25", "revenue": 330}, {"period": "Dec-24", "revenue": 300},
                    {"period": "Sep-25", "revenue": 999}]
        self.assertAlmostEqual(D._compute_revenue_growth(quarters), 0.10)
        self.assertIsNone(D._compute_revenue_growth([{"period": "FY24-25", "revenue": 1000}]))

    def test_latest_period_is_chosen_by_label_not_list_order(self):
        rows = [{"period": "FY22-23", "x": 1}, {"period": "FY24-25", "x": 3}, {"period": "FY23-24", "x": 2}]
        self.assertEqual(D._latest_period_row(rows)["x"], 3)


# ------------------------------------------------------- announcements (ported)
class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ann_row(1900, "Resignation of Chief Financial Officer"),
            ann_row(20, "Intimation of resignation of Chief Financial Officer of the Company"),
            ann_row(30, "Outcome of Board Meeting - results for the quarter", url="https://x/XYZ_intiresignationofcfo.pdf"),
            ann_row(40, "Resignation of Statutory Auditor"),
            ann_row(50, "Press release - strong quarter, said the Chief Financial Officer"),
            ann_row(60, "Intimation of invocation of pledge by lender"),
        ]
        self.out = D._announcement_flags(self.rows, today=TODAY, window_days=365)

    def test_old_announcements_are_outside_the_window(self):
        self.assertEqual(self.out["ann_cfo_exit"], 1)

    def test_url_slugs_cannot_trigger_a_hit(self):
        self.assertTrue(all("intiresignationofcfo" not in (e["text"] or "") for e in self.out["ann_events"]))

    def test_recent_auditor_resignation_and_pledge_invocation_counted(self):
        self.assertEqual((self.out["ann_auditor_resignation"], self.out["ann_pledge_invocation"]), (1, 1))

    def test_events_are_readable(self):
        self.assertTrue(all("http" not in e["text"] and e["date"] for e in self.out["ann_events"]))

    def test_window_metadata(self):
        self.assertEqual((self.out["ann_window_days"], self.out["ann_rows_in_window"]), (365, 5))

    def test_without_any_date_everything_counts_and_it_is_flagged(self):
        out = D._announcement_flags([{"desc": "Resignation of Chief Financial Officer"}], today=TODAY)
        self.assertEqual(out["ann_cfo_exit"], 1)
        self.assertIsNone(out["ann_window_days"])

    def test_undated_rows_are_skipped_when_dated_rows_exist(self):
        out = D._announcement_flags(self.rows + [{"desc": "Resignation of Statutory Auditor (no date)"}], today=TODAY)
        self.assertEqual((out["ann_undated_rows"], out["ann_auditor_resignation"]), (1, 1))


# ------------------------------------------------- statements and estimates
class StatementTests(unittest.TestCase):
    def test_trends_are_computed_from_the_statements(self):
        warnings = []
        m = D._statement_metrics(FakeTicker(INFO), warnings)
        self.assertAlmostEqual(m["revenue_growth_annual"], 0.2)
        self.assertAlmostEqual(m["revenue_acceleration"], 0.2 - 100 / 900)
        self.assertAlmostEqual(m["ebitda_margin_change"], 0.20 - 0.18)
        self.assertAlmostEqual(m["ebit_growth"], 50 / 150)
        self.assertAlmostEqual(m["net_margin"], 0.125)
        self.assertAlmostEqual(m["interest_coverage"], 20.0)
        self.assertAlmostEqual(m["ocf_to_ebitda"], 220 / 240)
        self.assertAlmostEqual(m["receivable_days"], 200 / 1200 * 365)
        self.assertAlmostEqual(m["debt_change_pct"], -50 / 350)
        self.assertEqual(m["latest_fy_end"], "2026-03-31")
        self.assertEqual(warnings, [])

    def test_zero_interest_means_debt_free_for_coverage(self):
        t = FakeTicker(INFO)
        t.financials.rows["Interest Expense"] = [0, 0, 0]
        m = D._statement_metrics(t, [])
        self.assertTrue(m["no_interest_expense"])
        self.assertIsNone(m["interest_coverage"])

    def test_missing_rows_give_none_not_guesses(self):
        t = FakeTicker(INFO)
        t.financials = Frame(rows={"Total Revenue": [1200, 1000]}, columns=COLS[:2])
        m = D._statement_metrics(t, [])
        self.assertIsNone(m["ebit_growth"])
        self.assertIsNone(m["revenue_acceleration"])      # needs three years

    def test_estimate_revisions(self):
        m = D._estimate_metrics(FakeTicker(INFO), [])
        self.assertAlmostEqual(m["eps_revision_90d_pct"], 0.2)
        self.assertEqual(m["eps_net_revisions_30d"], 3)


# -------------------------------------------------------- end to end, fakes
class GetFinancialDataTests(FetchCase):
    def test_full_record_from_yfinance_with_dalal_governance(self):
        rec = D.get_financial_data("TCS", use_cache=False) if "use_cache" in D.get_financial_data.__code__.co_varnames else D.get_financial_data("TCS")
        self.assertEqual(rec["ticker"], "TCS")
        self.assertEqual(rec["data_quality"], "complete")
        self.assertIn("yfinance", rec["providers_used"])
        self.assertAlmostEqual(rec["debt_to_equity"], 0.452)
        self.assertEqual(rec["sector"], "Industrials")
        self.assertEqual(rec["yf_symbol"], "TCS.NS")
        self.assertAlmostEqual(rec["revenue_acceleration"], 0.2 - 100 / 900)
        self.assertAlmostEqual(rec["eps_revision_90d_pct"], 0.2)
        self.assertEqual(rec["promoter_holding"], 50.0)
        self.assertAlmostEqual(rec["promoter_holding_change"], -1.0)
        self.assertEqual(rec["ann_cfo_exit"], 1)
        self.assertEqual(sorted(rec["governance_sources"]), ["dalal.announcements", "dalal.shareholding"])
        self.assertEqual(len(rec["block_deals_recent"]), 1)
        json.dumps(rec, allow_nan=False)                         # the store must be able to save it

    def test_conflicting_providers_are_flagged_not_hidden(self):
        rec = D.get_financial_data("TCS")
        text = json.dumps(rec.get("warnings", [])) + json.dumps(rec.get("conflicts", ""), default=str)
        self.assertIn("revenue_growth", text)     # yfinance says 12%, dalal's periods say 25%

    def test_second_call_comes_from_the_cache(self):
        D.get_financial_data("TCS")
        calls_before = len(D.yf.calls)
        again = D.get_financial_data("TCS")
        self.assertEqual(len(D.yf.calls), calls_before)
        self.assertEqual(again["ticker"], "TCS")

    def test_cache_can_be_switched_off(self):
        D.get_financial_data("TCS")
        n = len(D.yf.calls)
        with mock.patch.object(config, "FIN_CACHE_ENABLED", False):
            D.get_financial_data("TCS")
        self.assertGreater(len(D.yf.calls), n)

    def test_the_symbol_with_more_data_wins(self):
        thin = dict(list(INFO.items())[:3])
        D.yf.tickers = {"TCS.NS": FakeTicker(thin), "TCS.BO": FakeTicker(INFO)}
        rec = D.get_financial_data("TCS")
        self.assertEqual(rec["yf_symbol"], "TCS.BO")

    def test_a_missing_dalal_function_is_a_warning_without_retries(self):
        calls = {"n": 0}
        D.yf.tickers = {"TCS.NS": FakeTicker(INFO), "TCS.BO": FakeTicker(INFO)}   # no retries from yfinance
        D.dalal = fake_dalal(announcements=None)
        with mock.patch.object(D.time, "sleep", side_effect=lambda s: calls.__setitem__("n", calls["n"] + 1)):
            rec = D.get_financial_data("TCS")
        self.assertEqual(calls["n"], 0)
        self.assertNotIn("dalal.announcements", rec["governance_sources"])
        self.assertTrue(any("announcements" in w for w in rec["warnings"]))

    def test_transient_provider_errors_are_retried(self):
        state = {"n": 0}

        class Flaky(FakeTicker):
            @property
            def info(self):
                state["n"] += 1
                if state["n"] < 3:
                    raise ConnectionError("reset")
                return INFO
        D.yf.tickers = {"TCS.NS": Flaky(INFO)}
        D.dalal = None
        with mock.patch.object(D.time, "sleep"):
            rec = D.get_financial_data("TCS")
        self.assertEqual(rec["yf_symbol"], "TCS.NS")

    def test_all_providers_failing_returns_none(self):
        D.yf = FakeYF({})
        D.dalal = None
        with mock.patch.object(D.time, "sleep"):
            self.assertIsNone(D.get_financial_data("TCS"))

    def test_very_thin_data_is_flagged_partial_or_insufficient(self):
        D.yf = FakeYF({"TCS.NS": FakeTicker({"marketCap": 5e9, "trailingPE": 12.0})})
        D.dalal = None
        with mock.patch.object(D.time, "sleep"):
            rec = D.get_financial_data("TCS")
        self.assertIn(rec["data_quality"], ("partial", "insufficient"))


# ------------------------------------------------------------ pipeline stage
class FetchStageTests(unittest.TestCase):
    def test_returns_financials_and_empty_price_history(self):
        fetch = pipeline.make_real_fetch(lambda t: {"ticker": t, "data_quality": "partial"})
        out = fetch("TCS")
        self.assertEqual(out["financials"]["data_quality"], "partial")
        self.assertEqual(out["price_history"], {})

    def test_no_data_and_insufficient_data_are_permanent_errors(self):
        with self.assertRaises(PermanentError):
            pipeline.make_real_fetch(lambda t: None)("TCS")
        with self.assertRaises(PermanentError) as err:
            pipeline.make_real_fetch(lambda t: {"data_quality": "insufficient"})("TCS")
        self.assertIn("insufficient", str(err.exception))

    def test_demo_tickers_keep_the_stub_and_never_call_the_real_fetch(self):
        calls = []
        stages, _ = pipeline.make_stub_stages(fetch_fn=lambda t: calls.append(t) or {"data_quality": "complete"})
        run = pipeline.run_pipeline(["FAKE", "STALE429", "NOPRIOR429", "FAILPERM"], 30, stages)
        self.assertEqual(calls, [])
        self.assertEqual([r.status for r in run.results], ["ok", "stale", "partial", "failed"])

    def test_a_real_ticker_with_insufficient_data_fails_and_is_listed_last(self):
        stages, _ = pipeline.make_stub_stages(fetch_fn=lambda t: {"data_quality": "insufficient"})
        run = pipeline.run_pipeline(["TCS", "FAKE"], 30, stages)
        by = {r.ticker: r for r in run.results}
        self.assertEqual(by["TCS"].status, "failed")
        self.assertIn("insufficient", by["TCS"].stage_errors["fetch"])
        self.assertIsNone(by["TCS"].rank)
        self.assertEqual(by["FAKE"].status, "ok")

    def test_data_quality_is_shown_in_the_score_notes(self):
        rec = {"data_quality": "partial", "providers_used": ["yfinance", "dalal"],
               "missing_fields": ["forward_pe"], "warnings": ["w1", "w2"], "revenue_growth": 0.1}
        b = pipeline.score_stage_v21("TCS", None, rec, {}, {}, 30)
        text = " | ".join(b.labels)
        self.assertIn("Data quality: partial (providers: yfinance, dalal; 1 core field(s) missing; 2 warning(s))", text)
        self.assertNotIn("STUB data", text)

    def test_real_financials_change_the_score_from_the_prior(self):
        rec = {"revenue_growth": 0.2, "ebitda_margin": 0.2, "debt_to_equity": 0.3, "data_quality": "complete"}
        stub = pipeline.score_stage_v21("T", None, {"stub": True}, {}, {}, 30)
        real = pipeline.score_stage_v21("T", None, rec, {}, {}, 30)
        self.assertGreater(real.components["fin_level"]["points"], stub.components["fin_level"]["points"])


if __name__ == "__main__":
    unittest.main()
