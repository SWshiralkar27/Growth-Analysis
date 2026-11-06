"""
test_dalal_probe.py
Version: Step 10b, v0.10.0 (2026-11-06).
Tests for dalal_probe.py with a FAKE dalal module. No network, no dalal package needed.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import dalal_probe as P


def fake(**over):
    ns = types.SimpleNamespace(
        lookup=lambda t: [{"scripcode": "532977"}],
        shareholding=lambda t: [{"date": "2026-09-30", "name": "X", "promoter": {"pct": 55.0}}],
        announcements=lambda t: {"data": [{"desc": "hello"}] * 5},
        block_deals=lambda: [],
        fundamentals=lambda code: {"periods": []},
        meta=lambda code: {"roe": 1},
    )
    for k, v in over.items():
        if v is None:
            delattr(ns, k)
        else:
            setattr(ns, k, v)
    return ns


class ProbeTests(unittest.TestCase):
    def test_reports_functions_calls_and_scrip_code(self):
        out = P.probe("XYZ", module=fake())
        self.assertTrue(out["installed"])
        self.assertEqual(out["scrip_code"], "532977")
        self.assertIn("shareholding", [f["name"] for f in out["functions"]])
        labels = [c["label"] for c in out["calls"]]
        self.assertIn("shareholding(XYZ)", labels)
        self.assertIn("shareholding(532977)", labels)
        self.assertTrue(all("_payload" not in c for c in out["calls"]))

    def test_keys_nested_keys_and_row_count_are_shown(self):
        out = P.probe("XYZ", module=fake())
        sh = next(c for c in out["calls"] if c["label"] == "shareholding(XYZ)")["result"]
        self.assertEqual(sh["first_row_keys"], ["date", "name", "promoter"])
        self.assertEqual(sh["first_row_nested_keys"], {"promoter": ["pct"]})
        ann = next(c for c in out["calls"] if c["label"] == "announcements(XYZ)")["result"]
        self.assertEqual(ann["row_count"], 5)
        self.assertEqual(len(ann["sample_rows"]), P.MAX_ROWS)

    def test_a_failing_call_is_data_not_a_crash_and_skips_code_calls(self):
        def boom(t):
            raise RuntimeError("HTTP 404")
        out = P.probe("XYZ", module=fake(lookup=boom))
        self.assertFalse(out["calls"][0]["ok"])
        self.assertIn("HTTP 404", out["calls"][0]["error"])
        self.assertIsNone(out["scrip_code"])
        self.assertTrue(any(c["label"] == "scrip-code calls" for c in out["calls"]))

    def test_a_missing_function_is_reported(self):
        out = P.probe("XYZ", module=fake(announcements=None))
        ann = next(c for c in out["calls"] if c["label"] == "announcements(XYZ)")
        self.assertFalse(ann["ok"])
        self.assertIn("does not exist", ann["error"])

    def test_long_values_are_cut(self):
        big = [{"desc": "x" * 5000}]
        out = P.probe("XYZ", module=fake(announcements=lambda t: big))
        ann = next(c for c in out["calls"] if c["label"] == "announcements(XYZ)")["result"]
        self.assertLess(len(ann["sample_rows"][0]), P.MAX_TEXT + 30)

    def test_not_installed_is_reported(self):
        with mock.patch.dict(sys.modules, {"dalal": None}):
            out = P.probe("XYZ")
        self.assertFalse(out["installed"])


if __name__ == "__main__":
    unittest.main()