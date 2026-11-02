"""
test_score_card.py
Version: Step 3, v0.3.0 (2026-11-02).
Ported from the score_card section of the old test_patches.py (v2.1 checks),
plus tests for the pipeline's score stage.
Run from the project root:  python -m unittest discover -s . -p "test_*.py" -v
NOTE: the unverified-governance cap value 69 below is the TEST STAND-IN that was in the
old test file. It is not a confirmed setting; the real value is not recovered yet.
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import pipeline
import score_card as sc

NAMES = ["Business Catalysts", "Market Expansion", "Order Book Growth", "Competitive Advantage"]


def factors(score=70, conf="high", n=4):
    return [{"factor": nm, "score": score, "confidence_applied": conf} for nm in NAMES[:n]]


def gov(raised=None, quote="CFO resigned with effect from 1 June"):
    flags = ["auditor_qualification", "auditor_resignation", "related_party_concerns",
             "cfo_exit", "governance_concerns"]
    out = []
    for f in flags:
        on = f == raised
        out.append({"flag": f, "raised": on, "verified": on,
                    "evidence": [{"document": "x.pdf", "page": 12, "quote": quote, "verified": True}] if on else []})
    return out


def llm(score=70, conf="high", raised=None, quote="CFO resigned with effect from 1 June",
        coverage=("auditor_report", "related_party", "pledge")):
    out = {"factors": factors(score, conf), "governance": gov(raised, quote),
           "documents_used": ["BEL_2026_05_12_Q4.pdf"], "pages_included": 40}
    if coverage is not None:
        out["governance_coverage"] = list(coverage)
    return out


def fin(**kw):
    base = {"revenue_growth": 0.15, "ebitda_margin": 0.22, "debt_to_equity": 0.2, "ocf_to_ebitda": 0.9,
            "interest_coverage": 12.0, "promoter_pledge_pct": 0.0, "scoring_profile": "standard",
            "governance_sources": ["dalal.shareholding", "dalal.announcements"]}
    base.update(kw)
    return base


STRONG = dict(revenue_acceleration=0.05, ebitda_margin_change=0.02, ebit_growth=0.25,
              eps_revision_90d_pct=0.06, eps_net_revisions_30d=3, promoter_holding_change=0.5,
              institutional_holding_change=3.0)


def notes(r):
    return " | ".join(r["breakdown"]["governance_notes"])


class StructureTests(unittest.TestCase):
    def test_api_shape_and_version(self):
        r = sc.compute_score(fin(), llm())
        b = r["breakdown"]
        for k in ("llm_component", "financial_bonus", "governance_penalty", "raw_score",
                  "factor_notes", "financial_notes", "governance_notes"):
            self.assertIn(k, b)
        self.assertTrue(0 <= r["score"] <= 100)
        self.assertEqual(b["scoring_model"], "v2.1")

    def test_component_maximums(self):
        b = sc.compute_score(fin(**STRONG), llm(score=100))["breakdown"]
        self.assertLessEqual(b["financial_bonus"], 40.0 + 1e-9)
        self.assertLessEqual(b["llm_component"], 60.0 + 1e-9)
        self.assertLessEqual(b["fin_level"], 20.0 + 1e-9)
        self.assertLessEqual(b["fin_direction"], 14.0 + 1e-9)
        self.assertLessEqual(b["fin_valuation"], 6.0 + 1e-9)

    def test_revenue_acceleration_and_eps_upgrades_raise_score(self):
        self.assertGreater(sc.compute_score(fin(revenue_acceleration=0.05), llm())["score"],
                           sc.compute_score(fin(revenue_acceleration=-0.05), llm())["score"])
        self.assertGreater(sc.compute_score(fin(eps_revision_90d_pct=0.06), llm())["score"],
                           sc.compute_score(fin(eps_revision_90d_pct=-0.06), llm())["score"])

    def test_no_cliff_at_20_percent_revenue_growth(self):
        lo = sc.compute_score(fin(revenue_growth=0.199), llm())["score"]
        hi = sc.compute_score(fin(revenue_growth=0.201), llm())["score"]
        self.assertLess(abs(hi - lo), 0.1)

    def test_ordering_weakness_lt_no_evidence_lt_neutral(self):
        def comp(score, conf):
            return sc.compute_score(fin(), {**llm(), "factors": factors(score, conf)})["breakdown"]["llm_component"]
        weak, none_, neutral = comp(20, "high"), comp(50, "none"), comp(50, "medium")
        self.assertLess(weak, none_)
        self.assertLess(none_, neutral)

    def test_financials_profile_excludes_order_book_and_renormalises(self):
        facs = [*factors(70, "high", 2), {"factor": "Order Book Growth", "score": 50, "confidence_applied": "none"},
                {"factor": "Competitive Advantage", "score": 70, "confidence_applied": "high"}]
        val = sc.compute_score(fin(scoring_profile="financials", return_on_equity=0.16),
                               {**llm(), "factors": facs})["breakdown"]["llm_component"]
        self.assertAlmostEqual(val, 42.0, delta=0.1)

    def test_rating_bands(self):
        for score, name in [(85, "Strong Candidate"), (84.9, "Good Candidate"), (70, "Good Candidate"),
                            (69.9, "Mixed Signals"), (55, "Mixed Signals"), (54.9, "Speculative"),
                            (40, "Speculative"), (39.9, "Avoid")]:
            self.assertEqual(sc.rating_for_score(score), name, score)


class OrderBookRuleTests(unittest.TestCase):
    three = [*factors(80, "high", 2), {"factor": "Order Book Growth", "score": 50, "confidence_applied": "none"},
             {"factor": "Competitive Advantage", "score": 80, "confidence_applied": "high"}]

    def ob(self, sector, factor_list=None):
        return sc.compute_score(fin(sector=sector), {**llm(), "factors": factor_list or self.three})["breakdown"]

    def test_excluded_for_healthcare(self):
        hc = self.ob("Healthcare")
        self.assertAlmostEqual(hc["llm_component"], 48.0, delta=0.1)
        self.assertTrue(any("excluded" in n for n in hc["factor_notes"]))

    def test_counted_at_prior_for_industrials_and_unknown_sector(self):
        self.assertAlmostEqual(self.ob("Industrials")["llm_component"], 41.2, delta=0.2)
        nosec = sc.compute_score(fin(), {**llm(), "factors": self.three})["breakdown"]
        self.assertAlmostEqual(nosec["llm_component"], 41.2, delta=0.2)

    def test_evidence_is_always_counted(self):
        with_ev = [*factors(80, "high", 2), {"factor": "Order Book Growth", "score": 80, "confidence_applied": "medium"},
                   {"factor": "Competitive Advantage", "score": 80, "confidence_applied": "high"}]
        self.assertFalse(any("excluded" in n for n in self.ob("Healthcare", with_ev)["factor_notes"]))


class GovernanceTests(unittest.TestCase):
    def test_cfo_exit_with_supporting_quote_costs_5(self):
        r = sc.compute_score(fin(), llm(raised="cfo_exit", quote="CFO resigned with effect from 1 June"))
        self.assertEqual(r["breakdown"]["governance_penalty"], -5.0)

    def test_resolved_or_planned_matters_are_waived_and_explained(self):
        adani = sc.compute_score(fin(), llm(raised="governance_concerns",
            quote="indictment filed by US DOJ against the three Directors has been dismissed and all the charges against them have been dropped by EDNY"))
        self.assertEqual(adani["breakdown"]["governance_penalty"], 0.0)
        self.assertIn("penalty waived", notes(adani))
        glaxo = sc.compute_score(fin(), llm(raised="cfo_exit",
            quote="Whole-time Director & Chief Financial Officer, transitioned into new role to lead APAC Finance within the Group, and hence resigned"))
        self.assertEqual(glaxo["breakdown"]["governance_penalty"], 0.0)
        gujgas = sc.compute_score(fin(), llm(raised="cfo_exit", quote="Shri Rajesh Sivadasan (CFO) (w.e.f 6 May 2024)"))
        self.assertEqual(gujgas["breakdown"]["governance_penalty"], 0.0)
        nothing = sc.compute_score(fin(), llm(raised="cfo_exit", quote=""))
        self.assertEqual(nothing["breakdown"]["governance_penalty"], 0.0)

    def test_related_party_quote_describing_assistance_stands(self):
        r = sc.compute_score(fin(), llm(raised="related_party_concerns",
            quote="Providing financial assistance to AREDL... Being the promoter of the Company."))
        self.assertEqual(r["breakdown"]["governance_penalty"], -8.0)

    def test_exchange_only_auditor_resignation_reduced_penalty_no_gate(self):
        r = sc.compute_score(fin(**STRONG, ann_auditor_resignation=1), llm(score=95))
        self.assertEqual(r["breakdown"]["governance_penalty"], -10.0)
        self.assertGreater(r["score"], 54)
        self.assertIn("verify", notes(r))

    def test_corroborated_auditor_resignation_full_penalty_and_gate(self):
        r = sc.compute_score(fin(**STRONG, ann_auditor_resignation=1),
                             llm(score=95, raised="auditor_resignation", quote="Statutory auditor resigned with effect from 31 July"))
        self.assertEqual(r["breakdown"]["governance_penalty"], -20.0)
        self.assertLessEqual(r["score"], 54)

    def test_auditor_qualification_gates_the_score(self):
        r = sc.compute_score(fin(**STRONG), llm(score=95, raised="auditor_qualification", quote="Qualified opinion on standalone results"))
        self.assertLessEqual(r["score"], 54)
        self.assertEqual(sc.rating_for_score(r["score"]), "Speculative")

    def test_exchange_pledge_invocation_keeps_full_weight_and_gates(self):
        r = sc.compute_score(fin(**STRONG, ann_pledge_invocation=1), llm(score=95))
        self.assertLessEqual(r["score"], 54)
        self.assertEqual(r["breakdown"]["governance_penalty"], -20.0)

    def test_exchange_only_cfo_exit_costs_4_and_events_are_readable(self):
        self.assertEqual(sc.compute_score(fin(ann_cfo_exit=1), llm())["breakdown"]["governance_penalty"], -4.0)
        r = sc.compute_score(fin(ann_cfo_exit=1, ann_events=[{"event": "ann_cfo_exit", "date": "2026-09-01",
                             "text": "Intimation of resignation of Chief Financial Officer"}]), llm())
        self.assertIn("2026-09-01 Intimation of resignation", notes(r))

    def test_promoter_pledge_penalty(self):
        r = sc.compute_score(fin(promoter_pledge_pct=30.0), llm())
        self.assertEqual(r["breakdown"]["governance_penalty"], -15.0)

    def test_confirmed_event_gets_full_penalty_and_gate_for_that_ticker_only(self):
        with mock.patch.object(config, "SCORE_PARAMS", {"confirmed_events": {"AURUM": ["auditor_resignation"]}}):
            hit = sc.compute_score(fin(ticker="AURUM", **STRONG, ann_auditor_resignation=1), llm(score=95))
            other = sc.compute_score(fin(ticker="OTHER", **STRONG, ann_auditor_resignation=1), llm(score=95))
        self.assertEqual(hit["breakdown"]["governance_penalty"], -20.0)
        self.assertLessEqual(hit["score"], 54)
        self.assertEqual(other["breakdown"]["governance_penalty"], -10.0)

    def test_quote_support_check_can_be_switched_off(self):
        with mock.patch.object(config, "SCORE_PARAMS", {"quote_support_check": False}):
            off = sc.compute_score(fin(), llm(raised="cfo_exit", quote="Shri X (CFO) (w.e.f 6 May 2024)"))
        self.assertEqual(off["breakdown"]["governance_penalty"], -5.0)


class CapTests(unittest.TestCase):
    def test_unverified_cap_is_off_by_default_until_its_value_is_recovered(self):
        self.assertIsNone(config.UNVERIFIED_GOVERNANCE_CAP)
        r = sc.compute_score(fin(governance_sources=[], **STRONG), llm(score=100, coverage=None))
        self.assertGreater(r["score"], 69)
        self.assertNotIn("Governance unchecked", notes(r))

    def test_cap_applies_when_governance_never_checked(self):
        with mock.patch.object(config, "UNVERIFIED_GOVERNANCE_CAP", 69):
            r = sc.compute_score(fin(governance_sources=[], **STRONG), llm(score=100, coverage=None))
        self.assertLessEqual(r["score"], 69)
        self.assertIn("Governance unchecked", notes(r))

    def test_cap_lifts_when_filing_governance_sections_were_read(self):
        with mock.patch.object(config, "UNVERIFIED_GOVERNANCE_CAP", 69):
            r = sc.compute_score(fin(governance_sources=[], **STRONG),
                                 llm(score=100, coverage=("auditor_report", "related_party")))
        self.assertGreater(r["score"], 69)

    def test_announcements_feed_alone_does_not_lift_the_cap(self):
        with mock.patch.object(config, "UNVERIFIED_GOVERNANCE_CAP", 69):
            r = sc.compute_score(fin(governance_sources=["dalal.announcements"], **STRONG),
                                 llm(score=100, coverage=None))
        self.assertLessEqual(r["score"], 69)

    def test_no_filing_cap_of_54(self):
        out = {**llm(score=100), "documents_used": [], "pages_included": 0}
        r = sc.compute_score(fin(**STRONG), out)
        self.assertLessEqual(r["score"], 54)
        self.assertIn("No filing text analysed", notes(r))


class PipelineScoreStageTests(unittest.TestCase):
    def test_maps_the_scorer_result_onto_the_breakdown(self):
        b = pipeline.score_stage_v21("T", llm(), fin(**STRONG), {}, {}, 30)
        direct = sc.compute_score(fin(**STRONG), llm())
        self.assertEqual(b.total, direct["score"])
        self.assertIsNone(b.long_term)       # no 55/45 split in v2.1
        self.assertIsNone(b.short_term)
        self.assertEqual(b.horizon_days, 30)
        for k in ("llm_component", "fin_level", "fin_direction", "fin_valuation", "governance_penalty"):
            self.assertIn(k, b.components)
        self.assertEqual(b.components["fin_level"]["max"], 20.0)

    def test_labels_flag_the_open_items(self):
        b = pipeline.score_stage_v21("T", None, {"stub": True}, {}, {}, 30)
        text = " | ".join(b.labels)
        self.assertIn("No LLM analysis", text)
        self.assertIn("STUB data", text)
        self.assertIn("Unverified-governance cap is OFF", text)

    def test_cap_text_is_copied_into_caps_applied(self):
        out = {**llm(score=100), "documents_used": [], "pages_included": 0}
        b = pipeline.score_stage_v21("T", out, fin(**STRONG), {}, {}, 30)
        self.assertTrue(any(c.startswith("Capped at 54") for c in b.caps_applied))

    def test_all_prior_inputs_score_35(self):
        run = pipeline.run_pipeline(["FAKE"], 30)
        r = run.results[0]
        self.assertAlmostEqual(r.breakdown.total, 35.0, places=1)
        self.assertEqual(run.to_rows()[0]["Rating"], "Avoid")

    def test_breakdown_can_be_saved_by_the_real_store(self):
        import tempfile
        from pathlib import Path
        from store import Store
        with tempfile.TemporaryDirectory() as tmp:
            st = Store(Path(tmp))
            stages, _ = pipeline.make_stub_stages(store=st)
            run = pipeline.run_pipeline(["FAKE"], 30, stages)
            self.assertEqual(run.results[0].status, "ok")
            self.assertEqual(st.load("FAKE")["breakdown"]["total"], run.results[0].breakdown.total)


if __name__ == "__main__":
    unittest.main()
