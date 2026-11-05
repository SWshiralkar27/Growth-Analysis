# =====================================================
# PORTED INTO THE STEP 9 LAYOUT: Step 9, v0.9.0 (2026-11-05). Code version 2026-11-step9.
# The report format and code below are the recovered report_generator.py, UNCHANGED.
# The format must not change without the owner's approval; test_report_generator.py compares
# the output with a golden copy produced by the recovered original.
# =====================================================
# =====================================================
# report_generator.py
# =====================================================

import os
import html as _html
from datetime import datetime
import config
from score_card import rating_for_score


def _meta_strip(meta: dict) -> str:
    if not meta:
        return ""
    parts = []
    if meta.get("analysis_date"):         parts.append(f"Analysis: {meta['analysis_date'][:10]}")
    if meta.get("cache_hit"):             parts.append("🟢 From cache")
    if meta.get("financial_as_of_date"):  parts.append(f"Financials as-of: {meta['financial_as_of_date']}")
    if meta.get("missing_pdf"):           parts.append("⚠️ Source PDF missing; quotes from text cache")
    if meta.get("scanned"):               parts.append("⚠️ Scanned PDF — financials-only; governance cap applied")
    if meta.get("delta"):                 parts.append("🔁 Delta analysis (new filing merged)")
    if not parts:
        return ""
    return "<p class='muted'>" + " · ".join(parts) + "</p>"


def _esc(v): return _html.escape(str(v)) if v is not None else "&mdash;"

# Rating bands live in score_card.rating_for_score only; the report derives its CSS bucket from it,
# so re-banding after a shadow run cannot leave reports and rankings disagreeing.
_RATING_BUCKET = {"Strong Candidate": "strong", "Good Candidate": "good", "Mixed Signals": "mixed",
                  "Speculative": "spec", "Avoid": "avoid"}
def _rating_for(s): return rating_for_score(s)
def _score_bucket(s): return _RATING_BUCKET.get(rating_for_score(s), "avoid")


def _governance_banner(bd: dict) -> str:
    """Red banner when a cap/gate limits the score; amber banner for items that need a human check."""
    notes = [str(n) for n in bd.get("governance_notes", [])]
    caps = [n for n in notes if n.startswith("Capped at")]
    review = [n for n in notes if "exchange feed only" in n or "penalty waived" in n]
    html_out = ""
    if caps:
        items = "".join(f"<li>{_esc(c)}</li>" for c in caps)
        html_out += ("<div style='background:#fee2e2;padding:1rem;border-left:4px solid #dc2626;margin-bottom:1rem;'>"
                     "<strong>Score limited by a governance gate or cap, even if the raw score is higher</strong>"
                     f"<ul style='margin:0.5rem 0 0 1rem;'>{items}</ul></div>")
    if review:
        items = "".join(f"<li>{_esc(c)}</li>" for c in review)
        html_out += ("<div style='background:#fef3c7;padding:1rem;border-left:4px solid #f59e0b;margin-bottom:1rem;'>"
                     "<strong>Governance items to verify manually (exchange-feed-only hits carry a reduced penalty; "
                     "waived flags carry none)</strong>"
                     f"<ul style='margin:0.5rem 0 0 1rem;'>{items}</ul></div>")
    return html_out


def _render_exchange(financials: dict, llm: dict) -> str:
    """Exchange-feed governance signals (Grade A) and which filing sections the model actually read."""
    rows = ""
    for key, label in (("ann_auditor_resignation", "Auditor resignation announcements"),
                       ("ann_cfo_exit", "CFO exit announcements"),
                       ("ann_pledge_invocation", "Pledge invocation announcements")):
        val = financials.get(key)
        flag = " 🔴" if isinstance(val, (int, float)) and val > 0 else ""
        rows += f"<tr><td>{label}</td><td>{_esc(val)}{flag}</td></tr>"
    events = financials.get("ann_events") or []
    if events:
        head_html = "".join(
            f"<li>{_esc(e.get('date') or 'undated')} &middot; {_esc(e.get('event'))} &middot; {_esc(e.get('text'))}</li>"
            for e in events[:8] if isinstance(e, dict))
    else:
        heads = financials.get("ann_headlines") or []
        head_html = "".join(f"<li>{_esc(h)}</li>" for h in heads[:5])
    cov = llm.get("governance_coverage")
    cov_txt = (", ".join(cov) if cov else "none") if isinstance(cov, list) else "not recorded (analysis predates context v2)"
    return (f"<table><tr><th>Exchange signal</th><th>Count</th></tr>{rows}</table>"
            f"<p><strong>Filing governance sections read by the model:</strong> {_esc(cov_txt)}</p>"
            + (f"<p><strong>Recent matching headlines (verify manually):</strong></p><ul>{head_html}</ul>" if head_html else ""))

