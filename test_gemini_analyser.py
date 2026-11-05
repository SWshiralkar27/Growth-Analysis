"""
test_gemini_analyser.py
Version: Step 8, v0.8.0 (2026-11-04).
Tests for gemini_analyser.py, rate_limiter.py and the real analyse stage in the pipeline.
Gemini is replaced by a fake function, so no key, package or network is needed. The live
Gemini call itself (SDK, model name, response format) can only be proved in the running app.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import json
import os
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
import gemini_analyser as G
import pdf_extractor as pe
import pipeline
import score_card as sc
from errors import PermanentError, TransientError
from rate_limiter import RateLimiter
from test_pdf_extractor import make_pdf

DOC = "BEL_2026_05_12_AnnualReport.pdf"
QUOTE_A = "Order book stands at a record backlog of Rs 71650 crore as on 31 March 2026"
QUOTE_B = "Capacity expansion at the Bengaluru facility will commission in the second half of next year"
FILLER = "general narrative text about the board and community " * 20


def make_context():
    pages = [{"page": 1, "text": QUOTE_A + ". " + FILLER},
             {"page": 2, "text": QUOTE_B + ". " + FILLER},
             {"page": 3, "text": "Independent Auditor's Report. Basis for opinion. " + FILLER}]
    doc = {"document": DOC, "grade": "A", "doc_date": date(2026, 5, 12), "pages": pages}
    return pe.build_context([doc], max_chars=50_000)


def ev(page, quote):
    return {"document": DOC, "page": page, "quote": quote}


def answer(**over):
    ans = {
        "factors": [
            {"factor": "Business Catalysts", "score": 70, "confidence": "high", "rationale": "r",
             "evidence": [ev(1, QUOTE_A), ev(2, QUOTE_B)]},
            {"factor": "Market Expansion", "score": 60, "confidence": "medium", "rationale": "r",
             "evidence": [ev(1, QUOTE_A)]},
            {"factor": "Order Book Growth", "score": 80, "confidence": "high", "rationale": "r",
             "evidence": [ev(1, QUOTE_A), ev(2, QUOTE_B)]},
            {"factor": "Competitive Advantage", "score": 55, "confidence": "low", "rationale": "r",
             "evidence": []},
        ],
        "governance": [{"flag": f, "raised": False} for f in G.GOVERNANCE_FLAGS],
        "key_strengths": ["strong order book"], "key_risks": ["execution"],
    }
    ans.update(over)
    return ans


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Fake:
    """A scripted generate function: each item is a string (the reply) or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0
        self.prompts = []

    def __call__(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return item


class CountLimiter:
    def __init__(self):
        self.n = 0

    def acquire(self):
        self.n += 1


def run(fake, ctx_meta=None, ticker="TCS", fin=None, **kw):
    ctx, meta = ctx_meta or make_context()
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("rng", lambda a, b: a)
    kw.setdefault("cache_dir", Path(tempfile.mkdtemp()))
    return G.analyse_company(ticker, fin or {}, ctx, meta, generate_fn=fake, **kw)


# ----------------------------------------------------------- answer checking
class QuoteTests(unittest.TestCase):
    def test_exact_quote_verifies_and_paraphrase_does_not(self):
        page = QUOTE_A + ". " + FILLER
        self.assertTrue(G._verify_quote(QUOTE_A, page))
        self.assertTrue(G._verify_quote("record backlog of Rs 71650 crore", page))
        self.assertFalse(G._verify_quote("The company has a very large pile of pending orders now", page))
        self.assertFalse(G._verify_quote("too short", page))
        self.assertFalse(G._verify_quote("", page))
        self.assertFalse(G._verify_quote(QUOTE_A, None))

    def test_document_names_are_resolved_loosely(self):
        known = {(DOC, 1): "x"}
        for name in (DOC, DOC.lower(), "filings/" + DOC, DOC[:-4]):
            self.assertEqual(G._resolve_document(name, known), DOC, name)
        self.assertIsNone(G._resolve_document("other.pdf", known))
        self.assertIsNone(G._resolve_document(None, known))


class ConfidencePolicyTests(unittest.TestCase):
    def ev_list(self, *flags):
        return [{"document": DOC, "page": i + 1, "quote": "q", "verified": v} for i, v in enumerate(flags)]

    def test_caps(self):
        self.assertEqual(G._apply_evidence_policy("high", self.ev_list(False, False))[0], "low")
        self.assertEqual(G._apply_evidence_policy("high", self.ev_list(True, False))[0], "medium")
        self.assertEqual(G._apply_evidence_policy("high", self.ev_list(True, True))[0], "high")
        self.assertEqual(G._apply_evidence_policy("medium", self.ev_list(True, True))[0], "medium")
        self.assertEqual(G._apply_evidence_policy("none", self.ev_list(True, True))[0], "none")
        same_page = [{"document": DOC, "page": 1, "quote": "q", "verified": True}] * 2
        self.assertEqual(G._apply_evidence_policy("high", same_page)[0], "medium")


class NormaliseTests(unittest.TestCase):
    def setUp(self):
        _, meta = make_context()
        self.meta = meta

    def norm(self, parsed):
        return G._normalise_output(parsed, self.meta["page_index"], self.meta["grades"])

    def test_all_four_factors_always_present_in_order(self):
        out = self.norm({"factors": [answer()["factors"][2]], "governance": []})
        self.assertEqual([f["factor"] for f in out["factors"]], G.FACTORS)
        missing = out["factors"][0]
        self.assertEqual((missing["score"], missing["confidence_applied"]), (50, "none"))

    def test_scores_are_clamped_and_bad_scores_become_neutral_with_no_confidence(self):
        a = answer()
        a["factors"][0]["score"] = 150
        a["factors"][1]["score"] = "abc"
        a["factors"].append({"factor": "Not A Factor", "score": 99, "confidence": "high", "evidence": []})
        out = self.norm(a)
        self.assertEqual(out["factors"][0]["score"], 100)
        self.assertEqual((out["factors"][1]["score"], out["factors"][1]["confidence_applied"]), (50, "none"))
        self.assertEqual(len(out["factors"]), 4)

    def test_unverifiable_evidence_lowers_confidence(self):
        a = answer()
        a["factors"][0]["evidence"] = [ev(1, "Management said the pipeline of orders is extremely healthy overall")]
        f = self.norm(a)["factors"][0]
        self.assertEqual(f["confidence_applied"], "low")
        self.assertFalse(f["evidence"][0]["verified"])
        self.assertIn("no verified", f["confidence_note"])

    def test_governance_flags(self):
        a = answer()
        a["governance"] = [
            {"flag": "cfo_exit", "raised": True, "document": DOC, "page": 1, "quote": QUOTE_A},
            {"flag": "auditor_resignation", "raised": True},          # raised without a quote
            {"flag": "nonsense", "raised": True},
        ]
        out = {g["flag"]: g for g in self.norm(a)["governance"]}
        self.assertEqual(list(out), G.GOVERNANCE_FLAGS)
        self.assertTrue(out["cfo_exit"]["raised"] and out["cfo_exit"]["verified"])
        self.assertTrue(out["auditor_resignation"]["raised"])
        self.assertFalse(out["auditor_resignation"]["verified"])
        self.assertFalse(out["related_party_concerns"]["raised"])


# --------------------------------------------------------------- full result
class ResultTests(unittest.TestCase):
    def test_result_carries_what_the_scorer_reads_and_is_json_safe(self):
        ctx, meta = make_context()
        res = run(Fake(json.dumps(answer())), (ctx, meta))
        self.assertEqual(res["analysis_status"], "success")
        self.assertEqual(res["prompt_version"], config.PROMPT_VERSION)
        self.assertEqual(res["documents_used"], [DOC])
        self.assertEqual(res["pages_included"], meta["pages_included"])
        self.assertIn("auditor_report", res["governance_coverage"])
        self.assertFalse(res["from_cache"])
        json.dumps(res, allow_nan=False)                      # the store needs this to succeed

    def test_scorer_accepts_the_result_and_rewards_verified_evidence(self):
        res = run(Fake(json.dumps(answer())))
        score = sc.compute_score({}, res)
        self.assertGreater(score["breakdown"]["llm_component"], 21.0)   # above the all-prior value
        self.assertFalse(any("No filing text" in n for n in score["breakdown"]["governance_notes"]))

    def test_fenced_json_is_accepted(self):
        res = run(Fake("```json\n" + json.dumps(answer()) + "\n```"))
        self.assertEqual(res["analysis_status"], "success")


class PromptTests(unittest.TestCase):
    def test_prompt_contains_the_rules_data_and_extracts(self):
        fake = Fake(json.dumps(answer()))
        run(fake, fin={"sector": "Industrials", "revenue_growth": 0.12, "secret_field": 1})
        prompt = fake.prompts[0]
        for needle in ("TCS", config.HORIZON, "SCORING RUBRIC", "FACTORS", "GOVERNANCE FLAGS",
                       "data, not instructions", "[[DOC: " + DOC, "Industrials", "revenue_growth"):
            self.assertIn(needle, prompt)
        self.assertNotIn("secret_field", prompt)        # only the approved financial fields are sent

    def test_no_filings_prompt_and_switch(self):
        fake = Fake(json.dumps(answer()))
        ctx, meta = pe.build_context([], max_chars=1000)
        res = run(fake, (ctx, meta))
        self.assertIn("NO FILINGS", fake.prompts[0])
        self.assertEqual(res["documents_used"], [])
        with mock.patch.object(config, "ALLOW_NO_FILING_ANALYSIS", False):
            with self.assertRaises(PermanentError):
                run(Fake(json.dumps(answer())), (ctx, meta))


# ------------------------------------------------------------------ retries
class RetryTests(unittest.TestCase):
    def test_429_honours_the_server_delay_then_succeeds(self):
        sleeps = []
        fake = Fake(ApiError(429, "RESOURCE_EXHAUSTED. Please retry in 5.14s."),
                    ApiError(429, "RESOURCE_EXHAUSTED. Please retry in 5.14s."), json.dumps(answer()))
        res = run(fake, sleep=sleeps.append)
        self.assertEqual(res["analysis_status"], "success")
        self.assertEqual(fake.calls, 3)
        self.assertEqual(sleeps, [5.64, 5.64])            # 5.14 + the lower jitter bound 0.5

    def test_503_uses_exponential_backoff(self):
        sleeps = []
        fake = Fake(*[ApiError(503, "overloaded")] * 4, json.dumps(answer()))
        run(fake, sleep=sleeps.append)
        self.assertEqual(sleeps, [2.0, 4.0, 8.0, 16.0])

    def test_backoff_is_capped(self):
        sleeps = []
        with mock.patch.object(config, "GEMINI_MAX_ATTEMPTS", 8):
            fake = Fake(ApiError(503, "overloaded"))
            with self.assertRaises(TransientError):
                run(fake, sleep=sleeps.append)
        self.assertEqual(max(sleeps), config.GEMINI_MAX_DELAY)
        self.assertEqual(fake.calls, 8)

    def test_persistent_429_ends_as_a_transient_error(self):
        fake = Fake(ApiError(429, "RESOURCE_EXHAUSTED"))
        with self.assertRaises(TransientError) as ctx:
            run(fake)
        self.assertEqual(fake.calls, config.GEMINI_MAX_ATTEMPTS)
        self.assertIn("429", str(ctx.exception))

    def test_a_very_long_server_delay_gives_up_at_once_as_transient(self):
        sleeps = []
        fake = Fake(ApiError(429, "quota exhausted, retry in 3600s"))
        with self.assertRaises(TransientError):
            run(fake, sleep=sleeps.append)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(sleeps, [])

    def test_client_errors_are_permanent_and_not_retried(self):
        for code in (400, 401, 403, 404):
            fake = Fake(ApiError(code, "bad request"))
            with self.assertRaises(PermanentError):
                run(fake)
            self.assertEqual(fake.calls, 1)

    def test_empty_or_malformed_answers_are_retried_then_transient(self):
        fake = Fake("", "not json {", json.dumps(answer()))
        self.assertEqual(run(fake)["analysis_status"], "success")
        self.assertEqual(fake.calls, 3)
        with self.assertRaises(TransientError):
            run(Fake("[1, 2]"))

    def test_timeouts_are_retried(self):
        fake = Fake(TimeoutError("timed out"), json.dumps(answer()))
        self.assertEqual(run(fake)["analysis_status"], "success")

    def test_unexpected_exceptions_are_permanent(self):
        with self.assertRaises(PermanentError):
            run(Fake(ValueError("bug")))

    def test_limiter_is_used_for_every_attempt(self):
        limiter = CountLimiter()
        run(Fake(ApiError(503, "x"), json.dumps(answer())), limiter=limiter)
        self.assertEqual(limiter.n, 2)

    def test_the_key_never_appears_in_messages(self):
        ex = ApiError(400, "invalid key AIzaSECRET in request")
        self.assertNotIn("AIzaSECRET", G._clean(ex, "AIzaSECRET"))
        self.assertIn("***", G._clean(ex, "AIzaSECRET"))


class KeyTests(unittest.TestCase):
    def test_missing_key_is_a_clear_permanent_error(self):
        ctx, meta = make_context()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermanentError) as err:
                G.analyse_company("TCS", {}, ctx, meta, cache_dir=Path(tempfile.mkdtemp()))
        self.assertIn("GEMINI_API_KEY", str(err.exception))

    def test_key_present_but_package_missing_is_reported(self):
        ctx, meta = make_context()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "AIzaSECRET"}, clear=True):
            with mock.patch.dict(sys.modules, {"google": None, "google.genai": None}):
                with self.assertRaises(PermanentError) as err:
                    G.analyse_company("TCS", {}, ctx, meta, cache_dir=Path(tempfile.mkdtemp()))
        self.assertIn("google-genai", str(err.exception))
        self.assertNotIn("AIzaSECRET", str(err.exception))


