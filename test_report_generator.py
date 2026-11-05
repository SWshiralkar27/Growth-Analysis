"""
test_report_generator.py
Version: Step 9, v0.9.0 (2026-11-05).
Tests for report_generator.py (the recovered report format, which must not change without the
owner's approval) and for the pipeline's report, dashboard, zip and details helpers.
golden_report_v21.html and golden_dashboard_v21.html were produced by the RECOVERED ORIGINAL
report_generator.py from the inputs in golden_inputs(); the tests fail if the format drifts.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import pipeline
import report_generator as R
import score_card as sc
from store import Store


def normalise(html):
    html = re.sub(r"Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}", "Generated <TS>", html)
    return re.sub(r"Generated \d{4}-\d{2}-\d{2}<", "Generated <DATE><", html)


def golden_inputs():
    financials = {"market_cap": 1.2e12, "revenue_growth": 0.12, "ebitda_margin": 0.2, "debt_to_equity": 0.4,
                  "interest_coverage": 12.0, "ocf_to_ebitda": 0.8, "promoter_holding": 50.0,
                  "promoter_pledge_pct": 2.0, "eps_revision_90d_pct": 0.05,
                  "ann_auditor_resignation": 0, "ann_cfo_exit": 1, "ann_pledge_invocation": 0,
                  "ann_events": [{"date": "2026-09-01", "event": "ann_cfo_exit",
                                  "text": "Intimation of resignation of Chief Financial Officer"}]}
    ev = lambda page, quote, ok: {"document": "TCS_2026_05_12_AnnualReport.pdf", "page": page,
                                  "quote": quote, "verified": ok}
    llm = {"factors": [
        {"factor": "Business Catalysts", "score": 70, "confidence_applied": "high",
         "evidence": [ev(3, "Order book at a record backlog", True), ev(9, "Capacity expansion will commission", True)]},
        {"factor": "Market Expansion", "score": 55, "confidence_applied": "medium",
         "evidence": [ev(4, "New export markets opened & served", False)]},
        {"factor": "Order Book Growth", "score": 50, "confidence_applied": "none", "evidence": []},
        {"factor": "Competitive Advantage", "score": 60, "confidence_applied": "low", "evidence": []}],
        "governance": [{"flag": "cfo_exit", "raised": True, "verified": True, "evidence": [ev(7, "CFO resigned w.e.f. 1 June", True)]},
                       {"flag": "auditor_resignation", "raised": False, "verified": False, "evidence": []}],
        "governance_coverage": ["auditor_report", "pledge"],
        "key_strengths": ["Strong order book <b>growth</b>"], "key_risks": ["Execution & timing"]}
    score_result = {"score": 63.4, "breakdown": {
        "scoring_model": "v2.1", "llm_component": 38.2, "financial_bonus": 30.1, "fin_level": 16.0,
        "fin_direction": 9.1, "fin_valuation": 5.0, "governance_penalty": -5.0, "raw_score": 63.3,
        "llm_coverage": 0.62, "factor_notes": ["Business Catalysts: 70/100 (high confidence -> +11.2/15)"],
        "financial_notes": ["[level 16.0/20] Revenue growth 12.0% (+3.6/6)"],
        "governance_notes": ["cfo_exit via filing (-5)", "Capped at 69 (Governance unchecked: x)",
                             "CFO exit penalty waived (resolved)", "exchange feed only: verify"]}}
    meta = {"analysis_date": "2026-11-05T10:00:00", "cache_hit": True, "financial_as_of_date": "2026-03-31"}
    rows = [{"Ticker": "TCS", "Score": 63.4, "Rating": "Mixed Signals", "Has_Filing": True, "Report": "TCS.html"},
            {"Ticker": "INFY", "Score": 71.0, "Rating": "Good Candidate", "Has_Filing": False, "Report": "x/INFY.html"}]
    return financials, llm, score_result, meta, rows


class FormatGuardTests(unittest.TestCase):
    def test_per_stock_report_matches_the_recovered_original(self):
        fin, llm, score_result, meta, _ = golden_inputs()
        html = R.create_html_report("TCS", fin, llm, score_result, has_filing=True, meta=meta)
        golden = (Path(HERE) / "golden_report_v21.html").read_text(encoding="utf-8")
        self.assertEqual(normalise(html), golden)

    def test_dashboard_matches_the_recovered_original(self):
        *_, rows = golden_inputs()
        golden = (Path(HERE) / "golden_dashboard_v21.html").read_text(encoding="utf-8")
        self.assertEqual(normalise(R.create_portfolio_report(rows)), golden)

    def test_html_in_data_is_escaped(self):
        fin, llm, score_result, meta, _ = golden_inputs()
        html = R.create_html_report("TCS", fin, llm, score_result, True, meta)
        self.assertNotIn("<b>growth</b>", html)
        self.assertIn("&lt;b&gt;growth&lt;/b&gt;", html)


# ------------------------------------------------------------ the adapter
FIN = {"revenue_growth": 0.15, "ebitda_margin": 0.22, "debt_to_equity": 0.2, "ocf_to_ebitda": 0.9,
       "interest_coverage": 12.0, "scoring_profile": "standard", "ann_cfo_exit": 1,
       "governance_sources": ["dalal.shareholding"]}
ANALYSIS = {
    "factors": [{"factor": n, "score": 70, "confidence": "high", "confidence_applied": "high",
                 "evidence": [{"document": "d.pdf", "page": 1, "quote": "q q q", "verified": True}]}
                for n in ("Business Catalysts", "Market Expansion", "Order Book Growth", "Competitive Advantage")],
    "governance": [{"flag": "cfo_exit", "raised": False, "verified": False, "evidence": []}],
    "documents_used": ["d.pdf"], "pages_included": 5, "governance_coverage": ["auditor_report", "pledge"],
    "key_strengths": ["a"], "key_risks": ["b"], "analysis_status": "success"}


class AdapterTests(unittest.TestCase):
    def test_a_saved_breakdown_rebuilds_the_scorers_own_breakdown_exactly(self):
        direct = sc.compute_score(FIN, ANALYSIS)
        b = pipeline.score_stage_v21("TCS", ANALYSIS, FIN, {}, {}, 30)
        rebuilt = pipeline._legacy_breakdown(asdict(b))
        self.assertEqual(rebuilt, direct["breakdown"])

    def test_the_report_is_identical_through_the_adapter(self):
        direct = sc.compute_score(FIN, ANALYSIS)
        b = pipeline.score_stage_v21("TCS", ANALYSIS, FIN, {}, {}, 30)
        old_way = R.create_html_report("TCS", FIN, ANALYSIS, direct, has_filing=True, meta={})
        new_way = pipeline._report_html("TCS", FIN, ANALYSIS, asdict(b), True, {})
        self.assertEqual(normalise(old_way), normalise(new_way))


# ------------------------------------------------------- run-result reports
def run_pipeline_for(tickers, analyse=None, fetch=None):
    stages, store = pipeline.make_stub_stages(
        analyse_fn=analyse or (lambda t, c, f: ANALYSIS), fetch_fn=fetch or (lambda t: dict(FIN, data_quality="complete")))
    return pipeline.run_pipeline(tickers, 30, stages), store


class RunReportTests(unittest.TestCase):
    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_results_carry_what_the_reports_need(self):
        run, _ = run_pipeline_for(["TCS"])
        r = run.results[0]
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.financials["ebitda_margin"], 0.22)
        self.assertEqual(r.analysis["analysis_status"], "success")

    def test_ok_report_has_the_ticker_score_and_analysis_date(self):
        run, _ = run_pipeline_for(["TCS"])
        html = pipeline.build_report(run.results[0])
        self.assertIn("TCS Equity Analysis", html)
        self.assertIn(f"{run.results[0].breakdown.total:.1f}/100", html)
        self.assertIn("Analysis: 20", html)
        self.assertIn("No PDF filing found", html)          # no filing was uploaded in this test

    def test_failed_tickers_have_no_report_and_do_not_stop_the_others(self):
        run, _ = run_pipeline_for(["TCS", "FAILPERM"])
        by = {r.ticker: r for r in run.results}
        self.assertIsNotNone(pipeline.build_report(by["TCS"]))
        self.assertIsNone(pipeline.build_report(by["FAILPERM"]))

    def test_stale_and_partial_results_still_get_a_report(self):
        run, _ = run_pipeline_for(["STALE429", "NOPRIOR429"])
        by = {r.ticker: r for r in run.results}
        self.assertEqual(by["STALE429"].status, "stale")
        self.assertIsNotNone(pipeline.build_report(by["STALE429"]))
        self.assertIsNotNone(pipeline.build_report(by["NOPRIOR429"]))

    def test_dashboard_is_ranked_links_reports_and_skips_failed(self):
        run, _ = run_pipeline_for(["TCS", "FAKE", "FAILPERM"])
        html = pipeline.build_dashboard(run.results)
        self.assertIn("TCS.html", html)
        self.assertIn("FAKE.html", html)
        self.assertNotIn("FAILPERM", html)
        self.assertLess(html.index("TCS"), html.index("FAKE"))      # higher score first

    def test_zip_holds_the_dashboard_and_one_report_per_scored_ticker(self):
        run, _ = run_pipeline_for(["TCS", "FAKE", "FAILPERM"])
        zf = zipfile.ZipFile(io.BytesIO(pipeline.build_reports_zip(run.results)))
        self.assertEqual(sorted(zf.namelist()), ["FAKE.html", "Portfolio_Dashboard.html", "TCS.html"])
        self.assertIn("TCS Equity Analysis", zf.read("TCS.html").decode("utf-8"))

    def test_data_notes_report_providers_warnings_and_filings(self):
        fetch = lambda t: dict(FIN, data_quality="partial", providers_used=["yfinance"],
                               missing_fields=["forward_pe"], warnings=["dalal.announcements failed"])
        run, _ = run_pipeline_for(["TCS"], fetch=fetch)
        notes = pipeline.data_notes(run.results[0])
        self.assertEqual(notes["providers"], ["yfinance"])
        self.assertEqual(notes["missing_fields"], ["forward_pe"])
        self.assertEqual(notes["warnings"], ["dalal.announcements failed"])
        self.assertEqual(notes["governance_sources"], ["dalal.shareholding"])
        self.assertEqual(notes["filings_read"], [])

    def test_unreadable_filings_are_carried_into_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "TCS_2026_05_12_AnnualReport.pdf").write_bytes(b"not a pdf")
            with mock.patch.object(config, "FILINGS_FOLDER", tmp):
                run, _ = run_pipeline_for(["TCS"])
        r = run.results[0]
        self.assertEqual(r.documents, [])
        self.assertEqual(r.unreadable[0][0], "TCS_2026_05_12_AnnualReport.pdf")
        self.assertEqual(pipeline.data_notes(r)["filings_unreadable"], r.unreadable)

    def test_a_failed_analysis_still_returns_the_data_that_was_fetched(self):
        def boom(t, c, f):
            raise pipeline.PermanentError("nope")
        run, _ = run_pipeline_for(["TCS"], analyse=boom)
        r = run.results[0]
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.financials["ebitda_margin"], 0.22)


# ------------------------------------------------------------- saved records
class SavedRecordTests(unittest.TestCase):
    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name) / "data"
        for name, value in (("DATA_DIR", base), ("STORE_DIR", base / "store")):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_real_store(self):
        stages, _ = pipeline.make_stub_stages(store=pipeline.default_store(),
                                              analyse_fn=lambda t, c, f: ANALYSIS,
                                              fetch_fn=lambda t: dict(FIN, data_quality="complete"))
        return pipeline.run_pipeline(["TCS"], 30, stages)

    def test_a_report_can_be_rebuilt_from_the_saved_record_alone(self):
        run = self.run_real_store()
        self.assertIn("TCS", pipeline.saved_tickers())      # (the demo STALE429 prior is saved too)
        record = pipeline.load_saved("TCS")
        html = pipeline.report_from_record("TCS", record)
        self.assertIn("TCS Equity Analysis", html)
        self.assertIn(f"{run.results[0].breakdown.total:.1f}/100", html)
        self.assertIn("Analysis: " + record["saved_at"][:10], html)
        self.assertNotIn("No PDF filing found", html)        # the analysis recorded a document

    def test_a_record_without_a_score_has_no_report_and_missing_records_are_none(self):
        self.assertIsNone(pipeline.report_from_record("TCS", {"analysis": {}}))
        self.assertIsNone(pipeline.report_from_record("TCS", None))
        self.assertIsNone(pipeline.load_saved("NOSUCH"))
        self.assertEqual(pipeline.saved_tickers(), [])           # nothing saved in this fresh folder

    def test_a_damaged_record_is_none_not_a_crash(self):
        self.run_real_store()
        Store(config.STORE_DIR).path_for("TCS").write_bytes(b"garbage")
        self.assertIsNone(pipeline.load_saved("TCS"))


if __name__ == "__main__":
    unittest.main()