def _render_factors(llm: dict) -> str:
    rows = ""
    for f in llm.get("factors", []):
        conf = f.get("confidence_applied", "none")
        badge = f"<span class='badge {conf}'>{conf.upper()}</span>"
        ev_html = ""
        for e in f.get("evidence", []):
            icon = "✅" if e.get("verified") else "❌"
            ev_html += f"<div class='ev'>{icon} <strong>{_esc(e.get('document'))}</strong> p.{e.get('page')}<br><em>\"{_esc(e.get('quote'))}\"</em></div>"
        
        rows += f"""
        <tr>
            <td><strong>{_esc(f['factor'])}</strong></td>
            <td style="font-size:1.2rem; font-weight:bold;">{f['score']}</td>
            <td>{badge}</td>
            <td>{ev_html or '<span class="muted">No evidence provided</span>'}</td>
        </tr>"""
    return f"<table class='factors'><thead><tr><th>Factor</th><th>Score</th><th>Confidence</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table>"

def _render_governance(llm: dict) -> str:
    rows = ""
    for g in llm.get("governance", []):
        status = "🔴 RAISED" if g.get("raised") else "🟢 Clear"
        verified = "Yes" if g.get("verified") else "No"
        ev = g.get("evidence", [])
        ev_text = f"p.{ev[0]['page']}: \"{_esc(ev[0]['quote'])}\"" if ev else ""
        rows += f"<tr><td>{_esc(g['flag'])}</td><td>{status}</td><td>{verified}</td><td><small>{ev_text}</small></td></tr>"
    return f"<table class='gov'><thead><tr><th>Flag</th><th>Status</th><th>Verified</th><th>Evidence</th></tr></thead><tbody>{rows}</tbody></table>"

def create_html_report(ticker, financials, llm_output, score_result,
                       has_filing=True, meta=None):
    score = score_result["score"]
    bd = score_result.get("breakdown", {})
    bucket = _score_bucket(score)
    
    warning = "" if has_filing else "<div style='background:#fef3c7;padding:1rem;border-left:4px solid #f59e0b; margin-bottom:1rem;'><strong>⚠️ No PDF filing found. Qualitative scores are based on model knowledge.</strong></div>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{ticker} Analysis</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1000px; margin: 2rem auto; color: #222; }}
