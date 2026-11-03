"""
test_pdf_extractor.py
Version: Step 4, v0.4.0 (2026-11-02).
Tests for pdf_extractor.py (ported from the old test_patches.py where they existed,
plus new tests that read small generated PDFs) and for the pipeline's filings stage.
Needs the pypdf package. Run:  python -m unittest discover -s . -p "test_*.py" -v
Every test writes into a temporary folder.
"""
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import pdf_extractor as pe
import pipeline


# ------------------------------------------------------------ PDF builders
def make_pdf(path, page_texts):
    """Write a small valid text PDF (one line of Helvetica text per page)."""
    objs = []
    n_pages = len(page_texts)
    first_page_obj = 4
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(n_pages))
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(page_texts):
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_no = first_page_obj + 2 * i + 1
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    Path(path).write_bytes(bytes(out))


def make_blank_pdf(path):
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with open(path, "wb") as fh:
        w.write(fh)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


# ---------------------------------------------------------------- grading
class GradeAndDateTests(TmpCase):
    def test_grades(self):
        self.assertEqual(pe.classify_document("BEL_2026_05_12_AnnualReport.pdf"), "A")
        self.assertEqual(pe.classify_document("BEL_2026_08_10_Investor_Presentation.pdf"), "B")
        self.assertEqual(pe.classify_document("BEL_2026_08_10_Concall_Transcript.pdf"), "B")
        self.assertEqual(pe.classify_document("BEL_random.pdf"), "C")

    def test_date_from_file_name(self):
        self.assertEqual(pe.parse_doc_date("BEL_2026_05_12_Q4.pdf"), date(2026, 5, 12))
        self.assertEqual(pe.parse_doc_date("BEL_2026_05_Q4.pdf", mtime_fallback=False), date(2026, 5, 1))

    def test_undated_name_falls_back_to_modified_time(self):
        (self.dir / "NoDate_Results.pdf").write_bytes(b"%PDF-1.4")
        with mock.patch.object(config, "FILINGS_FOLDER", str(self.dir)):
            self.assertEqual(pe.parse_doc_date("NoDate_Results.pdf"), date.today())
            self.assertIsNone(pe.parse_doc_date("NoDate_Results.pdf", mtime_fallback=False))
            self.assertIsNone(pe.parse_doc_date("Ghost_Results.pdf"))


# ----------------------------------------------------------- real PDF files
class ExtractDocumentTests(TmpCase):
    def test_reads_text_pages_with_grade_and_date(self):
        path = self.dir / "BEL_2026_05_12_AnnualReport.pdf"
        make_pdf(path, ["Order book at a record backlog", "Independent Auditor's Report"])
        doc = pe.extract_document(str(path))
        self.assertIsNone(doc["error"])
        self.assertFalse(doc["scanned"])
        self.assertEqual(doc["page_count"], 2)
        self.assertEqual([p["page"] for p in doc["pages"]], [1, 2])
        self.assertIn("Order book", doc["pages"][0]["text"])
        self.assertEqual(doc["grade"], "A")
        self.assertEqual(doc["doc_date"], date(2026, 5, 12))
        self.assertEqual(doc["date_source"], "filename")

    def test_blank_pdf_is_flagged_as_scanned(self):
        path = self.dir / "BEL_2026_05_12_Scan.pdf"
        make_blank_pdf(path)
        doc = pe.extract_document(str(path))
        self.assertTrue(doc["scanned"])
        self.assertEqual(doc["pages"], [])

    def test_damaged_pdf_never_raises(self):
        path = self.dir / "BEL_2026_05_12_Bad.pdf"
        path.write_bytes(b"this is not a pdf at all")
        doc = pe.extract_document(str(path))
        self.assertEqual(doc["pages"], [])
        self.assertTrue(doc["error"] or doc["scanned"])

    def test_missing_file_never_raises(self):
        doc = pe.extract_document(str(self.dir / "ghost.pdf"))
        self.assertEqual(doc["pages"], [])
        self.assertIsNotNone(doc["error"])

    def test_page_text_is_truncated_to_the_configured_limit(self):
        path = self.dir / "BEL_2026_05_12_Long.pdf"
        make_pdf(path, ["word " * 400])
        with mock.patch.object(config, "MAX_PAGE_CHARS", 100):
            doc = pe.extract_document(str(path))
        self.assertLessEqual(len(doc["pages"][0]["text"]), 100)


# --------------------------------------------------------- context builder
def make_doc(name, grade, n_pages, d, special):
    pages = []
    for i in range(1, n_pages + 1):
        text = special.get(i, f"Page {i} general narrative text about the company, board and community. " * 40)
        pages.append({"page": i, "text": text[:3000]})
    return {"document": name, "grade": grade, "doc_date": d, "pages": pages}


BIZ = "Order book stands at a record backlog; order inflow strong, capacity expansion and outlook positive with guidance raised. " * 12
AUD = "Independent Auditor's Report. Basis for opinion. Key audit matters. Emphasis of matter. " * 15
RPT = "Related party transactions with promoter group entities were disclosed as related-party balances. " * 15
PLG = "Promoter shares pledged; encumbrance details and pledge invocation disclosures. " * 15


