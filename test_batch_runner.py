"""
test_batch_runner.py
Version: Step B2, v0.14.0 (2026-11-08). (B1 file; one test changed in B2.)
Tests for the pure batch logic in batch_runner.py: the tickers list, the six matrix rows, the budget,
the run order, the failure counters and the counts-only summary. No files, network or Gemini.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import json
import os
import sys
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import batch_runner as br
import config

TODAY = date(2026, 11, 8)


def record(days_old=10, docs=("A.pdf",), unreadable=(), coverage=("auditor_report", "pledge"),
           version=None, status="success", fin=None, use_analysed_at=True, saved_days_old=None):
    made = (TODAY - timedelta(days=days_old)).isoformat() + "T02:00:00+00:00"
    analysis = {"analysis_status": status, "prompt_version": version or config.PROMPT_VERSION,
                "documents_used": list(docs), "documents_unreadable": list(unreadable),
                "governance_coverage": list(coverage)}
    if use_analysed_at:
        analysis["analysed_at"] = made
    saved = (TODAY - timedelta(days=days_old if saved_days_old is None else saved_days_old)).isoformat() \
        + "T02:00:00+00:00"
    return {"analysis": analysis, "financials": fin if fin is not None else
            {"revenue_growth": 0.20, "ebitda_margin": 0.18, "return_on_equity": 0.15, "debt_to_equity": 0.40},
            "prompt_version": config.PROMPT_VERSION, "saved_at": saved}


SAME_FIN = {"revenue_growth": 0.20, "ebitda_margin": 0.18, "return_on_equity": 0.15, "debt_to_equity": 0.40}


class TickerFileTests(unittest.TestCase):
    def test_plain_list_header_comments_and_case(self):
        text = "\ufeffticker\n# my list\n tcs \nBAJAJ-AUTO,Bajaj Auto,note\n\nm&m\n"
        tickers, problems = br.parse_tickers_csv(text)
        self.assertEqual(tickers, ["TCS", "BAJAJ-AUTO", "M&M"])
        self.assertEqual(problems, [])

    def test_a_first_row_that_is_a_ticker_is_kept(self):
        self.assertEqual(br.parse_tickers_csv("TCS\nINFY\n")[0], ["TCS", "INFY"])

    def test_duplicates_and_bad_lines_are_reported_without_the_text(self):
        tickers, problems = br.parse_tickers_csv("TCS\nTCS\nbad ticker!\n...\nINFY\n")
        self.assertEqual(tickers, ["TCS", "INFY"])
        self.assertEqual(len(problems), 3)
        self.assertTrue(all("bad ticker" not in p for p in problems))
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_not_text_and_empty(self):
        self.assertEqual(br.parse_tickers_csv(None)[0], [])
        self.assertTrue(br.parse_tickers_csv(5)[1])
        self.assertEqual(br.parse_tickers_csv("")[0], [])


class RecordFactTests(unittest.TestCase):
    def test_analysed_at_wins_over_saved_at(self):
        r = record(days_old=100, saved_days_old=1)
        self.assertEqual(br.analysis_date(r), TODAY - timedelta(days=100))

    def test_saved_at_is_the_fallback_and_bad_values_give_none(self):
        r = record(days_old=5, use_analysed_at=False)
        self.assertEqual(br.analysis_date(r), TODAY - timedelta(days=5))
        self.assertIsNone(br.analysis_date({"analysis": {"analysed_at": "junk"}, "saved_at": 5}))
        self.assertIsNone(br.analysis_date(None))

    def test_z_suffix_is_read(self):
        self.assertEqual(br.analysis_date({"saved_at": "2026-11-01T10:00:00Z"}), date(2026, 11, 1))

    def test_known_documents_include_unreadable_names(self):
        a = {"documents_used": ["A.pdf"], "documents_unreadable": [("S.pdf", "scanned"), "T.pdf"]}
        self.assertEqual(br.known_documents(a), {"A.pdf", "S.pdf", "T.pdf"})
        self.assertEqual(br.known_documents(None), set())

    def test_drift_is_relative_percent_and_skips_unusable_values(self):
        d = br.financial_drift({"revenue_growth": 0.20, "ebitda_margin": 0.0, "return_on_equity": None},
                               {"revenue_growth": 0.21, "ebitda_margin": 0.1, "return_on_equity": 0.1})
        self.assertEqual(list(d), ["revenue_growth"])
        self.assertAlmostEqual(d["revenue_growth"], 5.0, places=6)

    def test_drift_threshold_is_strictly_greater(self):
        self.assertEqual(br.drifted_fields({"debt_to_equity": 1.0}, {"debt_to_equity": 1.05}, 5.0), ())
        self.assertEqual(br.drifted_fields({"debt_to_equity": 1.0}, {"debt_to_equity": 1.06}, 5.0),
                         ("debt_to_equity",))

    def test_non_numbers_never_count(self):
        d = br.financial_drift({"revenue_growth": True}, {"revenue_growth": float("nan")})
        self.assertEqual(d, {})

    def test_governance_checked_mirrors_the_two_routes(self):
        self.assertTrue(br.governance_checked({"governance_coverage": ["auditor_report", "pledge"]}, {}))
        self.assertFalse(br.governance_checked({"governance_coverage": ["pledge", "related_party"]}, {}))
        self.assertFalse(br.governance_checked({"governance_coverage": ["auditor_report"]}, {}))
        both = {"governance_sources": ["dalal.shareholding", "dalal.announcements"]}
        self.assertTrue(br.governance_checked({}, both))
        self.assertFalse(br.governance_checked({}, {"governance_sources": ["dalal.shareholding"]}))


class MatrixTests(unittest.TestCase):
    def classify(self, prior, docs=("A.pdf",), fin=None):
        return br.classify(prior, docs, SAME_FIN if fin is None else fin, TODAY)

    def test_no_record_rows(self):
        for prior in (None, {}, {"analysis": None}, {"analysis": {"stub": True}},
                      record(status="failed")):
            d = self.classify(prior)
            self.assertEqual(d.status, br.NO_RECORD)
            self.assertTrue(d.gemini_call)

    def test_a_prompt_version_change_is_no_record(self):
        d = self.classify(record(version="2026-10-v2"))
        self.assertEqual((d.status, d.reason), (br.NO_RECORD, "prompt version changed"))

    def test_a_new_filing_is_delta(self):
        d = self.classify(record(), docs=("A.pdf", "B.pdf"))
        self.assertEqual(d.status, br.DELTA)
        self.assertTrue(d.gemini_call)
        self.assertFalse(d.force_refresh)

    def test_an_unreadable_filing_seen_before_is_not_new(self):
        d = self.classify(record(unreadable=["S.pdf"]), docs=("A.pdf", "S.pdf"))
        self.assertEqual(d.status, br.FRESH)

    def test_a_removed_filing_is_ignored(self):
        self.assertEqual(self.classify(record(docs=("A.pdf", "B.pdf")), docs=("A.pdf",)).status, br.FRESH)

    def test_no_filings_at_all_is_not_delta(self):
        self.assertEqual(self.classify(record(docs=()), docs=()).status, br.FRESH)

    def test_stale_after_ninety_days_forces_a_refresh(self):
        d = self.classify(record(days_old=91))
        self.assertEqual(d.status, br.STALE)
        self.assertTrue(d.gemini_call and d.force_refresh)

    def test_exactly_ninety_days_is_still_fresh(self):
        self.assertEqual(self.classify(record(days_old=90)).status, br.FRESH)

    def test_an_undated_analysis_is_stale(self):
        r = record()
        r["analysis"].pop("analysed_at")
        r["saved_at"] = "junk"
        self.assertEqual(self.classify(r).status, br.STALE)

    def test_delta_beats_stale(self):
        self.assertEqual(self.classify(record(days_old=200), docs=("A.pdf", "B.pdf")).status, br.DELTA)

    def test_drift_is_financials_only_with_no_call(self):
        fin = dict(SAME_FIN, revenue_growth=0.30)
        d = self.classify(record(), fin=fin)
        self.assertEqual(d.status, br.FINANCIALS_ONLY)
        self.assertFalse(d.gemini_call)
        self.assertEqual(d.drifted, ("revenue_growth",))

    def test_missing_governance_is_governance_only(self):
        d = self.classify(record(coverage=("pledge",)))
        self.assertEqual(d.status, br.GOVERNANCE_ONLY)
        self.assertFalse(d.gemini_call)

    def test_both_exchange_feeds_count_as_governance_data(self):
        fin = dict(SAME_FIN, governance_sources=["dalal.shareholding", "dalal.announcements"])
        self.assertEqual(self.classify(record(coverage=()), fin=fin).status, br.FRESH)

    def test_fresh_and_only_three_rows_call_gemini(self):
        self.assertEqual(self.classify(record()).status, br.FRESH)
        calls = {s for s in br.MATRIX_STATUSES if s in br.GEMINI_CALL_STATUSES}
        self.assertEqual(calls, {br.DELTA, br.NO_RECORD, br.STALE})

    def test_the_input_record_is_not_changed(self):
        r = record()
        before = json.dumps(r, sort_keys=True)
        self.classify(r, docs=("A.pdf", "B.pdf"))
        self.assertEqual(json.dumps(r, sort_keys=True), before)


class BudgetTests(unittest.TestCase):
    def test_tokens_are_characters_over_four_rounded_up(self):
        self.assertEqual(br.estimate_tokens(120_000), 30_000)
        self.assertEqual(br.estimate_tokens(5), 2)
        self.assertEqual(br.estimate_tokens(0), 0)
        self.assertEqual(br.estimate_tokens(None), 0)
        self.assertEqual(br.estimate_tokens(-5), 0)

    def test_defaults_come_from_config(self):
        b = br.Budget()
        self.assertEqual((b.max_calls, b.max_tokens), (200, 1_500_000))

    def test_about_fifty_full_analyses_fit_in_the_default_token_budget(self):
        b, n = br.Budget(), 0
        while b.can_afford(br.estimate_tokens(config.BATCH_MAX_PROMPT_CHARS)):
            b.charge(br.estimate_tokens(config.BATCH_MAX_PROMPT_CHARS))
            n += 1
        self.assertEqual(n, 50)

    def test_the_call_limit_stops_it(self):
        b = br.Budget(max_calls=2, max_tokens=10_000)
        b.charge(1)
        b.charge(1)
        self.assertFalse(b.can_afford(1))
        with self.assertRaises(ValueError):
            b.charge(1)
        self.assertEqual(b.calls, 2)

    def test_the_token_limit_stops_it_and_a_refused_charge_changes_nothing(self):
        b = br.Budget(max_calls=10, max_tokens=100)
        b.charge(60)
        self.assertFalse(b.can_afford(41))
        self.assertTrue(b.can_afford(40))
        with self.assertRaises(ValueError):
            b.charge(41)
        self.assertEqual((b.calls, b.tokens), (1, 60))


class OrderTests(unittest.TestCase):
    def test_no_record_first_then_oldest_then_file_order(self):
        records = {"A": record(days_old=5), "B": record(days_old=50), "C": None,
                   "D": record(days_old=50), "E": None}
        self.assertEqual(br.order_tickers(["A", "B", "C", "D", "E"], records), ["C", "E", "B", "D", "A"])

    def test_no_records_given_keeps_file_order(self):
        self.assertEqual(br.order_tickers(["X", "Y", "Z"]), ["X", "Y", "Z"])


class StateTests(unittest.TestCase):
    def test_a_ticker_is_skipped_after_three_failures_in_a_row(self):
        s = br.new_state()
        for i in range(2):
            br.record_outcome(s, "TCS", "failed", TODAY)
            self.assertFalse(br.is_skipped(s, "TCS"))
        br.record_outcome(s, "TCS", "failed", TODAY)
        self.assertTrue(br.is_skipped(s, "TCS"))
        self.assertEqual(br.skipped_tickers(s, ["TCS", "INFY"]), ["TCS"])

    def test_ok_and_stale_reset_the_counter(self):
        for good in ("ok", "stale"):
            s = br.new_state()
            br.record_outcome(s, "TCS", "failed", TODAY)
            br.record_outcome(s, "TCS", "failed", TODAY)
            entry = br.record_outcome(s, "TCS", good, TODAY)
            self.assertEqual(entry["consecutive_failures"], 0)
            br.record_outcome(s, "TCS", "failed", TODAY)
            self.assertFalse(br.is_skipped(s, "TCS"))

    def test_partial_deferred_and_skipped_leave_the_counter_alone(self):
        s = br.new_state()
        br.record_outcome(s, "TCS", "failed", TODAY)
        for neutral in ("partial", "deferred", "skipped"):
            entry = br.record_outcome(s, "TCS", neutral, TODAY)
            self.assertEqual(entry["consecutive_failures"], 1)
            self.assertEqual(entry["last_status"], neutral)

    def test_clearing_makes_a_skipped_ticker_run_again(self):
        s = br.new_state()
        for _ in range(3):
            br.record_outcome(s, "TCS", "failed", TODAY)
        self.assertTrue(br.clear_failures(s, "TCS"))
        self.assertFalse(br.is_skipped(s, "TCS"))
        self.assertEqual(s["tickers"]["TCS"]["consecutive_failures"], 0)
        self.assertFalse(br.clear_failures(s, "NOPE"))

    def test_round_trip(self):
        s = br.new_state()
        br.record_outcome(s, "TCS", "failed", TODAY)
        again, warning = br.parse_state(br.dump_state(s))
        self.assertIsNone(warning)
        self.assertEqual(again, {"schema_version": 1, "tickers": {
            "TCS": {"consecutive_failures": 1, "permanently_failed": False,
                    "last_status": "failed", "last_run": "2026-11-08"}}})

    def test_missing_damaged_and_newer_files_give_an_empty_state(self):
        for text in (None, "", "   "):
            self.assertEqual(br.parse_state(text), (br.new_state(), None))
        for bad in ("{not json", "[]", '{"schema_version": 1}', '{"schema_version": 2, "tickers": {}}',
                    '{"schema_version": true, "tickers": {}}'):
            state, warning = br.parse_state(bad)
            self.assertEqual(state, br.new_state())
            self.assertTrue(warning)

    def test_bad_entries_are_dropped_with_a_warning(self):
        text = json.dumps({"schema_version": 1, "tickers": {
            "TCS": {"consecutive_failures": 2, "permanently_failed": False},
            "BAD": {"consecutive_failures": "x"}, "WORSE": 5}})
        state, warning = br.parse_state(text)
        self.assertEqual(list(state["tickers"]), ["TCS"])
        self.assertIn("2 unreadable entries", warning)


class SummaryTests(unittest.TestCase):
    ROWS = [{"ticker": "TCS", "status": "ok", "matrix": "fresh"},
            {"ticker": "BAJAJ-AUTO", "status": "ok", "matrix": "delta"},
            {"ticker": "INFY", "status": "failed", "matrix": None},
            {"ticker": "WIPRO", "status": "deferred", "matrix": "stale"},
            {"ticker": "HDFCBANK", "status": "skipped"}]

    def test_counts(self):
        c = br.summary_counts(self.ROWS, calls=3, est_tokens=90_000)
        self.assertEqual(c["tickers"], 5)
        self.assertEqual(c["by_status"], {"ok": 2, "stale": 0, "partial": 0, "failed": 1,
                                          "deferred": 1, "skipped": 1})
        self.assertEqual(c["by_matrix"]["fresh"], 1)
        self.assertEqual(c["by_matrix"]["delta"], 1)
        self.assertEqual(c["by_matrix"]["stale"], 1)
        self.assertEqual((c["gemini_calls"], c["est_tokens"], c["max_calls"], c["max_tokens"]),
                         (3, 90_000, 200, 1_500_000))

    def test_the_public_log_never_shows_a_ticker(self):
        lines = br.public_log_lines(br.summary_counts(self.ROWS, calls=3, est_tokens=90_000))
        text = "\n".join(lines)
        for row in self.ROWS:
            self.assertNotIn(row["ticker"], text)
        self.assertIn("Batch run: 5 tickers", text)
        self.assertIn("Gemini calls: 3 of 200; estimated tokens 90,000 of 1,500,000", text)

    def test_unknown_statuses_are_not_counted(self):
        c = br.summary_counts([{"status": "weird", "matrix": "nope"}])
        self.assertEqual(sum(c["by_status"].values()), 0)
        self.assertEqual(sum(c["by_matrix"].values()), 0)


class ConfigTests(unittest.TestCase):
    def test_the_v2_values_and_the_batch_prefix(self):
        self.assertEqual(config.ANALYSIS_MAX_AGE_DAYS, 90)
        self.assertEqual(config.FINANCIAL_CHANGE_THRESHOLD_PCT, 5.0)
        self.assertEqual(config.BATCH_MAX_CALLS_PER_RUN, 200)
        self.assertEqual(config.BATCH_MAX_TOKENS_PER_RUN, 1_500_000)
        self.assertEqual(config.MAX_CONSECUTIVE_FAILURES, 3)
        self.assertIn("batch/", config.PERSIST_PREFIXES)
        self.assertEqual(config.PERSIST_PREFIXES[:3], ("store/", "filings/", "manual_inputs/"))

    def test_the_module_imports_only_pipeline_config_and_errors(self):
        # Step B2: batch_runner is an entry point like app.py, so it may import pipeline.
        with open(os.path.join(HERE, "batch_runner.py"), encoding="utf-8") as fh:
            src = fh.read()
        for name in ("store", "persistence", "gemini_analyser", "data_fetch",
                     "pdf_extractor", "filings", "score_card", "report_generator",
                     "manual_inputs_store", "rate_limiter"):
            self.assertNotIn(f"import {name}", src)
            self.assertNotIn(f"from {name} ", src)


if __name__ == "__main__":
    unittest.main()
