"""
test_manual_scoring.py
Version: Step 12, v0.12.0 (2026-11-07). Code version 2026-11-step12.

Proves that saved manual values now count in the score (pledge, and FII plus DII when both are
fresh), that blank, missing and expired values change nothing, that next_result_date is not
scored, and that the inputs are never changed. score_card.py is not touched by this step.
Needs no network, pandas, yfinance, dalal, google-genai or Streamlit.
"""
import unittest

import pipeline
import score_card

LLM = {"factors": [], "governance": []}


def fin(**extra):
    base = {"ticker": "TEST", "sector": "Industrials"}
    base.update(extra)
    return base


def entry(value, status="fresh", blank=False, as_of="2026-11-07"):
    return {"status": status, "blank": blank, "value": value, "as_of": as_of}


def manual(**fields):
    """manual(promoter_pledge_pct=entry(30.0), ...) -> the dict manual_inputs_stage returns."""
    values = {k: v["value"] for k, v in fields.items()
              if v["status"] == "fresh" and not v["blank"] and v["value"] is not None}
    return {"fields": dict(fields), "values": values, "warning": None}


def breakdown_of(f):
    return score_card.compute_score(f, LLM, peers=None)["breakdown"]


class ApplyManualInputs(unittest.TestCase):
    def test_no_manual_values_changes_nothing(self):
        f = fin(revenue_growth=0.1)
        for m in (None, {}, {"fields": {}, "values": {}, "warning": None}, "junk", 5):
            out, labels = pipeline.apply_manual_inputs(f, m)
            self.assertEqual(out, f)
            self.assertEqual(labels, [])

    def test_inputs_are_not_mutated(self):
        f = fin(promoter_pledge_pct=2.0)
        m = manual(promoter_pledge_pct=entry(30.0))
        before_f, before_m = dict(f), {k: v for k, v in m.items()}
        out, _ = pipeline.apply_manual_inputs(f, m)
        self.assertEqual(f, before_f)
        self.assertEqual(m, before_m)
        self.assertIsNot(out, f)

    def test_none_financials_is_safe(self):
        out, labels = pipeline.apply_manual_inputs(None, manual(promoter_pledge_pct=entry(30.0)))
        self.assertEqual(out["promoter_pledge_pct"], 30.0)

    def test_pledge_above_ten_costs_half_of_it(self):
        out, labels = pipeline.apply_manual_inputs(fin(), manual(promoter_pledge_pct=entry(30.0)))
        self.assertEqual(breakdown_of(out)["governance_penalty"], -15.0)
        self.assertEqual(breakdown_of(fin())["governance_penalty"], 0.0)
        self.assertTrue(any("promoter pledge 30%" in s and "2026-11-07" in s for s in labels))

    def test_pledge_penalty_is_capped_at_twenty(self):
        out, _ = pipeline.apply_manual_inputs(fin(), manual(promoter_pledge_pct=entry(80.0)))
        self.assertEqual(breakdown_of(out)["governance_penalty"], -20.0)

    def test_pledge_at_or_below_ten_costs_nothing(self):
        for v in (0.0, 5.0, 10.0):
            out, labels = pipeline.apply_manual_inputs(fin(), manual(promoter_pledge_pct=entry(v)))
            self.assertEqual(breakdown_of(out)["governance_penalty"], 0.0)
            self.assertTrue(labels)

    def test_manual_pledge_replaces_fetched_and_says_so(self):
        out, labels = pipeline.apply_manual_inputs(
            fin(promoter_pledge_pct=2.0), manual(promoter_pledge_pct=entry(30.0)))
        self.assertEqual(out["promoter_pledge_pct"], 30.0)
        self.assertTrue(any("replaces fetched value 2%" in s for s in labels))

    def test_blank_pledge_changes_nothing(self):
        m = manual(promoter_pledge_pct=entry(None, blank=True))
        out, labels = pipeline.apply_manual_inputs(fin(), m)
        self.assertNotIn("promoter_pledge_pct", out)
        self.assertEqual(labels, [])

    def test_expired_pledge_is_not_counted_and_is_named(self):
        m = manual(promoter_pledge_pct=entry(30.0, status="stale", as_of="2026-06-01"))
        out, labels = pipeline.apply_manual_inputs(fin(), m)
        self.assertNotIn("promoter_pledge_pct", out)
        self.assertEqual(breakdown_of(out)["governance_penalty"], 0.0)
        self.assertTrue(any("older than 90 days" in s and "not counted" in s for s in labels))

    def test_missing_pledge_changes_nothing(self):
        out, labels = pipeline.apply_manual_inputs(
            fin(), manual(promoter_pledge_pct=entry(None, status="missing", as_of=None)))
        self.assertNotIn("promoter_pledge_pct", out)
        self.assertEqual(labels, [])

    def test_fii_plus_dii_feed_institutional_signal(self):
        m = manual(fii_holding_change=entry(1.5), dii_holding_change=entry(0.5))
        out, labels = pipeline.apply_manual_inputs(fin(), m)
        self.assertAlmostEqual(out["institutional_holding_change"], 2.0)
        # signal at the 0.35 prior is 0.525; at +2.0pp it is (2+2)/5 * 1.5 = 1.2, a gain of 0.675.
        # The scorer rounds its breakdown to one decimal, so allow for that.
        gain = breakdown_of(out)["fin_valuation"] - breakdown_of(fin())["fin_valuation"]
        self.assertAlmostEqual(gain, 0.675, delta=0.06)
        self.assertTrue(any("institutional holding change +2pp" in s for s in labels))

    def test_negative_flows_can_lower_the_signal(self):
        m = manual(fii_holding_change=entry(-2.0), dii_holding_change=entry(-1.0))
        out, _ = pipeline.apply_manual_inputs(fin(), m)
        self.assertAlmostEqual(out["institutional_holding_change"], -3.0)
        self.assertLess(breakdown_of(out)["fin_valuation"], breakdown_of(fin())["fin_valuation"])

    def test_only_one_of_fii_dii_is_not_counted(self):
        for m in (manual(fii_holding_change=entry(1.0)),
                  manual(dii_holding_change=entry(1.0)),
                  manual(fii_holding_change=entry(1.0), dii_holding_change=entry(None, blank=True))):
            out, labels = pipeline.apply_manual_inputs(fin(), m)
            self.assertNotIn("institutional_holding_change", out)
            self.assertTrue(any("both must be fresh" in s for s in labels))

    def test_expired_fii_dii_is_named(self):
        m = manual(fii_holding_change=entry(1.0, status="stale", as_of="2026-05-01"),
                   dii_holding_change=entry(1.0, status="stale", as_of="2026-05-01"))
        out, labels = pipeline.apply_manual_inputs(fin(), m)
        self.assertNotIn("institutional_holding_change", out)
        self.assertTrue(any("FII/DII is older than 90 days" in s for s in labels))

    def test_next_result_date_is_not_scored(self):
        m = manual(next_result_date=entry("2026-11-20"))
        out, labels = pipeline.apply_manual_inputs(fin(), m)
        self.assertEqual(out, fin())
        self.assertEqual(labels, [])

    def test_bad_values_are_ignored(self):
        for bad in (True, "30", float("nan"), float("inf"), 150.0, -5.0):
            m = {"fields": {}, "values": {"promoter_pledge_pct": bad}}
            out, _ = pipeline.apply_manual_inputs(fin(), m)
            self.assertNotIn("promoter_pledge_pct", out)

    def test_manual_pledge_does_not_lift_the_governance_cap(self):
        out, _ = pipeline.apply_manual_inputs(fin(), manual(promoter_pledge_pct=entry(0.0)))
        self.assertNotIn("governance_sources", out)
        self.assertNotIn("promoter_pledge_pct_verified", out)


