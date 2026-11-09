"""
test_batch_panel.py
Version: Step B5a, v0.14.1 (2026-11-09). (B5 file; Step B5a adds the display-rows tests.)
Tests for the helpers behind the app's "Batch results" tab in pipeline.py: reading the newest batch
run, reading the queue, saving the queue. The private repository is replaced by an in-memory fake.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import batch_runner
import config
import persistence
import pipeline


class FakeMirror:
    """In-memory private repository. pull_file never overwrites a local file, like the real one."""

    def __init__(self, root, remote=None, list_error="", fail_push=False, raise_on_list=False):
        self.root, self.remote = Path(root), dict(remote or {})
        self.list_error, self.fail_push, self.raise_on_list = list_error, fail_push, raise_on_list
        self.pushed = []

    def list_remote(self):
        if self.raise_on_list:
            raise RuntimeError("boom")
        if self.list_error:
            return None, self.list_error
        return [persistence.RemoteFile(p, "sha", len(t)) for p, t in sorted(self.remote.items())], ""

    def pull_file(self, rel, expected_sha=None):
        target = self.root / rel
        if target.exists():
            return persistence.OpResult(True, "skipped", "already present locally", rel)
        if rel not in self.remote:
            return persistence.OpResult(False, "failed", "not found", rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.remote[rel], encoding="utf-8")
        return persistence.OpResult(True, "restored", "", rel)

    def push_file(self, rel):
        if self.fail_push:
            return persistence.OpResult(False, "failed", "GitHub said no", rel)
        self.pushed.append(rel)
        self.remote[rel] = (self.root / rel).read_text(encoding="utf-8")
        return persistence.OpResult(True, "pushed", "", rel)


def summary(date="2026-11-08", rows=None, queue=None, counts=None, warnings=None):
    return json.dumps({"run_date": date, "counts": counts if counts is not None else {"tickers": 2},
                       "rows": rows if rows is not None else [{"ticker": "TCS", "status": "ok", "total": 50.0}],
                       "queue": queue if queue is not None else ["INFY"],
                       "warnings": warnings if warnings is not None else []})


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.root.mkdir()
        batch = self.root / "batch"
        for name, value in (("DATA_DIR", self.root), ("BATCH_DIR", batch), ("BATCH_RUNS_DIR", batch / "runs"),
                            ("BATCH_QUEUE_FILE", batch / "queue.csv"), ("ALL_DIRS", ())):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(pipeline.reset_mirror)

    def mirror(self, remote=None, **kw):
        m = FakeMirror(self.root, remote, **kw)
        pipeline.set_mirror(m)
        return m


class LatestRunTests(PanelCase):
    def test_storage_off(self):
        pipeline.set_mirror(None)
        view = pipeline.load_latest_batch_run()
        self.assertFalse(view.ok)
        self.assertIn("Permanent storage is off", view.message)

    def test_the_newest_run_by_date_is_read_and_other_files_are_ignored(self):
        self.mirror({"batch/runs/2026-11-07.json": summary("2026-11-07", queue=["OLD"]),
                     "batch/runs/2026-11-09.csv": "ignored", "batch/runs/2026-11-09.log": "ignored",
                     "batch/runs/2026-11-08.json": summary("2026-11-08", queue=["NEW"]),
                     "batch/state.json": "{}", "store/TCS.json": "{}"})
        view = pipeline.load_latest_batch_run()
        self.assertTrue(view.ok)
        self.assertEqual((view.run_date, view.queue), ("2026-11-08", ["NEW"]))

    def test_the_fields_come_through(self):
        self.mirror({"batch/runs/2026-11-08.json": summary(
            counts={"tickers": 5, "by_status": {"ok": 4}}, warnings=["one warning"],
            rows=[{"ticker": "TCS", "status": "ok"}, "junk", {"ticker": "INFY", "status": "deferred"}],
            queue=["INFY", 5, "WIPRO"])})
        view = pipeline.load_latest_batch_run()
        self.assertEqual(view.counts["tickers"], 5)
        self.assertEqual([r["ticker"] for r in view.rows], ["TCS", "INFY"])
        self.assertEqual(view.queue, ["INFY", "WIPRO"])
        self.assertEqual(view.warnings, ["one warning"])

    def test_a_same_day_rerun_replaces_a_stale_local_copy(self):
        stale = self.root / "batch/runs/2026-11-08.json"
        stale.parent.mkdir(parents=True)
        stale.write_text(summary(queue=["STALE"]), encoding="utf-8")
        self.mirror({"batch/runs/2026-11-08.json": summary(queue=["FRESH"])})
        self.assertEqual(pipeline.load_latest_batch_run().queue, ["FRESH"])

    def test_no_run_saved_yet(self):
        self.mirror({"batch/state.json": "{}"})
        view = pipeline.load_latest_batch_run()
        self.assertEqual((view.ok, view.message), (False, "No batch run has been saved yet."))

    def test_odd_files_never_break_it(self):
        for text in ("{not json", "[]", '{"rows": "x"}', ""):
            self.mirror({"batch/runs/2026-11-08.json": text})
            view = pipeline.load_latest_batch_run()
            self.assertFalse(view.ok)
            self.assertTrue(view.message)

    def test_a_listing_error_and_an_exception_become_messages(self):
        self.mirror({}, list_error="rate limited")
        self.assertIn("rate limited", pipeline.load_latest_batch_run().message)
        self.mirror({}, raise_on_list=True)
        view = pipeline.load_latest_batch_run()
        self.assertEqual((view.ok, view.message), (False, "Could not read the batch results (RuntimeError)."))

    def test_a_missing_file_after_listing_is_a_message(self):
        m = self.mirror({"batch/runs/2026-11-08.json": summary()})
        m.remote.pop("batch/runs/2026-11-08.json")
        m.list_remote = lambda: ([persistence.RemoteFile("batch/runs/2026-11-08.json", "s", 1)], "")
        view = pipeline.load_latest_batch_run()
        self.assertFalse(view.ok)
        self.assertIn("Could not read the latest batch run", view.message)


class QueueReadTests(PanelCase):
    def test_storage_off(self):
        pipeline.set_mirror(None)
        self.assertEqual(pipeline.load_batch_queue()[0], [])
        self.assertIn("Permanent storage is off", pipeline.load_batch_queue()[1])

    def test_the_queue_written_by_the_batch_is_read_back(self):
        self.mirror({"batch/queue.csv": batch_runner.queue_csv(["TCS", "INFY", "WIPRO"])})
        self.assertEqual(pipeline.load_batch_queue(), (["TCS", "INFY", "WIPRO"], ""))

    def test_a_missing_queue_is_empty_with_a_message(self):
        self.mirror({})
        tickers, message = pipeline.load_batch_queue()
        self.assertEqual(tickers, [])
        self.assertIn("no queue yet", message)

    def test_a_stale_local_queue_is_replaced(self):
        local = self.root / "batch/queue.csv"
        local.parent.mkdir(parents=True)
        local.write_text("ticker\nSTALE\n", encoding="utf-8")
        self.mirror({"batch/queue.csv": "ticker\nFRESH\n"})
        self.assertEqual(pipeline.load_batch_queue()[0], ["FRESH"])

    def test_the_parser_agrees_with_the_batch_runner(self):
        samples = ["", "ticker\n", "TCS\ninfy\n", "# help\nticker\nTCS\nTCS\nbad ticker!\nWIPRO,extra\n",
                   "\ufeffsymbol\nM&M\nBAJAJ-AUTO\n", batch_runner.queue_csv(["A", "B", "C"]),
                   "# only comments\n# nothing else\n"]
        for text in samples:
            self.assertEqual(pipeline.parse_queue_text(text), batch_runner.parse_tickers_csv(text)[0], text)
        self.assertEqual(pipeline.parse_queue_text(None), [])

    def test_the_file_text_is_identical_to_the_batch_runners(self):
        for tickers in ([], ["TCS"], ["TCS", "INFY", "M&M"]):
            self.assertEqual(pipeline.queue_text(tickers), batch_runner.queue_csv(tickers))
        self.assertEqual(pipeline.BATCH_QUEUE_HELP, batch_runner.QUEUE_HELP)


class QueueSaveTests(PanelCase):
    def test_a_saved_queue_is_written_and_backed_up(self):
        m = self.mirror({})
        ok, message = pipeline.save_batch_queue(["TCS", "infy", " WIPRO ", "TCS"])
        self.assertTrue(ok)
        self.assertIn("3 ticker(s)", message)
        self.assertEqual(m.pushed, ["batch/queue.csv"])
        self.assertEqual(m.remote["batch/queue.csv"], batch_runner.queue_csv(["TCS", "INFY", "WIPRO"]))
        self.assertEqual((self.root / "batch/queue.csv").read_text(encoding="utf-8"),
                         m.remote["batch/queue.csv"])

    def test_save_then_load_keeps_the_order(self):
        self.mirror({})
        pipeline.save_batch_queue(["C", "A", "B"])
        self.assertEqual(pipeline.load_batch_queue()[0], ["C", "A", "B"])

    def test_an_empty_queue_can_be_saved(self):
        m = self.mirror({})
        ok, message = pipeline.save_batch_queue([])
        self.assertTrue(ok)
        self.assertEqual(batch_runner.parse_tickers_csv(m.remote["batch/queue.csv"]), ([], []))

    def test_an_invalid_entry_refuses_everything(self):
        m = self.mirror({})
        for bad in (["TCS", "bad ticker!"], ["TCS", 5], ["", "TCS"], ["../x"]):
            ok, message = pipeline.save_batch_queue(bad)
            self.assertFalse(ok)
            self.assertNotIn("bad ticker", message)
        self.assertEqual(m.pushed, [])
        self.assertFalse((self.root / "batch/queue.csv").exists())

    def test_storage_off_writes_nothing(self):
        pipeline.set_mirror(None)
        ok, message = pipeline.save_batch_queue(["TCS"])
        self.assertFalse(ok)
        self.assertIn("Permanent storage is off", message)
        self.assertFalse((self.root / "batch/queue.csv").exists())

    def test_a_failed_backup_is_reported(self):
        self.mirror({}, fail_push=True)
        ok, message = pipeline.save_batch_queue(["TCS"])
        self.assertFalse(ok)
        self.assertIn("could not be backed up", message)
        self.assertIn("GitHub said no", message)

    def test_a_write_problem_never_raises(self):
        self.mirror({})
        with mock.patch.object(pipeline, "_write_text_atomic", side_effect=OSError("disk full")):
            ok, message = pipeline.save_batch_queue(["TCS"])
        self.assertEqual((ok, message), (False, "Could not save the queue (OSError)."))


class DisplayRowsTests(unittest.TestCase):
    ROWS = [
        {"ticker": "DEF1", "status": "deferred", "total": 53.2, "rating": "Speculative", "rank": 1},
        {"ticker": "LOW", "status": "ok", "total": 40.0, "rating": "Speculative", "rank": 2},
        {"ticker": "PART", "status": "partial", "total": 52.4, "rating": "Speculative", "rank": 3},
        {"ticker": "TOP", "status": "ok", "total": 78.25, "rating": "Good Candidate", "rank": 4},
        {"ticker": "DEF2", "status": "deferred", "total": 51.0, "rating": "Speculative", "rank": 5},
        {"ticker": "OLD", "status": "stale", "total": 60.0, "rating": "Mixed Signals", "rank": 6},
        {"ticker": "HELD", "status": "held", "total": 49.0, "rating": "Speculative", "rank": 7},
        {"ticker": "BAD", "status": "failed", "total": None, "rating": None, "rank": 8},
        {"ticker": "SKIP", "status": "skipped", "total": None, "rating": None, "rank": None},
    ]

    def test_only_tickers_with_an_analysis_show_a_total_and_are_ranked_first(self):
        out = pipeline.batch_display_rows(self.ROWS)
        self.assertEqual([r["ticker"] for r in out[:3]], ["TOP", "OLD", "LOW"])
        self.assertEqual([(r["rank"], r["total"]) for r in out[:3]], [("1", "78.2"), ("2", "60.0"), ("3", "40.0")])
        self.assertEqual([r["rating"] for r in out[:3]], ["Good Candidate", "Mixed Signals", "Speculative"])

    def test_tickers_without_an_analysis_show_a_dash_and_come_last(self):
        out = pipeline.batch_display_rows(self.ROWS)
        for row in out[3:]:
            self.assertIn(row["status"], ("deferred", "partial", "held", "failed", "skipped"))
            self.assertEqual((row["rank"], row["total"], row["rating"]), ("\u2013", "\u2013", "\u2013"))
        self.assertEqual(len(out), len(self.ROWS))

    def test_waiting_tickers_follow_the_queue_order(self):
        out = pipeline.batch_display_rows(self.ROWS, ["DEF2", "HELD", "DEF1"])
        self.assertEqual([r["ticker"] for r in out[3:]], ["DEF2", "HELD", "DEF1", "PART", "BAD", "SKIP"])

    def test_without_a_queue_the_original_order_is_kept(self):
        out = pipeline.batch_display_rows(self.ROWS)
        self.assertEqual([r["ticker"] for r in out[3:]], ["DEF1", "PART", "DEF2", "HELD", "BAD", "SKIP"])

    def test_the_input_rows_are_not_changed(self):
        before = json.dumps(self.ROWS, sort_keys=True)
        pipeline.batch_display_rows(self.ROWS, ["DEF1"])
        self.assertEqual(json.dumps(self.ROWS, sort_keys=True), before)

    def test_equal_totals_keep_their_order_and_odd_values_do_not_break_it(self):
        rows = [{"ticker": "A", "status": "ok", "total": 50, "rating": "Speculative"},
                {"ticker": "B", "status": "ok", "total": 50.0, "rating": "Speculative"},
                {"ticker": "C", "status": "ok", "total": None, "rating": None},
                {"ticker": "D", "status": "ok", "total": "x", "rating": "Avoid"},
                "junk", None]
        out = pipeline.batch_display_rows(rows)
        self.assertEqual([r["ticker"] for r in out], ["A", "B", "C", "D"])
        self.assertEqual([r["total"] for r in out], ["50.0", "50.0", "\u2013", "\u2013"])
        self.assertEqual(pipeline.batch_display_rows([]), [])
        self.assertEqual(pipeline.batch_display_rows(None or []), [])


if __name__ == "__main__":
    unittest.main()