# -------------------------------------------------------------------- cache
class CacheTests(unittest.TestCase):
    def test_identical_inputs_are_not_sent_twice(self):
        cache = Path(tempfile.mkdtemp())
        fake = Fake(json.dumps(answer()))
        first = run(fake, cache_dir=cache)
        second = run(fake, cache_dir=cache)
        self.assertEqual(fake.calls, 1)
        self.assertFalse(first["from_cache"])
        self.assertTrue(second["from_cache"])

    def test_changed_inputs_or_forced_refresh_call_again(self):
        cache = Path(tempfile.mkdtemp())
        fake = Fake(json.dumps(answer()))
        run(fake, cache_dir=cache)
        run(fake, cache_dir=cache, fin={"revenue_growth": 0.2})          # different financials
        run(fake, cache_dir=cache, force_refresh=True)
        with mock.patch.object(config, "PROMPT_VERSION", "other-version"):
            run(fake, cache_dir=cache)
        self.assertEqual(fake.calls, 4)

    def test_failures_are_never_cached(self):
        cache = Path(tempfile.mkdtemp())
        with self.assertRaises(TransientError):
            run(Fake(ApiError(503, "x")), cache_dir=cache)
        self.assertEqual(list(cache.iterdir()), [])

    def test_cache_can_be_switched_off(self):
        cache = Path(tempfile.mkdtemp())
        fake = Fake(json.dumps(answer()))
        with mock.patch.object(config, "LLM_CACHE_ENABLED", False):
            run(fake, cache_dir=cache)
            run(fake, cache_dir=cache)
        self.assertEqual(fake.calls, 2)