class ScoreStage(unittest.TestCase):
    def run_stage(self, financials, m):
        return pipeline.score_stage_v21("TEST", LLM, financials, {}, m, 30)

    def test_no_manual_score_is_unchanged(self):
        base = self.run_stage(fin(), {})
        for m in (None, {"fields": {}, "values": {}, "warning": None}):
            again = self.run_stage(fin(), m)
            self.assertEqual(again.total, base.total)
            self.assertEqual(again.labels, base.labels)

    def test_pledge_lowers_total_and_is_labelled(self):
        base = self.run_stage(fin(), {})
        scored = self.run_stage(fin(), manual(promoter_pledge_pct=entry(30.0)))
        self.assertAlmostEqual(base.total - scored.total, 15.0, places=1)
        self.assertEqual(scored.components["governance_penalty"]["points"], -15.0)
        self.assertTrue(any(s.startswith("Manual input counted: promoter pledge") for s in scored.labels))
        self.assertTrue(any("Promoter pledge 30.0%" in n
                            for n in scored.components["notes"]["governance_notes"]))

    def test_stage_does_not_change_the_financials_it_was_given(self):
        f = fin()
        self.run_stage(f, manual(promoter_pledge_pct=entry(30.0),
                                 fii_holding_change=entry(1.0), dii_holding_change=entry(1.0)))
        self.assertEqual(f, fin())


if __name__ == "__main__":
    unittest.main()
