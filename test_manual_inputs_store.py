"""
test_manual_inputs_store.py
Version: Step 11a, v0.11.0 (2026-11-07).
Tests for manual_inputs_store.py. Every test writes into a temporary folder; no network needed.
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
import manual_inputs_store as M
from errors import StoreError

TODAY = date(2026, 11, 7)
PDF = b"%PDF-1.4 a source document"


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "manual_inputs"
        self.store = M.ManualInputStore(self.root)

    def ago(self, days):
        return TODAY - timedelta(days=days)

    def save(self, field="promoter_pledge_pct", value="12.5", as_of=None, **kw):
        return self.store.save("TCS", field, value, as_of or TODAY, today=TODAY, **kw)

    def values_files(self):
        d = self.store.values_dir
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


class ConfigTests(unittest.TestCase):
    def test_settings_match_the_approved_decisions(self):
        self.assertEqual(config.MANUAL_INPUT_MAX_AGE_DAYS, 90)
        self.assertEqual(list(config.MANUAL_FIELDS),
                         ["promoter_pledge_pct", "fii_holding_change", "dii_holding_change", "next_result_date"])

    def test_saved_inputs_are_inside_the_backed_up_folders(self):
        rel = "manual_inputs/values/TCS.json"
        self.assertTrue(any(rel.startswith(p) for p in config.PERSIST_PREFIXES))
        rel = "manual_inputs/sources/TCS_promoter_pledge_pct_2026_11_07_abc.pdf"
        self.assertTrue(any(rel.startswith(p) for p in config.PERSIST_PREFIXES))


class ParseTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(M.parse_value("promoter_pledge_pct", "12.5%"), 12.5)
        self.assertEqual(M.parse_value("promoter_pledge_pct", " 0 "), 0.0)
        self.assertEqual(M.parse_value("fii_holding_change", -1.25), -1.25)
        self.assertEqual(M.parse_value("promoter_pledge_pct", 5), 5.0)

    def test_bad_numbers_are_refused(self):
        for raw in ("abc", "nan", "inf", True, "", None, "-1", "101"):
            with self.assertRaises(M.InputRejected, msg=repr(raw)):
                M.parse_value("promoter_pledge_pct", raw)
        with self.assertRaises(M.InputRejected):
            M.parse_value("fii_holding_change", "-51")

    def test_dates(self):
        for raw in ("2026-12-15", "15-12-2026", "15/12/2026", "15-DEC-2026"):
            self.assertEqual(M.parse_value("next_result_date", raw, TODAY), "2026-12-15", raw)

    def test_bad_dates_are_refused(self):
        for raw in ("1999-12-31", "not a date", "2028-01-01", None):
            with self.assertRaises(M.InputRejected, msg=repr(raw)):
                M.parse_value("next_result_date", raw, TODAY)

    def test_unknown_field_and_ticker_are_refused(self):
        with self.assertRaises(M.InputRejected):
            M.parse_value("shoe_size", "1")
        for bad in ("../x", "", "tcs", "A/B", None):
            with self.assertRaises(M.InputRejected, msg=repr(bad)):
                M.check_ticker(bad)

    def test_as_of_cannot_be_future_or_ancient(self):
        with self.assertRaises(M.InputRejected):
            M.parse_as_of(TODAY + timedelta(days=1), TODAY)
        with self.assertRaises(M.InputRejected):
            M.parse_as_of("1999-01-01", TODAY)
        self.assertEqual(M.parse_as_of("2026-11-07", TODAY), TODAY)


class SaveAndGetTests(Case):
    def test_round_trip(self):
        entry = self.save()
        self.assertEqual(entry.value, 12.5)
        got = self.store.get("TCS")["promoter_pledge_pct"]
        self.assertEqual((got.value, got.as_of, got.ticker, got.field), (12.5, "2026-11-07", "TCS", "promoter_pledge_pct"))
        self.assertTrue(got.saved_at)

    def test_nothing_saved_gives_an_empty_result(self):
        self.assertEqual(self.store.get("TCS"), {})
        self.assertEqual(self.store.list_tickers(), [])

    def test_saving_one_field_keeps_the_others_and_replaces_the_same_one(self):
        self.save("promoter_pledge_pct", "10")
        self.save("fii_holding_change", "1.5")
        self.save("promoter_pledge_pct", "11")
        got = self.store.get("TCS")
        self.assertEqual((got["promoter_pledge_pct"].value, got["fii_holding_change"].value), (11.0, 1.5))
        self.assertEqual(self.values_files(), ["TCS.json"])

    def test_blank_is_saved_as_none_not_zero(self):
        self.save("dii_holding_change", None, blank=True)
        got = self.store.get("TCS")["dii_holding_change"]
        self.assertIsNone(got.value)
        self.assertEqual(self.store.freshness(got, TODAY), "fresh")
        self.assertNotIn("dii_holding_change", self.store.usable_values("TCS", TODAY))

    def test_a_refused_input_changes_nothing(self):
        self.save("promoter_pledge_pct", "10")
        before = self.store.path_for("TCS").read_bytes()
        for kw in ({"value": "999"}, {"value": "abc"}, {"as_of": TODAY + timedelta(days=2)}):
            with self.assertRaises(M.InputRejected):
                self.save(**kw)
        self.assertEqual(self.store.path_for("TCS").read_bytes(), before)

    def test_list_tickers_ignores_temp_and_set_aside_files(self):
        self.save()
        (self.store.values_dir / ".TCS.crashed.tmp").write_text("{half", encoding="utf-8")
        (self.store.values_dir / "TCS.corrupt-2026").write_text("x", encoding="utf-8")
        self.assertEqual(self.store.list_tickers(), ["TCS"])


class FreshnessTests(Case):
    def test_limit_is_90_days_inclusive(self):
        entry = self.save(as_of=self.ago(90))
        self.assertEqual(self.store.freshness(entry, TODAY), "fresh")
        old = self.save("fii_holding_change", "1", as_of=self.ago(91))
        self.assertEqual(self.store.freshness(old, TODAY), "stale")
        self.assertEqual(self.store.freshness(None, TODAY), "missing")

    def test_limit_is_read_from_config(self):
        entry = self.save(as_of=self.ago(31))
        with mock.patch.object(config, "MANUAL_INPUT_MAX_AGE_DAYS", 30):
            self.assertEqual(self.store.freshness(entry, TODAY), "stale")

    def test_usable_values_skip_stale_and_blank_ones(self):
        self.save("promoter_pledge_pct", "10")
        self.save("fii_holding_change", "2", as_of=self.ago(200))
        self.save("dii_holding_change", None, blank=True)
        self.assertEqual(self.store.usable_values("TCS", TODAY), {"promoter_pledge_pct": 10.0})

    def test_pending_lists_missing_and_stale_in_field_order(self):
        self.save("promoter_pledge_pct", "10")
        self.save("dii_holding_change", "1", as_of=self.ago(120))
        self.assertEqual(self.store.pending("TCS", TODAY),
                         ["fii_holding_change", "dii_holding_change", "next_result_date"])
        self.assertEqual(self.store.pending("NOSUCH", TODAY), list(config.MANUAL_FIELDS))


class SourceDocumentTests(Case):
    def test_source_is_stored_with_its_hash_and_recorded(self):
        entry = self.save(source_name="Shareholding pattern.PDF", source_bytes=PDF)
        self.assertEqual(len(entry.source_hash), 64)
        self.assertTrue(entry.source_file.startswith("TCS_promoter_pledge_pct_2026_11_07_"))
        self.assertTrue(entry.source_file.endswith(".pdf"))
        self.assertEqual((self.store.sources_dir / entry.source_file).read_bytes(), PDF)
        self.assertEqual(self.store.get("TCS")["promoter_pledge_pct"].source_hash, entry.source_hash)

    def test_the_same_document_is_stored_once(self):
        a = self.save(source_name="a.pdf", source_bytes=PDF)
        b = self.save(source_name="b.pdf", source_bytes=PDF)
        self.assertEqual(a.source_file, b.source_file)
        self.assertEqual(len(list(self.store.sources_dir.iterdir())), 1)

    def test_a_different_document_never_overwrites_the_first(self):
        a = self.save(source_name="a.pdf", source_bytes=PDF)
        b = self.save(source_name="a.pdf", source_bytes=PDF + b"more")
        self.assertNotEqual(a.source_file, b.source_file)
        self.assertEqual((self.store.sources_dir / a.source_file).read_bytes(), PDF)

    def test_refused_documents_leave_the_record_unchanged(self):
        self.save("promoter_pledge_pct", "10")
        before = self.store.path_for("TCS").read_bytes()
        with mock.patch.object(config, "MANUAL_SOURCE_MAX_MB", 0.0001):
            with self.assertRaises(M.InputRejected):
                self.save(source_name="big.pdf", source_bytes=PDF + b"x" * 400)
        for name, data in (("x.exe", PDF), ("empty.pdf", b""), ("noext", PDF)):
            with self.assertRaises(M.InputRejected, msg=name):
                self.save(source_name=name, source_bytes=data)
        with self.assertRaises(M.InputRejected):
            self.save(source_name="a.pdf")          # name without content
        self.assertEqual(self.store.path_for("TCS").read_bytes(), before)


class SafetyTests(Case):
    def test_failed_swap_keeps_the_old_record_and_leaves_no_temp_files(self):
        self.save("promoter_pledge_pct", "10")
        before = self.store.path_for("TCS").read_bytes()
        with mock.patch("manual_inputs_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(StoreError):
                self.save("promoter_pledge_pct", "20")
        self.assertEqual(self.store.path_for("TCS").read_bytes(), before)
        self.assertEqual(self.values_files(), ["TCS.json"])

    def test_damaged_file_is_set_aside_not_overwritten(self):
        self.store.values_dir.mkdir(parents=True)
        path = self.store.path_for("TCS")
        path.write_bytes(b"{not json")
        with self.assertRaises(StoreError) as ctx:
            self.store.get("TCS")
        self.assertIn("damaged", str(ctx.exception))
        kept = [p for p in self.store.values_dir.iterdir() if ".corrupt-" in p.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), b"{not json")
        self.assertEqual(self.store.get("TCS"), {})

    def test_a_save_never_overwrites_a_damaged_file(self):
        self.store.values_dir.mkdir(parents=True)
        self.store.path_for("TCS").write_bytes(b"garbage")
        with self.assertRaises(StoreError):
            self.save()
        self.assertTrue(any(".corrupt-" in n for n in self.values_files()))

    def test_unknown_schema_is_refused_and_untouched(self):
        self.store.values_dir.mkdir(parents=True)
        path = self.store.path_for("TCS")
        path.write_text('{"schema_version": 99, "fields": {}}', encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(StoreError):
            self.store.get("TCS")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()