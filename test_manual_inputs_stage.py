"""
test_manual_inputs_stage.py
Version: Step 11c, v0.11.2 (2026-11-07).
Tests for the real manual_inputs pipeline stage and the text lines the Details panel shows.
Every test writes into a temporary folder; no network needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import pipeline

TODAY = date.today()


class StageCase(unittest.TestCase):
    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name) / "data"
        for name, value in (("DATA_DIR", base), ("MANUAL_INPUTS_DIR", base / "manual_inputs")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, field, value, days_ago=0, blank=False):
        pipeline.save_manual_input("FAKE", field, value, TODAY - timedelta(days=days_ago), blank=blank)


class StageTests(StageCase):
    def test_nothing_saved_gives_all_missing_and_no_values(self):
        out = pipeline.manual_inputs_stage("FAKE")
        self.assertEqual(out["values"], {})
        self.assertIsNone(out["warning"])
        self.assertEqual(list(out["fields"]), list(config.MANUAL_FIELDS))
        self.assertTrue(all(f["status"] == "missing" for f in out["fields"].values()))

    def test_only_fresh_non_blank_values_are_passed_on(self):
        self.save("promoter_pledge_pct", "5")
        self.save("fii_holding_change", "2", days_ago=config.MANUAL_INPUT_MAX_AGE_DAYS + 5)
        self.save("dii_holding_change", None, blank=True)
        out = pipeline.manual_inputs_stage("FAKE")
        self.assertEqual(out["values"], {"promoter_pledge_pct": 5.0})
        self.assertEqual(out["fields"]["fii_holding_change"]["status"], "stale")
        self.assertTrue(out["fields"]["dii_holding_change"]["blank"])
        self.assertEqual(out["fields"]["next_result_date"]["status"], "missing")

    def test_a_damaged_file_is_a_warning_not_an_error(self):
        store = pipeline.default_manual_store()
        store.values_dir.mkdir(parents=True)
        store.path_for("FAKE").write_bytes(b"garbage")
        out = pipeline.manual_inputs_stage("FAKE")
        self.assertEqual(out["values"], {})
        self.assertIn("damaged", out["warning"])

    def test_the_stage_never_touches_the_network(self):
        with mock.patch.object(pipeline, "get_mirror", side_effect=AssertionError("network used")):
            pipeline.manual_inputs_stage("FAKE")


class RunTests(StageCase):
    def test_the_stage_is_real_not_a_stub(self):
        run = pipeline.run_pipeline(["FAKE"], 30)
        self.assertNotIn("manual_inputs", run.results[0].stubbed_stages)
        self.assertIn("indicators", run.results[0].stubbed_stages)

    def test_saved_values_appear_in_the_result_but_do_not_change_the_score(self):
        before = pipeline.run_pipeline(["FAKE"], 30).results[0]
        self.save("promoter_pledge_pct", "40")
        self.save("fii_holding_change", "5")
        after = pipeline.run_pipeline(["FAKE"], 30).results[0]
        self.assertEqual(after.manual_inputs["values"], {"promoter_pledge_pct": 40.0, "fii_holding_change": 5.0})
        self.assertEqual(after.breakdown.total, before.breakdown.total)
        self.assertEqual(after.status, "ok")

    def test_a_damaged_manual_file_does_not_downgrade_the_run(self):
        store = pipeline.default_manual_store()
        store.values_dir.mkdir(parents=True)
        store.path_for("FAKE").write_bytes(b"garbage")
        r = pipeline.run_pipeline(["FAKE"], 30).results[0]
        self.assertEqual(r.status, "ok")
        self.assertIn("damaged", r.manual_inputs["warning"])
        self.assertNotIn("manual_inputs", r.stage_errors)

    def test_a_failed_analysis_still_carries_the_manual_values_that_were_read(self):
        pipeline.save_manual_input("FAILPERM", "promoter_pledge_pct", "7", TODAY)
        r = pipeline.run_pipeline(["FAILPERM"], 30).results[0]
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.manual_inputs["values"], {"promoter_pledge_pct": 7.0})

    def test_a_ticker_that_failed_at_fetch_has_no_manual_summary(self):
        stages, _ = pipeline.make_stub_stages(fetch_fn=lambda t: None)
        r = pipeline.run_pipeline(["TCS"], 30, stages).results[0]
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.manual_inputs, {})
        self.assertEqual(pipeline.manual_summary_lines(r), ["Not read (the run stopped before this step)"])

    def test_a_stage_error_makes_the_ticker_partial_as_before(self):
        import dataclasses

        def boom(ticker):
            raise RuntimeError("kaboom")
        stages, _ = pipeline.make_stub_stages()
        r = pipeline.run_pipeline(["FAKE"], 30, dataclasses.replace(stages, manual_inputs=boom)).results[0]
        self.assertEqual(r.status, "partial")
        self.assertIn("manual_inputs", r.stage_errors)


class LinesTests(StageCase):
    def lines_for(self, ticker="FAKE"):
        r = pipeline.run_pipeline([ticker], 30).results[0]
        return pipeline.manual_summary_lines(r)

    def test_one_line_per_field_with_the_right_wording(self):
        self.save("promoter_pledge_pct", "5")
        self.save("fii_holding_change", "2.5", days_ago=config.MANUAL_INPUT_MAX_AGE_DAYS + 1)
        self.save("dii_holding_change", None, blank=True)
        lines = self.lines_for()
        self.assertEqual(len(lines), len(config.MANUAL_FIELDS))
        self.assertIn(f"Promoter pledge (% of promoter shares pledged): 5 (as of {TODAY.isoformat()})", lines[0])
        self.assertIn("2.5", lines[1])
        self.assertIn("not used", lines[1])
        self.assertIn(f"older than {config.MANUAL_INPUT_MAX_AGE_DAYS} days", lines[1])
        self.assertIn("left blank", lines[2])
        self.assertIn("not entered", lines[3])

    def test_a_date_field_is_shown_as_text(self):
        self.save("next_result_date", "2026-12-15")
        lines = self.lines_for()
        self.assertIn("Next quarterly result date: 2026-12-15", lines[3])

    def test_a_warning_comes_first(self):
        store = pipeline.default_manual_store()
        store.values_dir.mkdir(parents=True)
        store.path_for("FAKE").write_bytes(b"garbage")
        lines = self.lines_for()
        self.assertTrue(lines[0].startswith("manual inputs not read"))


if __name__ == "__main__":
    unittest.main()