class BuildContextTests(unittest.TestCase):
    CAP = 60_000

    def setUp(self):
        self.annual = make_doc("BEL_2026_05_12_AnnualReport.pdf", "A", 200, date(2026, 5, 12),
                               {**{i: BIZ for i in range(150, 170)}, **{i: AUD for i in range(120, 126)},
                                **{i: RPT for i in range(130, 134)}, **{i: PLG for i in range(140, 143)}})
        self.deck = make_doc("BEL_2026_08_10_Investor_Presentation.pdf", "B", 30, date(2026, 8, 10),
                             {i: BIZ for i in range(5, 12)})
        self.ctx, self.meta = pe.build_context([self.annual, self.deck], max_chars=self.CAP)

    def test_header_format_is_unchanged(self):
        headers = re.findall(r"\[\[DOC: (.+?) \| GRADE: ([ABC]) \| PAGE: (\d+)\]\]", self.ctx)
        self.assertEqual(len(headers), self.meta["pages_included"])

    def test_business_pages_from_both_documents_reach_the_model(self):
        headers = re.findall(r"\[\[DOC: (.+?) \| GRADE: ([ABC]) \| PAGE: (\d+)\]\]", self.ctx)
        sel = {(d, int(p)) for d, _, p in headers}
        biz_annual = sum(1 for d, p in sel if d == self.annual["document"] and 150 <= p <= 169)
        biz_deck = sum(1 for d, p in sel if d == self.deck["document"] and 5 <= p <= 11)
        self.assertGreaterEqual(biz_annual, 6)
        self.assertGreaterEqual(biz_deck, 5)

    def test_all_governance_sections_covered_within_their_share(self):
        self.assertEqual(set(self.meta["governance_coverage"]), {"auditor_report", "related_party", "pledge"})
        self.assertLessEqual(self.meta["selection"]["governance_chars"], self.CAP * 0.30)

    def test_total_stays_within_the_budget_and_index_covers_every_page(self):
        self.assertLessEqual(self.meta["chars"], self.CAP)
        self.assertEqual(self.meta["pages_total"], 230)

    def test_single_document_and_no_documents(self):
        _, m1 = pe.build_context([self.annual], max_chars=self.CAP)
        self.assertGreater(m1["pages_included"], 0)
        self.assertGreater(m1["business_pages"], 0)
        c0, m0 = pe.build_context([], max_chars=self.CAP)
        self.assertEqual(c0, "")
        self.assertEqual(m0["pages_included"], 0)
        self.assertEqual(m0["governance_coverage"], [])


# ------------------------------------------------------------ pipeline stage
class FilingsStageTests(TmpCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "FILINGS_FOLDER", str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_filings_gives_an_empty_context(self):
        out = pipeline.filings_context_stage("TCS")
        self.assertEqual(out["text"], "")
        self.assertEqual(out["documents"], [])
        self.assertEqual(out["meta"]["pages_included"], 0)

    def test_finds_dated_and_exact_named_files_for_this_ticker_only(self):
        make_pdf(self.dir / "TCS_2026_05_12_AnnualReport.pdf",
                 ["Order book at a record backlog", "Independent Auditor's Report"])
        make_pdf(self.dir / "TCS.pdf", ["Capacity expansion and outlook"])
        make_pdf(self.dir / "INFY_2026_05_12_AnnualReport.pdf", ["Not this ticker"])
        out = pipeline.filings_context_stage("TCS")
        self.assertEqual(sorted(out["documents"]), ["TCS.pdf", "TCS_2026_05_12_AnnualReport.pdf"])
        self.assertGreater(out["meta"]["pages_included"], 0)
        self.assertNotIn("Not this ticker", out["text"])
        self.assertIn("[[DOC: TCS_2026_05_12_AnnualReport.pdf | GRADE: A |", out["text"])

    def test_unreadable_files_are_listed_not_raised(self):
        make_pdf(self.dir / "TCS_2026_05_12_AnnualReport.pdf", ["Order book backlog"])
        (self.dir / "TCS_2026_06_01_Bad.pdf").write_bytes(b"garbage")
        make_blank_pdf(self.dir / "TCS_2026_07_01_Scan.pdf")
        out = pipeline.filings_context_stage("TCS")
        self.assertEqual(out["documents"], ["TCS_2026_05_12_AnnualReport.pdf"])
        self.assertEqual(sorted(name for name, _ in out["unreadable"]),
                         ["TCS_2026_06_01_Bad.pdf", "TCS_2026_07_01_Scan.pdf"])

    def test_ticker_with_an_ampersand_works(self):
        make_pdf(self.dir / "M&M_2026_05_12_AnnualReport.pdf", ["Order book backlog"])
        out = pipeline.filings_context_stage("M&M")
        self.assertEqual(out["documents"], ["M&M_2026_05_12_AnnualReport.pdf"])

    def test_a_full_run_still_works_with_and_without_filings(self):
        make_pdf(self.dir / "FAKE_2026_05_12_AnnualReport.pdf", ["Order book backlog"])
        run = pipeline.run_pipeline(["FAKE", "OTHER"], 30)
        self.assertEqual([r.status for r in run.results], ["ok", "ok"])
        self.assertNotIn("filings_context", run.results[0].stubbed_stages)


if __name__ == "__main__":
    unittest.main()
