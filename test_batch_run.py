"""
test_batch_run.py
Version: Step B2, v0.14.0 (2026-11-08).
Tests for the batch runner (run_batch, run_live, the analyse wrapper) and the two small pipeline.py
changes made for it. Gemini, the data fetch and the private repository are replaced by fakes, so
nothing here needs a network, pandas, yfinance, google-genai or Streamlit.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import batch_runner as br
import config
import persistence
import pipeline
from errors import PermanentError, TransientError

TODAY = date(2026, 11, 8)
NOW = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)
GOOD_FIN = {"revenue_growth": 0.20, "ebitda_margin": 0.18, "return_on_equity": 0.15,
            "debt_to_equity": 0.40, "governance_sources": ["dalal.shareholding", "dalal.announcements"]}


def prior(days_old=10, docs=("A.pdf",), coverage=("auditor_report", "pledge"), stamp=True, fin=None):
    made = (TODAY - timedelta(days=days_old)).isoformat() + "T02:00:00+00:00"
    analysis = {"analysis_status": "success", "prompt_version": config.PROMPT_VERSION,
                "documents_used": list(docs), "governance_coverage": list(coverage),
                "factors": [], "governance": [], "pages_included": 1}
    if stamp:
        analysis["analysed_at"] = made
    return {"analysis": analysis, "financials": dict(GOOD_FIN if fin is None else fin),
            "prompt_version": config.PROMPT_VERSION, "saved_at": made}


class Fake:
    """Fake fetch, filings context, Gemini and store."""

    def __init__(self):
        self.store = pipeline.StubStore()
        self.fetch_calls, self.gemini_calls = [], []
        self.fin = {}            # ticker -> financials dict, or None to make the fetch fail
        self.ctx = {}            # ticker -> context dict
        self.gemini_error = {}   # ticker -> exception to raise
        self.print_in_fetch = False

    def fetch(self, ticker):
        self.fetch_calls.append(ticker)
        if self.print_in_fetch:
            print(f"fetching {ticker}")
        fin = self.fin.get(ticker, dict(GOOD_FIN))
        if fin is None:
            raise PermanentError("no financial data from any provider")
        return {"financials": dict(fin), "price_history": {}}

    def context(self, ticker):
        return self.ctx.get(ticker) or {"text": "x" * 400, "meta": {"pages_included": 1},
                                        "documents": ["A.pdf"], "unreadable": []}

    def real_analyse(self, ticker, context, financials, force_refresh=False):
        self.gemini_calls.append((ticker, force_refresh))
        if ticker in self.gemini_error:
            raise self.gemini_error[ticker]
        return {"analysis_status": "success", "prompt_version": config.PROMPT_VERSION,
                "documents_used": list(context.get("documents", [])), "governance_coverage":
                ["auditor_report", "pledge"], "factors": [], "governance": [], "pages_included": 1}

    def stages(self):
        def original_analyse(*args):
            raise AssertionError("the original analyse stage must be replaced")
        return pipeline.Stages(fetch=self.fetch, indicators=lambda ph: {}, manual_inputs=lambda t: {},
                               filings_context=self.context, analyse=original_analyse,
                               score=pipeline.score_stage_v21, load_prior=self.store.load,
                               save=self.store.save, backup=None)

    def run(self, tickers, state=None, **kw):
        kw.setdefault("today", TODAY)
        kw.setdefault("now_fn", lambda: NOW)
        return br.run_batch(tickers, state if state is not None else br.new_state(),
                            self.stages(), self.real_analyse, **kw)


def row(result, ticker):
    return next(r for r in result.rows if r["ticker"] == ticker)


class MatrixRunTests(unittest.TestCase):
    def setUp(self):
        self.f = Fake()

    def test_a_fresh_ticker_reuses_the_stored_analysis_without_gemini(self):
        self.f.store.records["TCS"] = prior(days_old=10)
        stamp = self.f.store.records["TCS"]["analysis"]["analysed_at"]
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [])
        self.assertEqual((row(res, "TCS")["status"], row(res, "TCS")["matrix"]), ("ok", "fresh"))
        self.assertEqual(self.f.store.records["TCS"]["analysis"]["analysed_at"], stamp)
        self.assertEqual(res.counts["gemini_calls"], 0)

    def test_a_new_ticker_calls_gemini_once_and_is_stamped(self):
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [("TCS", False)])
        self.assertEqual(row(res, "TCS")["matrix"], "no_record")
        self.assertEqual(self.f.store.records["TCS"]["analysis"]["analysed_at"], NOW.isoformat())
        self.assertEqual((res.counts["gemini_calls"], res.counts["est_tokens"]), (1, 100))

    def test_a_new_filing_is_a_delta_call(self):
        self.f.store.records["TCS"] = prior()
        self.f.ctx["TCS"] = {"text": "y" * 40, "meta": {"pages_included": 1},
                             "documents": ["A.pdf", "B.pdf"], "unreadable": []}
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [("TCS", False)])
        self.assertEqual(row(res, "TCS")["matrix"], "delta")

    def test_a_stale_ticker_forces_a_refresh(self):
        self.f.store.records["TCS"] = prior(days_old=100)
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [("TCS", True)])
        self.assertEqual(row(res, "TCS")["matrix"], "stale")
        self.assertEqual(self.f.store.records["TCS"]["analysis"]["analysed_at"], NOW.isoformat())

    def test_a_legacy_analysis_takes_the_saved_date_when_it_is_reused(self):
        rec = prior(days_old=10, stamp=False)
        self.f.store.records["TCS"] = rec
        self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [])
        self.assertEqual(self.f.store.records["TCS"]["analysis"]["analysed_at"], rec["saved_at"])

    def test_drift_reuses_the_ai_output(self):
        self.f.store.records["TCS"] = prior(fin=dict(GOOD_FIN, revenue_growth=0.10))
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [])
        self.assertEqual(row(res, "TCS")["matrix"], "financials_only")

    def test_missing_governance_data_reuses_the_ai_output(self):
        self.f.store.records["TCS"] = prior(coverage=("pledge",))
        self.f.fin["TCS"] = {k: v for k, v in GOOD_FIN.items() if k != "governance_sources"}
        res = self.f.run(["TCS"])
        self.assertEqual(self.f.gemini_calls, [])
        self.assertEqual(row(res, "TCS")["matrix"], "governance_only")

    def test_tickers_are_run_oldest_analysis_first(self):
        self.f.store.records["TCS"] = prior(days_old=5)
        self.f.store.records["INFY"] = prior(days_old=50)
        self.f.run(["TCS", "INFY", "WIPRO"])
        self.assertEqual(self.f.fetch_calls, ["WIPRO", "INFY", "TCS"])

    def test_limit_takes_the_first_tickers_of_the_file(self):
        self.f.run(["A", "B", "C", "D"], limit=2)
        self.assertEqual(sorted(self.f.fetch_calls), ["A", "B"])

    def test_demo_tickers_are_ignored(self):
        res = self.f.run(["FAKE", "TCS"])
        self.assertEqual(self.f.fetch_calls, ["TCS"])
        self.assertTrue(any("demo ticker" in w for w in res.warnings))


class BudgetRunTests(unittest.TestCase):
    def setUp(self):
        self.f = Fake()

    def test_a_ticker_the_budget_cannot_afford_is_deferred_and_not_saved(self):
        res = self.f.run(["A", "B"], budget=br.Budget(max_calls=1, max_tokens=10_000))
        self.assertEqual([r["status"] for r in res.rows if r["ticker"] == "A"], ["ok"])
        self.assertEqual(row(res, "B")["status"], "deferred")
        self.assertEqual(row(res, "B")["matrix"], "no_record")
        self.assertNotIn("B", self.f.store.records)
        self.assertEqual(self.f.gemini_calls, [("A", False)])
        self.assertEqual(res.counts["by_status"]["deferred"], 1)
        self.assertEqual(res.state["tickers"]["B"]["consecutive_failures"], 0)
        self.assertEqual(res.state["tickers"]["B"]["last_status"], "deferred")

    def test_a_deferred_ticker_keeps_its_stored_record_untouched(self):
        self.f.store.records["TCS"] = prior(days_old=100)
        before = json.dumps(self.f.store.records["TCS"], sort_keys=True)
        res = self.f.run(["TCS"], budget=br.Budget(max_calls=0, max_tokens=0))
        self.assertEqual(row(res, "TCS")["status"], "deferred")
        self.assertEqual(json.dumps(self.f.store.records["TCS"], sort_keys=True), before)
        self.assertEqual(self.f.gemini_calls, [])

    def test_the_token_budget_counts_the_context_size(self):
        self.f.ctx["A"] = {"text": "x" * 400, "meta": {}, "documents": [], "unreadable": []}
        self.f.ctx["B"] = {"text": "x" * 400, "meta": {}, "documents": [], "unreadable": []}
        res = self.f.run(["A", "B"], budget=br.Budget(max_calls=10, max_tokens=150))
        self.assertEqual(res.counts["gemini_calls"], 1)
        self.assertEqual(res.counts["by_status"]["deferred"], 1)

    def test_a_transient_gemini_error_keeps_the_prior_and_does_not_count_as_a_failure(self):
        self.f.store.records["TCS"] = prior(days_old=100)
        before = json.dumps(self.f.store.records["TCS"], sort_keys=True)
        self.f.gemini_error["TCS"] = TransientError("429")
        state = br.new_state()
        br.record_outcome(state, "TCS", "failed", TODAY)
        br.record_outcome(state, "TCS", "failed", TODAY)
        res = self.f.run(["TCS"], state=state)
        self.assertEqual(row(res, "TCS")["status"], "stale")
        self.assertEqual(json.dumps(self.f.store.records["TCS"], sort_keys=True), before)
        self.assertEqual(state["tickers"]["TCS"]["consecutive_failures"], 0)


class FailureCounterRunTests(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.f.fin["BAD"] = None

    def test_one_failing_ticker_does_not_stop_the_others(self):
        res = self.f.run(["BAD", "GOOD"])
        self.assertEqual(row(res, "BAD")["status"], "failed")
        self.assertEqual(row(res, "GOOD")["status"], "ok")

    def test_three_failed_runs_in_a_row_skip_the_ticker(self):
        state = br.new_state()
        for _ in range(3):
            self.f.run(["BAD"], state=state)
        self.assertTrue(br.is_skipped(state, "BAD"))
        self.f.fetch_calls.clear()
        res = self.f.run(["BAD", "GOOD"], state=state)
        self.assertNotIn("BAD", self.f.fetch_calls)
        self.assertEqual(row(res, "BAD")["status"], "skipped")
        self.assertEqual(state["tickers"]["BAD"]["consecutive_failures"], 3)

    def test_clearing_lets_the_ticker_run_again(self):
        state = br.new_state()
        for _ in range(3):
            self.f.run(["BAD"], state=state)
        br.clear_failures(state, "BAD")
        self.f.fetch_calls.clear()
        self.f.run(["BAD"], state=state)
        self.assertEqual(self.f.fetch_calls, ["BAD"])

    def test_a_successful_run_resets_the_counter(self):
        state = br.new_state()
        self.f.run(["BAD"], state=state)
        self.f.run(["BAD"], state=state)
        self.f.fin["BAD"] = dict(GOOD_FIN)
        self.f.run(["BAD"], state=state)
        self.assertEqual(state["tickers"]["BAD"]["consecutive_failures"], 0)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.f = Fake()
        self.f.fin["BAD"] = None
        self.res = self.f.run(["TCS", "BAD", "INFY"])

    def test_the_public_lines_show_no_ticker_and_no_score(self):
        text = "\n".join(self.res.public_lines())
        for t in ("TCS", "BAD", "INFY"):
            self.assertNotIn(t, text)
        self.assertIn("Batch run: 3 tickers", text)
        self.assertIn("failed 1", text)

    def test_the_private_summary_has_everything(self):
        data = json.loads(self.res.summary_json())
        self.assertEqual({r["ticker"] for r in data["rows"]}, {"TCS", "BAD", "INFY"})
        self.assertEqual(data["code_version"], config.CODE_VERSION)
        self.assertEqual(data["counts"]["tickers"], 3)

    def test_the_ranked_csv_has_a_header_and_every_ticker(self):
        lines = self.res.ranked_csv().strip().split("\n")
        self.assertTrue(lines[0].startswith("Rank,Ticker,Status,Matrix row,Total,Rating"))
        self.assertEqual(len(lines), 4)

    def test_warnings_are_counted_in_the_public_lines(self):
        res = self.f.run(["TCS"], warnings=["2 problem line(s) in batch/tickers.csv"])
        self.assertIn("Warnings: 1", "\n".join(res.public_lines()))


class PipelineChangeTests(unittest.TestCase):
    def setUp(self):
        pipeline.set_mirror(None)
        self.addCleanup(pipeline.reset_mirror)

    def test_the_context_budget_defaults_to_the_interactive_one(self):
        with mock.patch.object(pipeline.pdf_extractor, "build_context", return_value=("", {})) as m:
            pipeline.filings_context_stage("NOFILES")
            pipeline.filings_context_stage("NOFILES", max_chars=config.BATCH_MAX_PROMPT_CHARS)
        self.assertEqual(m.call_args_list[0].kwargs["max_chars"], config.MAX_PROMPT_CHARS)
        self.assertEqual(m.call_args_list[1].kwargs["max_chars"], 120_000)

    def test_force_refresh_is_sent_to_the_analyser_only_when_asked_for(self):
        seen = []

        def fake_analyse_company(*args, **kwargs):
            seen.append(dict(kwargs))
            return {"analysis_status": "success"}
        with mock.patch.object(pipeline.gemini_analyser, "analyse_company", fake_analyse_company):
            analyse = pipeline.make_real_analyse(generate_fn=lambda p: "{}")
            analyse("TCS", {"text": "", "meta": {}}, {})
            analyse("TCS", {"text": "", "meta": {}}, {}, force_refresh=True)
        self.assertNotIn("force_refresh", seen[0])
        self.assertTrue(seen[1]["force_refresh"])


class FakeMirror:
    def __init__(self, root, remote=None, fail_push=()):
        self.root, self.remote, self.fail_push = Path(root), dict(remote or {}), set(fail_push)
        self.pushed = []

    def pull_file(self, rel, expected_sha=None):
        target = self.root / rel
        if target.exists():
            return persistence.OpResult(True, "skipped", "", rel)
        if rel not in self.remote:
            return persistence.OpResult(False, "failed", "not found", rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.remote[rel], encoding="utf-8")
        return persistence.OpResult(True, "restored", "", rel)

    def pull_missing(self, prefixes):
        return persistence.PullResult()

    def push_file(self, rel):
        if rel in self.fail_push:
            return persistence.OpResult(False, "failed", "boom", rel)
        self.pushed.append(rel)
        self.remote[rel] = (self.root / rel).read_text(encoding="utf-8")
        return persistence.OpResult(True, "pushed", "", rel)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.root.mkdir()
        batch = self.root / "batch"
        for name, value in (("DATA_DIR", self.root), ("BATCH_DIR", batch), ("BATCH_RUNS_DIR", batch / "runs"),
                            ("BATCH_TICKERS_FILE", batch / "tickers.csv"),
                            ("BATCH_STATE_FILE", batch / "state.json"), ("ALL_DIRS", ())):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(pipeline.reset_mirror)
        self.f = Fake()

    def live(self, mirror, **kw):
        pipeline.set_mirror(mirror)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = br.run_live(today=TODAY, stages_factory=self.f.stages,
                                 analyse_factory=lambda: self.f.real_analyse, **kw)
        return result, out.getvalue()

    def test_a_full_run_writes_and_backs_up_the_four_files(self):
        self.f.print_in_fetch = True
        mirror = FakeMirror(self.root, {"batch/tickers.csv": "ticker\nTCS\nINFY\n"})
        (code, lines), shown = self.live(mirror)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(mirror.pushed), ["batch/runs/2026-11-08.csv", "batch/runs/2026-11-08.json",
                                                 "batch/runs/2026-11-08.log", "batch/state.json"])
        text = "\n".join(lines)
        self.assertIn("Batch run: 2 tickers", text)
        self.assertIn("Backup of the counters, summary and log: 4 of 4 ok", text)
        for t in ("TCS", "INFY"):
            self.assertNotIn(t, text)
            self.assertNotIn(t, shown)
        log = (self.root / "batch/runs/2026-11-08.log").read_text(encoding="utf-8")
        self.assertIn("fetching TCS", log)
        state, warning = br.parse_state((self.root / "batch/state.json").read_text(encoding="utf-8"))
        self.assertIsNone(warning)
        self.assertEqual(sorted(state["tickers"]), ["INFY", "TCS"])

    def test_a_skipped_ticker_stays_skipped(self):
        state = br.new_state()
        for _ in range(3):
            br.record_outcome(state, "TCS", "failed", TODAY)
        mirror = FakeMirror(self.root, {"batch/tickers.csv": "TCS\nINFY\n", "batch/state.json": br.dump_state(state)})
        (code, _), _ = self.live(mirror)
        self.assertEqual(code, 0)
        self.assertNotIn("TCS", self.f.fetch_calls)
        data = json.loads((self.root / "batch/runs/2026-11-08.json").read_text(encoding="utf-8"))
        self.assertEqual(next(r for r in data["rows"] if r["ticker"] == "TCS")["status"], "skipped")

    def test_limit_is_passed_through(self):
        mirror = FakeMirror(self.root, {"batch/tickers.csv": "A\nB\nC\n"})
        self.live(mirror, limit=1)
        self.assertEqual(self.f.fetch_calls, ["A"])

    def test_no_permanent_storage_stops_the_run(self):
        (code, lines), _ = self.live(None)
        self.assertEqual(code, 2)
        self.assertIn("permanent storage is not set up", lines[0])

    def test_no_gemini_key_stops_the_run_when_the_real_analyser_would_be_used(self):
        pipeline.set_mirror(FakeMirror(self.root, {"batch/tickers.csv": "TCS\n"}))
        code, lines = br.run_live(today=TODAY, stages_factory=self.f.stages)
        self.assertEqual(code, 2)
        self.assertIn("GEMINI_API_KEY", lines[0])

    def test_a_missing_or_empty_tickers_file_stops_the_run(self):
        (code, lines), _ = self.live(FakeMirror(self.root, {}))
        self.assertEqual((code, "tickers.csv was not found" in lines[0]), (2, True))
        (code, lines), _ = self.live(FakeMirror(self.root, {"batch/tickers.csv": "# nothing\n"}))
        self.assertEqual((code, "no valid tickers" in lines[0]), (2, True))

    def test_a_failed_backup_gives_exit_code_one(self):
        mirror = FakeMirror(self.root, {"batch/tickers.csv": "TCS\n"}, fail_push=["batch/state.json"])
        (code, lines), _ = self.live(mirror)
        self.assertEqual(code, 1)
        self.assertIn("3 of 4 ok", "\n".join(lines))

    def test_an_unexpected_error_prints_only_its_type(self):
        def boom():
            raise RuntimeError("secret detail about TCS")
        pipeline.set_mirror(FakeMirror(self.root, {"batch/tickers.csv": "TCS\n"}))
        code, lines = br.run_live(today=TODAY, stages_factory=boom, analyse_factory=lambda: self.f.real_analyse)
        self.assertEqual(code, 2)
        self.assertEqual(lines, ["Batch stopped: unexpected RuntimeError"])


class CommandLineTests(unittest.TestCase):
    def test_limit_option(self):
        self.assertEqual(br.parse_args(["--limit", "3"]).limit, 3)
        self.assertIsNone(br.parse_args([]).limit)

    def test_a_bad_limit_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for bad in (["--limit", "0"], ["--limit", "x"]):
                with self.assertRaises(SystemExit):
                    br.parse_args(bad)


if __name__ == "__main__":
    unittest.main()