.badge {{ padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.8rem; font-weight: bold; }}
.badge.high {{ background: #d1fae5; color: #065f46; }} 
.badge.medium {{ background: #fef3c7; color: #92400e; }} 
.badge.low {{ background: #fee2e2; color: #991b1b; }}
.badge.none {{ background: #f1f5f9; color: #64748b; }}
.badge.strong {{ background: #d1fae5; color: #065f46; }} .badge.good {{ background: #dbeafe; color: #1e40af; }}
.badge.mixed {{ background: #fef3c7; color: #92400e; }} .badge.spec {{ background: #ffedd5; color: #9a3412; }}
.badge.avoid {{ background: #fee2e2; color: #991b1b; }}
table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; }}
th, td {{ border: 1px solid #e2e8f0; padding: 0.75rem; text-align: left; vertical-align: top; }}
.ev {{ background: #f8fafc; padding: 0.5rem; margin-bottom: 0.5rem; border-radius: 4px; font-size: 0.9rem; }}
.muted {{ color: #94a3b8; font-style: italic; }}
.flex {{ display: flex; gap: 2rem; }}
</style></head><body>

<h1>{ticker} Equity Analysis</h1>
<p class="muted">Generated {datetime.now():%Y-%m-%d %H:%M} | Horizon: {config.HORIZON}</p>
{_meta_strip(meta or {})}
{warning}
{_governance_banner(bd)}

<div class="flex">
    <div>
        <h2>Score: <span class="badge {bucket}" style="font-size:1.5rem;">{score:.1f}/100</span></h2>
        <p><strong>{_rating_for(score)}</strong></p>
    </div>
</div>

<h2>Score Breakdown</h2>
<ul>
    <li><strong>LLM Factor Score:</strong> {bd.get('llm_component')} / 60 (Coverage: {(bd.get('llm_coverage', 0) * 100):.0f}%)</li>
    <li><strong>Financial Bonus:</strong> {bd.get('financial_bonus')} / 40
        <ul style="margin-top: 0.5rem; font-size: 0.9rem; color: #475569;">
            <li>Level (Size/Profitability): {bd.get('fin_level', 0)} / 20</li>
            <li>Direction (Growth/Acceleration): {bd.get('fin_direction', 0)} / 14</li>
            <li>Valuation & Ownership: {bd.get('fin_valuation', 0)} / 6</li>
        </ul>
    </li>
    <li><strong>Governance Penalty:</strong> {bd.get('governance_penalty')}</li>
    <li><strong>Raw Score:</strong> {bd.get('raw_score')}</li>
    <li><strong>Scoring model:</strong> {_esc(bd.get('scoring_model', 'v1'))}</li>
</ul>
<details><summary>View scoring notes</summary>
    <ul>
        {''.join(f'<li>{_esc(n)}</li>' for n in bd.get('factor_notes', []))}
        {''.join(f'<li>{_esc(n)}</li>' for n in bd.get('financial_notes', []))}
        {''.join(f'<li>{_esc(n)}</li>' for n in bd.get('governance_notes', []))}
    </ul>
</details>

<h2>Qualitative Factors</h2>
{_render_factors(llm_output)}

<h2>Governance Flags</h2>
{_render_governance(llm_output)}
<h3>Exchange signals &amp; coverage</h3>
{_render_exchange(financials, llm_output)}

<h2>Financials & Smart Money</h2>
<table>
    <tr><th>Metric</th><th>Value</th></tr>
    <tr><td>Market Cap</td><td>{financials.get('market_cap')}</td></tr>
    <tr><td>Revenue Growth (YoY, per data source)</td><td>{financials.get('revenue_growth')}</td></tr>
    <tr><td>EBITDA Margin</td><td>{financials.get('ebitda_margin')}</td></tr>
    <tr><td>D/E Ratio</td><td>{financials.get('debt_to_equity')}</td></tr>
    <tr><td>Interest Coverage</td><td>{financials.get('interest_coverage')}</td></tr>
    <tr><td>OCF / EBITDA</td><td>{financials.get('ocf_to_ebitda')}</td></tr>
    <tr><td>Promoter Holding</td><td>{financials.get('promoter_holding')}%</td></tr>
    <tr><td>Promoter Pledge</td><td>{financials.get('promoter_pledge_pct')}%</td></tr>
    <tr><td>EPS Revisions (90d)</td><td>{financials.get('eps_revision_90d_pct')}</td></tr>
</table>

<h2>Key Strengths</h2>
<ul>{''.join(f'<li>{_esc(s)}</li>' for s in llm_output.get('key_strengths', []))}</ul>

<h2>Key Risks</h2>
<ul>{''.join(f'<li>{_esc(r)}</li>' for r in llm_output.get('key_risks', []))}</ul>

</body></html>"""

def create_portfolio_report(results: list) -> str:
    sorted_r = sorted(results, key=lambda x: x.get("Score", 0), reverse=True)
    rows = ""
    for i, s in enumerate(sorted_r, 1):
        bucket = _score_bucket(s['Score'])
        warn = "" if s.get("Has_Filing") else " ⚠️"
        rows += f"<tr><td>#{i}</td><td><strong>{s['Ticker']}{warn}</strong></td><td><span class='badge {bucket}'>{s['Rating']}</span></td><td>{s['Score']:.1f}</td><td><a href='{os.path.basename(s['Report'])}'>View Report</a></td></tr>"
    return f"""<html><head><style>
    body {{ font-family: sans-serif; max-width: 800px; margin: 2rem auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #ddd; padding: 0.8rem; text-align: left; }}
    .badge {{ padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.8rem; }}
    .badge.strong {{ background: #d1fae5; color: #065f46; }} .badge.good {{ background: #dbeafe; color: #1e40af; }}
    .badge.mixed {{ background: #fef3c7; color: #92400e; }} .badge.spec {{ background: #ffedd5; color: #9a3412; }}
    .badge.avoid {{ background: #fee2e2; color: #991b1b; }}
    </style></head><body>
    <h1>Portfolio Dashboard</h1><p>Generated {datetime.now():%Y-%m-%d}</p>
    <table><thead><tr><th>Rank</th><th>Ticker</th><th>Rating</th><th>Score</th><th>Action</th></tr></thead>
    <tbody>{rows}</tbody></table></body></html>"""
