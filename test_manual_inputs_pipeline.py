"""
test_manual_inputs_pipeline.py
Version: Step 11b, v0.11.1 (2026-11-07).
Tests for the manual-input helpers in pipeline.py (status rows, save with backup, restore after a
restart). GitHub is replaced by the in-memory fake from test_persistence.py. No network needed.
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
import persistence as P
import pipeline
from errors import StoreError
from test_persistence import FakeGitHub, TOKEN

PDF = b"%PDF-1.4 a source document"
TODAY = date.today()


class InputsCase(unittest.TestCase):
    """Real folders in a temp directory, with permanent storage off unless a test turns it on."""

    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "data"
        for name, value in (("DATA_DIR", self.base), ("MANUAL_INPUTS_DIR", self.base / "manual_inputs")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def turn_storage_on(self):
        self.fake = FakeGitHub()
        self.mirror = P.GitHubMirror("me/data", TOKEN, http=self.fake, root=self.base, tree_ttl=0)
        pipeline.set_mirror(self.mirror)


class StatusTests(InputsCase):
    def test_a_new_ticker_shows_every_field_as_missing(self):
        rows = pipeline.manual_status("TCS")
        self.assertEqual([r["field"] for r in rows], list(config.MANUAL_FIELDS))
        for r in rows:
            self.assertEqual(r["status"], "missing")
            self.assertIsNone(r["value"])
            self.assertFalse(r["blank"])
            self.assertTrue(r["label"])

    def test_a_saved_value_shows_as_fresh_with_its_details(self):
        pipeline.save_manual_input("TCS", "promoter_pledge_pct", "12.5", TODAY)
        row = {r["field"]: r for r in pipeline.manual_status("TCS")}["promoter_pledge_pct"]
        self.assertEqual((row["status"], row["value"], row["as_of"]), ("fresh", 12.5, TODAY.isoformat()))
        self.assertIsNone(row["source_file"])

    def test_old_values_show_as_stale_and_blanks_are_marked(self):
        old = TODAY - timedelta(days=config.MANUAL_INPUT_MAX_AGE_DAYS + 1)
        pipeline.save_manual_input("TCS", "fii_holding_change", "1.5", old)
        pipeline.save_manual_input("TCS", "dii_holding_change", None, TODAY, blank=True)
        rows = {r["field"]: r for r in pipeline.manual_status("TCS")}
        self.assertEqual(rows["fii_holding_change"]["status"], "stale")
        self.assertEqual(rows["dii_holding_change"]["status"], "fresh")
        self.assertTrue(rows["dii_holding_change"]["blank"])
        self.assertIsNone(rows["dii_holding_change"]["value"])

    def test_an_invalid_ticker_is_rejected_and_a_damaged_file_is_a_store_error(self):
        with self.assertRaises(pipeline.InputRejected):
            pipeline.manual_status("bad/ticker")
        pipeline.default_manual_store().values_dir.mkdir(parents=True)
        pipeline.default_manual_store().path_for("TCS").write_bytes(b"garbage")
        with self.assertRaises(StoreError):
            pipeline.manual_status("TCS")


class SaveTests(InputsCase):
    def test_with_storage_off_the_save_works_and_the_note_is_empty(self):
        res = pipeline.save_manual_input("TCS", "next_result_date", "2026-12-15", TODAY)
        self.assertEqual(res.backup_note, "")
        self.assertEqual(res.entry.value, "2026-12-15")
        self.assertTrue(pipeline.default_manual_store().path_for("TCS").exists())

    def test_the_record_and_its_source_document_are_copied_to_permanent_storage(self):
        self.turn_storage_on()
        res = pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY,
                                         source_name="pattern.pdf", source_bytes=PDF)
        self.assertEqual(res.backup_note, "backed up")
        self.assertIn("manual_inputs/values/TCS.json", self.fake.files)
        self.assertIn(f"manual_inputs/sources/{res.entry.source_file}", self.fake.files)
        self.assertEqual(self.fake.files[f"manual_inputs/sources/{res.entry.source_file}"], PDF)

    def test_a_value_without_a_document_copies_only_the_record(self):
        self.turn_storage_on()
        pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY)
        self.assertEqual(list(self.fake.files), ["manual_inputs/values/TCS.json"])

    def test_a_failed_backup_never_undoes_the_save_and_says_so(self):
        self.turn_storage_on()
        self.fake.hook = lambda m, u: (500, b'{"message":"boom"}')
        res = pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY)
        self.assertTrue(res.backup_note.startswith("not backed up"))
        self.assertEqual(pipeline.default_manual_store().get("TCS")["promoter_pledge_pct"].value, 5.0)

    def test_refused_input_saves_and_copies_nothing(self):
        self.turn_storage_on()
        for kwargs in ({"value": "150"}, {"value": "abc"}, {"value": ""}):
            with self.assertRaises(pipeline.InputRejected):
                pipeline.save_manual_input("TCS", "promoter_pledge_pct", kwargs["value"], TODAY)
        with self.assertRaises(pipeline.InputRejected):
            pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY,
                                       source_name="x.exe", source_bytes=PDF)
        self.assertEqual(self.fake.files, {})
        self.assertFalse(pipeline.default_manual_store().path_for("TCS").exists())

    def test_a_second_save_updates_the_remote_copy(self):
        self.turn_storage_on()
        pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY)
        pipeline.save_manual_input("TCS", "fii_holding_change", "1", TODAY)
        remote = self.fake.files["manual_inputs/values/TCS.json"]
        self.assertIn(b"fii_holding_change", remote)
        self.assertIn(b"promoter_pledge_pct", remote)


class RestoreTests(InputsCase):
    def saved_with_document(self):
        self.turn_storage_on()
        res = pipeline.save_manual_input("TCS", "promoter_pledge_pct", "5", TODAY,
                                         source_name="pattern.pdf", source_bytes=PDF)
        store = pipeline.default_manual_store()
        before = store.path_for("TCS").read_bytes()
        store.path_for("TCS").unlink()                                  # simulate the app restarting empty
        (store.sources_dir / res.entry.source_file).unlink()
        return res, store, before

    def test_restore_from_backup_brings_back_manual_inputs_after_a_restart(self):
        res, store, before = self.saved_with_document()
        out = pipeline.restore_from_backup()
        self.assertTrue(out.ok)
        self.assertIn("manual_inputs/values/TCS.json", out.restored)
        self.assertEqual(store.path_for("TCS").read_bytes(), before)
        self.assertEqual((store.sources_dir / res.entry.source_file).read_bytes(), PDF)
        self.assertEqual(store.get("TCS")["promoter_pledge_pct"].value, 5.0)

    def test_ensure_local_manual_inputs_restores_on_demand_and_never_overwrites(self):
        res, store, before = self.saved_with_document()
        pipeline.ensure_local_manual_inputs()
        self.assertEqual(store.path_for("TCS").read_bytes(), before)
        store.path_for("TCS").write_bytes(before.replace(b"5.0", b"6.0"))
        pipeline.ensure_local_manual_inputs()
        self.assertIn(b"6.0", store.path_for("TCS").read_bytes())      # local copy kept

    def test_both_helpers_are_quiet_when_storage_is_off(self):
        self.assertIsNone(pipeline.restore_from_backup())
        pipeline.ensure_local_manual_inputs()                           # no error


if __name__ == "__main__":
    unittest.main()
