"""
test_store.py
Version: Step 2, v0.2.0 (2026-11-02).
Tests for store.py and for the pipeline running on the real file-backed store.
Run from the project root:  python -m unittest discover -s . -p "test_*.py" -v
Every test writes into a temporary folder; nothing touches the real data folder.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import pipeline
import store as store_module
from errors import StoreError
from store import Store


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "store"
        self.store = Store(self.root)

    def files(self):
        return sorted(p.name for p in self.root.iterdir()) if self.root.exists() else []


class RoundTripTests(StoreTestCase):
    def test_save_then_load_returns_the_record_with_metadata(self):
        self.store.save("TCS", {"analysis": {"x": 1}})
        rec = self.store.load("TCS")
        self.assertEqual(rec["analysis"], {"x": 1})
        self.assertEqual(rec["ticker"], "TCS")
        self.assertEqual(rec["schema_version"], config.SCHEMA_VERSION)
        self.assertEqual(rec["spec_version"], config.SPEC_VERSION)
        self.assertEqual(rec["prompt_version"], config.PROMPT_VERSION)
        self.assertIn("saved_at", rec)

    def test_missing_ticker_loads_as_none(self):
        self.assertIsNone(self.store.load("NOSUCH"))

    def test_list_and_exists(self):
        self.store.save("INFY", {"a": 1})
        self.store.save("M&M", {"a": 2})
        self.assertEqual(self.store.list_tickers(), ["INFY", "M&M"])
        self.assertTrue(self.store.exists("INFY"))
        self.assertFalse(self.store.exists("TCS"))

    def test_invalid_tickers_are_rejected(self):
        for bad in ("../x", "", "..", "tcs", "A/B", None, "-"):
            with self.assertRaises(StoreError):
                self.store.save(bad, {"a": 1})
            with self.assertRaises(StoreError):
                self.store.load(bad)

    def test_second_save_replaces_the_first(self):
        self.store.save("TCS", {"v": 1})
        self.store.save("TCS", {"v": 2})
        self.assertEqual(self.store.load("TCS")["v"], 2)
        self.assertEqual(self.files(), ["TCS.json"])


class AtomicWriteTests(StoreTestCase):
    def test_failed_swap_leaves_the_old_record_intact_and_no_temp_files(self):
        self.store.save("TCS", {"v": "old"})
        before = self.store.path_for("TCS").read_bytes()
        with mock.patch("store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(StoreError):
                self.store.save("TCS", {"v": "new"})
        self.assertEqual(self.store.path_for("TCS").read_bytes(), before)
        self.assertEqual(self.files(), ["TCS.json"])

    def test_unserialisable_record_leaves_the_old_record_intact(self):
        self.store.save("TCS", {"v": "old"})
        before = self.store.path_for("TCS").read_bytes()
        for bad in ({"v": {1, 2}}, {"v": float("nan")}, {"v": float("inf")}):
            with self.assertRaises(StoreError):
                self.store.save("TCS", bad)
        self.assertEqual(self.store.path_for("TCS").read_bytes(), before)
        self.assertEqual(self.files(), ["TCS.json"])

    def test_leftover_temp_file_from_a_crash_is_ignored(self):
        self.store.save("TCS", {"v": "old"})
        (self.root / ".TCS.crashed.tmp").write_text("{half written", encoding="utf-8")
        self.assertEqual(self.store.load("TCS")["v"], "old")
        self.assertEqual(self.store.list_tickers(), ["TCS"])

    def test_concurrent_saves_never_corrupt_a_record(self):
        def worker(n):
            for i in range(25):
                self.store.save("TCS", {"writer": n, "i": i})

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rec = self.store.load("TCS")
        self.assertIn(rec["writer"], range(4))
        self.assertEqual(self.files(), ["TCS.json"])


class DamageAndSchemaTests(StoreTestCase):
    def test_damaged_file_is_set_aside_not_overwritten(self):
        self.root.mkdir(parents=True)
        path = self.store.path_for("TCS")
        path.write_bytes(b"{not json at all")
        with self.assertRaises(StoreError) as ctx:
            self.store.load("TCS")
        self.assertIn("damaged", str(ctx.exception))
        kept = [p for p in self.root.iterdir() if ".corrupt-" in p.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"{not json at all")  # bytes preserved
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.load("TCS"))      # nothing there any more
        self.assertEqual(self.store.list_tickers(), [])  # set-aside file is not listed

    def test_non_object_json_counts_as_damaged(self):
        self.root.mkdir(parents=True)
        self.store.path_for("TCS").write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.load("TCS")

    def test_newer_schema_is_refused_and_file_untouched(self):
        self.root.mkdir(parents=True)
        path = self.store.path_for("TCS")
        path.write_text(json.dumps({"schema_version": 999, "ticker": "TCS"}), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(StoreError):
            self.store.load("TCS")
        self.assertEqual(path.read_bytes(), before)

    def test_missing_schema_version_is_an_error(self):
        self.root.mkdir(parents=True)
        self.store.path_for("TCS").write_text(json.dumps({"ticker": "TCS"}), encoding="utf-8")
        with self.assertRaises(StoreError):
            self.store.load("TCS")

    def test_older_schema_is_migrated_in_memory_only(self):
        self.root.mkdir(parents=True)
        path = self.store.path_for("TCS")
        path.write_text(json.dumps({"schema_version": 1, "ticker": "TCS", "a": 1}), encoding="utf-8")
        before = path.read_bytes()

        def up(rec):
            rec["schema_version"] = 2
            rec["migrated"] = True
            return rec

        with mock.patch.object(config, "SCHEMA_VERSION", 2), \
                mock.patch.object(store_module, "MIGRATIONS", {1: up}):
            rec = self.store.load("TCS")
        self.assertTrue(rec["migrated"])
        self.assertEqual(path.read_bytes(), before)  # never edited on disk

    def test_missing_migration_is_an_error(self):
        self.root.mkdir(parents=True)
        self.store.path_for("TCS").write_text(
            json.dumps({"schema_version": 1, "ticker": "TCS"}), encoding="utf-8")
        with mock.patch.object(config, "SCHEMA_VERSION", 2):
            with self.assertRaises(StoreError):
                self.store.load("TCS")


class PipelineWithRealStoreTests(StoreTestCase):
    def test_ok_ticker_is_saved_to_disk(self):
        stages, _ = pipeline.make_stub_stages(store=self.store)
        run = pipeline.run_pipeline(["FAKE"], 30, stages)
        self.assertEqual(run.results[0].status, "ok")
        rec = self.store.load("FAKE")
        self.assertEqual(rec["ticker"], "FAKE")
        self.assertIn("breakdown", rec)
        self.assertNotIn("save", run.results[0].stubbed_stages)  # real store, not a stub

    def test_simulated_429_leaves_the_stored_analysis_byte_for_byte_unchanged(self):
        stages, _ = pipeline.make_stub_stages(store=self.store)   # seeds STALE429
        path = self.store.path_for("STALE429")
        before = path.read_bytes()
        run = pipeline.run_pipeline(["STALE429"], 30, stages)
        r = run.results[0]
        self.assertEqual(r.status, "stale")
        self.assertTrue(r.prior_used)
        self.assertIsNotNone(r.rank)
        self.assertEqual(path.read_bytes(), before)

    def test_429_without_a_prior_creates_no_file(self):
        stages, _ = pipeline.make_stub_stages(store=self.store)
        run = pipeline.run_pipeline(["NOPRIOR429"], 30, stages)
        self.assertEqual(run.results[0].status, "partial")
        self.assertFalse(self.store.exists("NOPRIOR429"))

    def test_permanent_failure_creates_no_file(self):
        stages, _ = pipeline.make_stub_stages(store=self.store)
        run = pipeline.run_pipeline(["FAILPERM"], 30, stages)
        self.assertEqual(run.results[0].status, "failed")
        self.assertFalse(self.store.exists("FAILPERM"))

    def test_save_failure_downgrades_to_partial_and_keeps_the_old_record(self):
        self.store.save("FAKE", {"analysis": {"note": "old"}})
        before = self.store.path_for("FAKE").read_bytes()
        stages, _ = pipeline.make_stub_stages(store=self.store)
        with mock.patch("store.os.replace", side_effect=OSError("disk full")):
            run = pipeline.run_pipeline(["FAKE"], 30, stages)
        r = run.results[0]
        self.assertEqual(r.status, "partial")
        self.assertIn("save", r.stage_errors)
        self.assertEqual(self.store.path_for("FAKE").read_bytes(), before)

    def test_damaged_prior_is_reported_and_run_continues(self):
        stages, _ = pipeline.make_stub_stages(store=self.store)
        self.store.path_for("STALE429").write_bytes(b"garbage")
        run = pipeline.run_pipeline(["STALE429", "FAKE"], 30, stages)
        by = {r.ticker: r for r in run.results}
        self.assertEqual(by["STALE429"].status, "partial")
        self.assertIn("load_prior", by["STALE429"].stage_errors)
        self.assertEqual(by["FAKE"].status, "ok")


if __name__ == "__main__":
    unittest.main()
