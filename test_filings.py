"""
test_filings.py
Version: Step 5, v0.5.0 (2026-11-03).
Tests for filings.py (the upload box's engine) and how the pipeline sees uploaded files.
Run:  python -m unittest discover -s . -p "test_*.py" -v
Every test writes into a temporary folder.
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
import filings
import pdf_extractor as pe
import pipeline
from errors import FilingError
from test_pdf_extractor import make_pdf

PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"
D = date(2026, 5, 12)


class FilingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "filings"
        patcher = mock.patch.object(config, "FILINGS_FOLDER", str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)

    def names(self):
        return sorted(p.name for p in self.dir.iterdir()) if self.dir.exists() else []


class NamingTests(FilingsCase):
    def test_standard_name(self):
        self.assertEqual(filings.filing_name("TCS", D, "Annual Report"), "TCS_2026_05_12_AnnualReport.pdf")
        self.assertEqual(filings.filing_name("M&M", D, "Investor Presentation"),
                         "M&M_2026_05_12_Investor_Presentation.pdf")

    def test_invalid_inputs_are_refused(self):
        bad = [("tcs", D, "Annual Report"), ("", D, "Annual Report"), ("..", D, "Annual Report"),
               ("A/B", D, "Annual Report"), ("TCS", None, "Annual Report"),
               ("TCS", date(1999, 12, 31), "Annual Report"),
               ("TCS", date.today() + timedelta(days=1), "Annual Report"),
               ("TCS", D, "Mystery")]
        for args in bad:
            with self.assertRaises(FilingError, msg=str(args)):
                filings.filing_name(*args)

    def test_every_type_is_graded_and_dated_the_way_the_reader_expects(self):
        expected = {"Annual Report": "A", "Quarterly Results": "A", "Investor Presentation": "B",
                    "Concall Transcript": "B", "Other": "C"}
        for label, grade in expected.items():
            name = filings.filing_name("TCS", D, label)
            self.assertEqual(pe.classify_document(name), grade, label)
            self.assertEqual(pe.parse_doc_date(name, mtime_fallback=False), D, label)


class SaveTests(FilingsCase):
    def test_saves_a_valid_pdf(self):
        res = filings.save_filing("TCS", D, "Annual Report", PDF)
        self.assertEqual(res.status, "saved")
        self.assertEqual((self.dir / res.name).read_bytes(), PDF)
        self.assertEqual(self.names(), ["TCS_2026_05_12_AnnualReport.pdf"])

    def test_refuses_empty_non_pdf_and_oversized_files(self):
        for data in (b"", b"just some text, not a pdf"):
            with self.assertRaises(FilingError):
                filings.save_filing("TCS", D, "Annual Report", data)
        with mock.patch.object(config, "MAX_UPLOAD_MB", 0.0001):   # about 104 bytes
            with self.assertRaises(FilingError) as ctx:
                filings.save_filing("TCS", D, "Annual Report", PDF + b"x" * 300)
        self.assertIn("limit", str(ctx.exception))
        self.assertEqual(self.names(), [])

    def test_same_content_again_is_reported_not_rewritten(self):
        filings.save_filing("TCS", D, "Annual Report", PDF)
        res = filings.save_filing("TCS", D, "Annual Report", PDF)
        self.assertEqual(res.status, "already_present")
        self.assertEqual(len(self.names()), 1)

    def test_different_content_under_the_same_name_is_refused_and_old_file_kept(self):
        filings.save_filing("TCS", D, "Annual Report", PDF)
        other = PDF + b"%different"
        with self.assertRaises(FilingError) as ctx:
            filings.save_filing("TCS", D, "Annual Report", other)
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual((self.dir / "TCS_2026_05_12_AnnualReport.pdf").read_bytes(), PDF)

    def test_failed_swap_leaves_no_file_and_no_temp_files(self):
        with mock.patch("filings.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(FilingError):
                filings.save_filing("TCS", D, "Annual Report", PDF)
        self.assertEqual(self.names(), [])


class ListAndRemoveTests(FilingsCase):
    def test_lists_only_this_ticker_newest_first_and_ignores_temp_files(self):
        filings.save_filing("TCS", date(2026, 5, 12), "Annual Report", PDF)
        filings.save_filing("TCS", date(2026, 8, 10), "Investor Presentation", PDF)
        filings.save_filing("INFY", date(2026, 6, 1), "Annual Report", PDF)
        (self.dir / "TCS.pdf").write_bytes(PDF)
        (self.dir / ".TCS_2026_09_01_AnnualReport.pdf.123.tmp").write_bytes(b"half")
        items = filings.list_filings("TCS")
        self.assertEqual([f.name for f in items],
                         ["TCS_2026_08_10_Investor_Presentation.pdf",
                          "TCS_2026_05_12_AnnualReport.pdf", "TCS.pdf"])
        self.assertEqual(items[0].type_label, "Investor Presentation")
        self.assertEqual(items[0].doc_date, date(2026, 8, 10))
        self.assertEqual(items[2].type_label, "Unknown")
        self.assertIsNone(items[2].doc_date)

    def test_list_on_a_missing_folder_is_empty(self):
        self.assertEqual(filings.list_filings("TCS"), [])

    def test_remove_deletes_only_that_file(self):
        filings.save_filing("TCS", D, "Annual Report", PDF)
        filings.save_filing("TCS", D, "Quarterly Results", PDF)
        self.assertTrue(filings.remove_filing("TCS", "TCS_2026_05_12_AnnualReport.pdf"))
        self.assertEqual(self.names(), ["TCS_2026_05_12_Results.pdf"])
        self.assertFalse(filings.remove_filing("TCS", "TCS_2026_05_12_AnnualReport.pdf"))

    def test_remove_refuses_other_tickers_and_path_tricks(self):
        filings.save_filing("INFY", D, "Annual Report", PDF)
        for name in ("INFY_2026_05_12_AnnualReport.pdf", "../x.pdf", "TCS_/../y.pdf",
                     ".hidden.pdf", "TCS_2026_05_12_AnnualReport.txt"):
            with self.assertRaises(FilingError, msg=name):
                filings.remove_filing("TCS", name)
        self.assertEqual(self.names(), ["INFY_2026_05_12_AnnualReport.pdf"])


class PipelineSeesUploadsTests(FilingsCase):
    def test_wrappers_round_trip(self):
        res = pipeline.upload_filing("TCS", D, "Annual Report", PDF)
        self.assertEqual([f.name for f in pipeline.stored_filings("TCS")], [res.name])
        self.assertTrue(pipeline.delete_filing("TCS", res.name))
        self.assertEqual(pipeline.stored_filings("TCS"), [])
        self.assertIn("Annual Report", pipeline.FILING_TYPE_LABELS)

    def test_uploaded_readable_pdf_is_read_and_counted_in_the_run(self):
        self.dir.mkdir(parents=True)
        make_pdf(self.dir / "FAKE_2026_05_12_AnnualReport.pdf",
                 ["Order book at a record backlog", "Independent Auditor's Report"])
        run = pipeline.run_pipeline(["FAKE", "STALE429"], 30)
        by = {r.ticker: r for r in run.results}
        self.assertEqual(by["FAKE"].documents, ["FAKE_2026_05_12_AnnualReport.pdf"])
        self.assertEqual(by["STALE429"].documents, [])
        rows = {row["Ticker"]: row for row in run.to_rows()}
        self.assertEqual(rows["FAKE"]["Filings"], 1)
        self.assertEqual(rows["STALE429"]["Filings"], 0)

    def test_failed_ticker_still_reports_zero_filings(self):
        run = pipeline.run_pipeline(["FAILPERM"], 30)
        self.assertEqual(run.to_rows()[0]["Filings"], 0)


if __name__ == "__main__":
    unittest.main()