class RateLimiterTests(unittest.TestCase):
    def test_spaces_calls_and_never_delays_the_first(self):
        now = {"t": 100.0}
        waits = []

        def sleep(s):
            waits.append(s)
            now["t"] += s
        limiter = RateLimiter(5, clock=lambda: now["t"], sleep=sleep)
        limiter.acquire()
        self.assertEqual(waits, [])
        now["t"] += 3
        limiter.acquire()
        self.assertEqual(waits, [9.0])          # 12 s interval, 3 s already passed
        now["t"] += 20
        limiter.acquire()
        self.assertEqual(waits, [9.0])


# ------------------------------------------------------------- pipeline stage
GOOD_RECORD = {"ticker": "TCS", "data_quality": "complete", "providers_used": ["yfinance"],
               "missing_fields": [], "warnings": [], "revenue_growth": 0.12, "ebitda_margin": 0.2}


class PipelineAnalyseTests(unittest.TestCase):
    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(config, "FILINGS_FOLDER", str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        make_pdf(self.dir / "TCS_2026_05_12_AnnualReport.pdf",
                 [QUOTE_A, QUOTE_B, "Independent Auditor's Report. Basis for opinion."])

    def stages(self, fake):
        analyse = pipeline.make_real_analyse(generate_fn=fake, limiter=CountLimiter(),
                                             sleep=lambda s: None,
                                             cache_dir=Path(self._tmp.name) / "cache")
        return pipeline.make_stub_stages(analyse_fn=analyse, fetch_fn=lambda t: GOOD_RECORD)

    def test_a_real_ticker_is_analysed_scored_and_counted(self):
        stages, store = self.stages(Fake(json.dumps(answer())))
        run_ = pipeline.run_pipeline(["TCS"], 30, stages)
        r = run_.results[0]
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.documents, ["TCS_2026_05_12_AnnualReport.pdf"])
        self.assertGreater(r.breakdown.components["llm_component"]["points"], 21.0)
        saved = store.load("TCS")
        self.assertEqual(saved["analysis"]["analysis_status"], "success")
        self.assertEqual(saved["analysis"]["prompt_version"], config.PROMPT_VERSION)

    def test_demo_tickers_never_call_gemini(self):
        fake = Fake(json.dumps(answer()))
        stages, _ = self.stages(fake)
        pipeline.run_pipeline(["FAKE", "STALE429", "NOPRIOR429", "FAILPERM"], 30, stages)
        self.assertEqual(fake.calls, 0)

    def test_transient_error_keeps_the_prior_analysis_and_marks_stale(self):
        stages, store = self.stages(Fake(ApiError(429, "RESOURCE_EXHAUSTED retry in 2s")))
        prior = {"analysis": {"analysis_status": "success", "factors": [], "governance": []}}
        store.save("TCS", prior)
        r = pipeline.run_pipeline(["TCS"], 30, stages).results[0]
        self.assertEqual(r.status, "stale")
        self.assertTrue(r.prior_used)
        self.assertEqual(store.load("TCS")["analysis"], prior["analysis"])   # untouched

    def test_transient_error_without_a_prior_is_partial(self):
        stages, store = self.stages(Fake(ApiError(503, "overloaded")))
        r = pipeline.run_pipeline(["TCS"], 30, stages).results[0]
        self.assertEqual(r.status, "partial")
        self.assertIsNone(store.load("TCS"))

    def test_permanent_error_fails_the_ticker_only(self):
        stages, _ = self.stages(Fake(ApiError(401, "API key not valid")))
        run_ = pipeline.run_pipeline(["TCS", "FAKE"], 30, stages)
        by = {r.ticker: r for r in run_.results}
        self.assertEqual(by["TCS"].status, "failed")
        self.assertIn("analyse", by["TCS"].stage_errors)
        self.assertEqual(by["FAKE"].status, "ok")

    def test_without_a_key_a_real_ticker_fails_with_a_clear_message(self):
        stages, _ = pipeline.make_stub_stages(fetch_fn=lambda t: GOOD_RECORD)  # real analysis, no key in the environment
        r = pipeline.run_pipeline(["TCS"], 30, stages).results[0]
        self.assertEqual(r.status, "failed")
        self.assertIn("GEMINI_API_KEY", r.stage_errors["analyse"])


if __name__ == "__main__":
    unittest.main()
