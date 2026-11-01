"""
tests/test_step1.py
Version: Step 1, v0.1.0 (2026-11-01).
Run from the project root:  python -m unittest discover -s tests -v
No network, no API key and no Streamlit needed.
"""
import dataclasses
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import pipeline
from errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid_with_notice(self):
        notices = config.validate_config()
        self.assertEqual(len(notices), 1)
        self.assertIn("not set", notices[0])

    def test_broken_split_fails_loudly(self):
        with mock.patch.object(config, "LONG_TERM_POINTS", 60):
            with self.assertRaises(ConfigError):
                config.validate_config()

    def test_good_weights_pass_and_bad_weights_fail(self):
        good = {"a": ("long", 55), "b": ("short", 45)}
        with mock.patch.object(config, "COMPONENT_WEIGHTS", good):
            self.assertEqual(config.validate_config(), [])
        bad = {"a": ("long", 50), "b": ("short", 45)}
        with mock.patch.object(config, "COMPONENT_WEIGHTS", bad):
            with self.assertRaises(ConfigError):
                config.validate_config()

    def test_secret_comes_from_environment_only(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}):
            self.assertTrue(config.has_secret("gemini_api_key"))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.has_secret("gemini_api_key"))


class TickerInputTests(unittest.TestCase):
    def test_normalise_dedupes_uppercases_and_rejects(self):
        valid, rejected = pipeline.normalise_tickers("tcs, infy\nTCS  m&m bad$name")
        self.assertEqual(valid, ["TCS", "INFY", "M&M"])
        self.assertEqual([t for t, _ in rejected], ["BAD$NAME"])

    def test_limit_is_enforced(self):
        raw = ",".join(f"T{i}" for i in range(12))
        valid, rejected = pipeline.normalise_tickers(raw, max_count=10)
        self.assertEqual(len(valid), 10)
        self.assertEqual(len(rejected), 2)


class PipelineTests(unittest.TestCase):
    def test_happy_path_is_ok_and_saved(self):
        stages, store = pipeline.make_stub_stages()
        run = pipeline.run_pipeline(["FAKE"], 30, stages)
        r = run.results[0]
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.rank, 1)
        self.assertIn("score", r.stubbed_stages)
        self.assertIn("FAKE", store.records)

    def test_transient_error_keeps_prior_and_marks_stale(self):
        stages, store = pipeline.make_stub_stages()
        before = dict(store.records["STALE429"])
        run = pipeline.run_pipeline(["STALE429"], 30, stages)
        r = run.results[0]
        self.assertEqual(r.status, "stale")
        self.assertTrue(r.prior_used)
        self.assertIsNotNone(r.rank)          # stale is still ranked
        self.assertEqual(store.records["STALE429"], before)  # prior untouched

    def test_transient_without_prior_is_partial(self):
        run = pipeline.run_pipeline(["NOPRIOR429"], 30)
        r = run.results[0]
        self.assertEqual(r.status, "partial")
        self.assertFalse(r.prior_used)
        self.assertIsNotNone(r.rank)

    def test_permanent_error_fails_but_does_not_stop_the_run(self):
        run = pipeline.run_pipeline(["FAKE", "FAILPERM", "STALE429"], 30)
        by = {r.ticker: r for r in run.results}
        self.assertEqual(len(run.results), 3)
        self.assertEqual(by["FAILPERM"].status, "failed")
        self.assertIsNone(by["FAILPERM"].rank)
        self.assertEqual(run.results[-1].ticker, "FAILPERM")  # failed listed last
        self.assertEqual(by["FAKE"].status, "ok")

    def test_ranking_order_and_tiebreaks(self):
        stages, _ = pipeline.make_stub_stages()
        table = {"A": (50.0, 20.0, 80.0), "B": (70.0, 30.0, 90.0), "C": (70.0, 35.0, 60.0)}

        def score(ticker, analysis, financials, indicators, manual, horizon):
            total, long_term, cov = table[ticker]
            return pipeline.ScoreBreakdown(long_term=long_term, short_term=total - long_term,
                                           total=total, coverage_pct=cov, horizon_days=horizon)

        stages = dataclasses.replace(stages, score=score)
        run = pipeline.run_pipeline(["A", "B", "C"], 30, stages)
        self.assertEqual([r.ticker for r in run.results], ["C", "B", "A"])
        self.assertEqual([r.rank for r in run.results], [1, 2, 3])

    def test_broken_progress_callback_does_not_break_run(self):
        def boom(ticker, stage, state):
            raise RuntimeError("callback failure")
        run = pipeline.run_pipeline(["FAKE"], 30, on_progress=boom)
        self.assertEqual(run.results[0].status, "ok")

    def test_progress_events_are_reported(self):
        events = []
        pipeline.run_pipeline(["FAKE"], 30, on_progress=lambda t, s, st: events.append((t, s, st)))
        self.assertIn(("FAKE", "fetch", "start"), events)
        self.assertIn(("FAKE", "save", "done"), events)

    def test_horizon_out_of_range_is_rejected(self):
        for bad in (0, 91):
            with self.assertRaises(ValueError):
                pipeline.run_pipeline(["FAKE"], bad)

    def test_multiple_workers_keep_input_order_before_ranking(self):
        run = pipeline.run_pipeline(["A1", "A2", "A3", "A4"], 30, max_workers=4)
        self.assertEqual([r.ticker for r in run.results], ["A1", "A2", "A3", "A4"])


if __name__ == "__main__":
    unittest.main()
