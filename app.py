"""
app.py
Version: Step 11b, v0.11.1 (2026-11-07). Code version 2026-11-step11b.
Spec: README v3.1 DRAFT, sections 5.3, 8 and 13 (Streamlit replaces the desktop
GUI; this is a spec change awaiting recording in v3.2).

Changes in v0.10.0:
  - The stale notice "Component weights are not set..." is no longer shown (the scorer is
    real now). config.validate_config() still returns it.
  - New read-only "Diagnostics" tab (dalal_probe).
Changes in v0.10.2:
  - Diagnostics tab gets a second read-only button, "Run Yahoo probe" (yf_probe).
  app.py imports the two probe modules directly because they are diagnostic tools, not
  engine stages. Neither touches any score, record or report.
Changes in v0.11.1 (Step 11b):
  - New "Inputs" tab: see the four manual fields for a ticker and their freshness, then enter or
    confirm one field at a time, with an optional source document. All logic lives in
    pipeline.py; this file only shows it. Saved values are stored and shown only: the v2.1
    score does not read them yet.

Purpose: Streamlit front end. It collects tickers and a horizon, runs the
pipeline in a worker thread and shows progress from the main thread. It holds
no scoring, fetching or report logic.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import json
import queue
import threading
from datetime import date

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import config
import dalal_probe
import pipeline
import yf_probe
from errors import ConfigError, FilingError, StoreError

st.set_page_config(page_title="Equity Growth Analyst", layout="wide")

# Notices that are out of date now that the scorer is real. Hidden here only;
# config.validate_config() still returns them (test_step1.py expects that).
STALE_NOTICE_PREFIXES = ("Component weights are not set",)


@st.cache_resource(show_spinner=False)
def restore_once():
    """Bring back saved records from permanent storage once per app start."""
    return pipeline.restore_from_backup()


def render_storage_status() -> None:
    info = pipeline.storage_status()
    if not info["enabled"]:
        st.caption("Permanent storage: OFF (temporary)")
        return
    st.caption(f"Permanent storage: ON ({info['repo']})")
    restored = restore_once()
    if restored is not None and restored.restored:
        st.caption(f"Restored {len(restored.restored)} saved record(s) after restart")
    if restored is not None and restored.errors:
        st.caption(f"Restore problem: {restored.errors[0]}")
    st.caption(f"Backups this session: {info['ok']} ok, {info['failed']} failed")
    if info["last_error"]:
        st.caption(f"Last backup problem: {info['last_error']}")


def start_worker(tickers: list[str], horizon_days: int, q: "queue.Queue") -> threading.Thread:
    """Run the pipeline off the main thread. Streamlit calls stay on the main
    thread: the worker only puts messages on the queue."""

    def on_progress(ticker: str, stage: str, state: str) -> None:
        q.put(("progress", ticker, stage, state))

    def target() -> None:
        try:
            stages, _ = pipeline.make_stub_stages(store=pipeline.default_store())
            result = pipeline.run_pipeline(
                tickers, horizon_days,
                stages=stages,
                on_progress=on_progress,
                max_workers=config.DEFAULT_WORKERS,
            )
            q.put(("done", result))
        except Exception as exc:  # always end the main-thread loop
            q.put(("crash", f"{type(exc).__name__}: {exc}"))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def run_with_progress(tickers: list[str], horizon_days: int):
    q: "queue.Queue" = queue.Queue()
    start_worker(tickers, horizon_days, q)
    lines: list[str] = []
    result, crash = None, None
    with st.status("Running pipeline...", expanded=True) as box:
        log = st.empty()
        while result is None and crash is None:
            try:
                msg = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg[0] == "progress":
                _, ticker, stage, state = msg
                lines.append(f"{ticker:<12} {stage:<16} {state}")
                log.code("\n".join(lines[-12:]))
            elif msg[0] == "done":
                result = msg[1]
            else:
                crash = msg[1]
        if crash:
            box.update(label="Run crashed", state="error")
        else:
            box.update(label="Run complete", state="complete")
    return result, crash


def render_result(result: pipeline.RunResult) -> None:
    df = pd.DataFrame(result.to_rows())
    st.subheader("Results")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        "Download CSV", df.to_csv(index=False).encode("utf-8"),
        file_name="results.csv", mime="text/csv",
    )
    problems = [r for r in result.results if r.stage_errors]
    if problems:
        with st.expander(f"Stage errors ({len(problems)} ticker(s))"):
            for r in problems:
                st.markdown(f"**{r.ticker}** ({r.status})")
                for stage, msg in r.stage_errors.items():
                    st.text(f"  {stage}: {msg}")
    render_details(result)
    st.caption(
        f"Horizon {result.horizon_days} days | code {result.code_version} | "
        f"spec {result.spec_version} | prompt {result.prompt_version} | "
        f"finished {result.finished_at}"
    )


def render_details(result: pipeline.RunResult) -> None:
    """Per-ticker data notes and the HTML report, plus the downloads for the whole run."""
    scored = [r for r in result.results if pipeline.build_report(r)]
    if scored:
        st.download_button("Download all reports (zip)", pipeline.build_reports_zip(result.results),
                           file_name="reports.zip", mime="application/zip")
    st.subheader("Details")
    for r in result.results:
        with st.expander(f"{r.ticker}: {r.status}"):
            notes = pipeline.data_notes(r)
            st.markdown("**Data**")
            st.text(f"Quality: {notes['data_quality'] or 'n/a'} | providers: "
                    f"{', '.join(notes['providers']) or 'none'}")
            if notes["missing_fields"]:
                st.text("Missing core fields: " + ", ".join(notes["missing_fields"]))
            if notes["governance_sources"]:
                st.text("Governance feeds that answered: " + ", ".join(notes["governance_sources"]))
            else:
                st.text("Governance feeds that answered: none")
            for w in notes["warnings"]:
                st.caption(f"Warning: {w}")
            st.markdown("**Filings**")
            st.text("Read: " + (", ".join(notes["filings_read"]) or "none"))
            for name, reason in notes["filings_unreadable"]:
                st.caption(f"Could not read {name}: {reason}")
            for stage, msg in notes["stage_errors"].items():
                st.caption(f"{stage}: {msg}")
            html = pipeline.build_report(r)
            if html:
                if r.status != "ok":
                    st.warning("This report uses a prior or missing analysis (status "
                               f"{r.status}); the report itself does not show that.")
                components.html(html, height=700, scrolling=True)
                st.download_button(f"Download {r.ticker} report (HTML)", html.encode("utf-8"),
                                   file_name=f"{r.ticker}.html", mime="text/html", key=f"dl_{r.ticker}")
    if scored:
        st.download_button("Download dashboard (HTML)", pipeline.build_dashboard(result.results).encode("utf-8"),
                           file_name="Portfolio_Dashboard.html", mime="text/html")


def render_saved_panel() -> None:
    st.subheader("Saved analyses")
    st.caption("Reports rebuilt from saved analyses, without calling Yahoo, the exchanges or Gemini.")
    tickers = pipeline.saved_tickers()
    if not tickers:
        st.caption("Nothing saved yet. Run a ticker first.")
        return
    ticker = st.selectbox("Ticker", tickers)
    record = pipeline.load_saved(ticker)
    if not record:
        st.error("That saved record could not be read.")
        return
    st.caption(f"Saved {record.get('saved_at', 'date unknown')} | prompt {record.get('prompt_version')}")
    html = pipeline.report_from_record(ticker, record)
    if not html:
        st.info("This record has no score to report.")
        return
    components.html(html, height=700, scrolling=True)
    st.download_button(f"Download {ticker} report (HTML)", html.encode("utf-8"),
                       file_name=f"{ticker}.html", mime="text/html", key=f"saved_{ticker}")


def _clear_uploader() -> None:
    st.session_state["uploader_gen"] = st.session_state.get("uploader_gen", 0) + 1


def render_filings_panel() -> None:
    st.subheader("Filings")
    if pipeline.storage_status()["enabled"]:
        st.info("Uploaded files are copied to your private data repository.")
    else:
        st.warning(
            "Permanent storage is OFF: uploaded files disappear when the app "
            "restarts or redeploys."
        )
    raw = st.text_input("Ticker", key="filing_ticker", placeholder="for example TCS")
    tickers, rejected = pipeline.normalise_tickers(raw, 1) if raw.strip() else ([], [])
    ticker = tickers[0] if tickers else None
    if rejected and raw.strip():
        st.error(f"{rejected[0][0]}: {rejected[0][1]}")

    col_type, col_date = st.columns(2)
    type_label = col_type.selectbox("Document type", pipeline.FILING_TYPE_LABELS)
    doc_date = col_date.date_input("Document date", value=date.today(), max_value=date.today())
    upload = st.file_uploader(
        f"PDF file (up to {config.MAX_UPLOAD_MB} MB)", type=["pdf"],
        key=f"uploader_{st.session_state.get('uploader_gen', 0)}",
    )

    if st.button("Save file", type="primary"):
        if not ticker:
            st.session_state["filing_msg"] = ("error", "Enter a valid ticker first.")
        elif upload is None:
            st.session_state["filing_msg"] = ("error", "Choose a PDF file first.")
        else:
            try:
                result = pipeline.upload_filing(ticker, doc_date, type_label, upload.getvalue())
                base = (f"{result.name} was already uploaded." if result.status == "already_present"
                        else f"Saved {result.name}.")
                note = f" {result.backup_note.capitalize()}." if result.backup_note else ""
                kind = "warning" if result.backup_note.startswith("not backed up") else (
                    "info" if result.status == "already_present" else "success")
                st.session_state["filing_msg"] = (kind, base + note)
                _clear_uploader()
            except FilingError as exc:
                st.session_state["filing_msg"] = ("error", str(exc))
        st.rerun()

    msg = st.session_state.pop("filing_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])

    if ticker:
        st.markdown(f"**Stored filings for {ticker}**")
        try:
            files = pipeline.stored_filings(ticker)
        except FilingError as exc:
            files = []
            st.error(str(exc))
        if not files:
            st.caption("None yet.")
        for f in files:
            c_name, c_info, c_btn = st.columns([5, 3, 2])
            c_name.text(f.name)
            when = f.doc_date.isoformat() if f.doc_date else "date unknown"
            c_info.caption(f"{f.type_label} | {when} | {f.size_bytes / 1_048_576:.1f} MB")
            if c_btn.button("Remove", key=f"rm_{f.name}"):
                try:
                    pipeline.delete_filing(ticker, f.name)
                except FilingError as exc:
                    st.session_state["filing_msg"] = ("error", str(exc))
                st.rerun()


def _status_text(row: dict) -> str:
    if row["status"] == "missing":
        return "Not entered"
    if row["status"] == "fresh":
        return "Left blank (valid)" if row["blank"] else "Fresh"
    return "Stale, please enter again"


def _clear_inputs_form() -> None:
    st.session_state["inputs_gen"] = st.session_state.get("inputs_gen", 0) + 1


def render_inputs_panel() -> None:
    st.subheader("Manual inputs")
    st.caption(
        f"Values the free feeds cannot supply. Each saved value stays valid for "
        f"{config.MANUAL_INPUT_MAX_AGE_DAYS} days from its as-of date. They are stored and shown here "
        f"only: the v2.1 score does not use them yet."
    )
    if pipeline.storage_status()["enabled"]:
        st.info("Saved values and their documents are copied to your private data repository.")
    else:
        st.warning("Permanent storage is OFF: saved values disappear when the app restarts or redeploys.")

    raw = st.text_input("Ticker", key="inputs_ticker", placeholder="for example BAJAJ-AUTO")
    tickers, rejected = pipeline.normalise_tickers(raw, 1) if raw.strip() else ([], [])
    ticker = tickers[0] if tickers else None
    if rejected and raw.strip():
        st.error(f"{rejected[0][0]}: {rejected[0][1]}")

    msg = st.session_state.pop("inputs_msg", None)
    if msg:
        getattr(st, msg[0])(msg[1])
    if not ticker:
        return

    pipeline.ensure_local_manual_inputs()
    try:
        rows = pipeline.manual_status(ticker)
    except (StoreError, pipeline.InputRejected) as exc:
        st.error(str(exc))
        return

    st.markdown(f"**Saved inputs for {ticker}**")
    table = pd.DataFrame([{
        "Field": r["label"],
        "Value": ("(blank)" if r["blank"] else ("" if r["value"] is None else r["value"])),
        "As of": r["as_of"] or "",
        "Status": _status_text(r),
        "Source document": r["source_file"] or "",
    } for r in rows]).astype(str)
    st.dataframe(table, use_container_width=True, hide_index=True)

    st.markdown("**Enter or update one field**")
    gen = st.session_state.get("inputs_gen", 0)
    labels = {r["label"]: r for r in rows}
    choice = st.selectbox("Field", list(labels), key=f"in_field_{gen}")
    row = labels[choice]
    blank = st.checkbox("Leave blank (record that this is not available)", key=f"in_blank_{gen}")
    if row["kind"] == "date":
        value_text = st.text_input("Date (YYYY-MM-DD)", key=f"in_val_{gen}", disabled=blank,
                                   placeholder="for example 2026-12-15")
    else:
        value_text = st.text_input("Value (a number)", key=f"in_val_{gen}", disabled=blank)
    as_of = st.date_input("As of (the date this value refers to)", value=date.today(),
                          max_value=date.today(), key=f"in_asof_{gen}")
    allowed = [ext.lstrip(".") for ext in config.MANUAL_SOURCE_EXTENSIONS]
    upload = st.file_uploader(
        f"Source document (optional, up to {config.MANUAL_SOURCE_MAX_MB:g} MB)", type=allowed,
        key=f"in_src_{gen}")

    if st.button("Save input", type="primary", key=f"in_save_{gen}"):
        try:
            result = pipeline.save_manual_input(
                ticker, row["field"], None if blank else value_text, as_of, blank=blank,
                source_name=upload.name if upload is not None else None,
                source_bytes=upload.getvalue() if upload is not None else None)
            note = f" {result.backup_note.capitalize()}." if result.backup_note else ""
            kind = "warning" if result.backup_note.startswith("not backed up") else "success"
            st.session_state["inputs_msg"] = (kind, f"Saved {row['label']} for {ticker}.{note}")
            _clear_inputs_form()
        except pipeline.InputRejected as exc:
            st.session_state["inputs_msg"] = ("error", f"Not saved: {exc}")
        except StoreError as exc:
            st.session_state["inputs_msg"] = ("error", f"Not saved: {exc}")
        st.rerun()


def _diag_ticker(raw: str):
    tickers, _ = pipeline.normalise_tickers(raw, 1) if raw.strip() else ([], [])
    return tickers[0] if tickers else None


def render_dalal_result(result: dict) -> None:
    if not result.get("installed"):
        st.error(result.get("note", "dalal is not installed"))
        return
    st.text(f"dalal version: {result.get('version')} | ticker: {result.get('ticker')} | "
            f"scrip code: {result.get('scrip_code') or 'not found'}")
    with st.expander("Functions dalal really has"):
        st.code("\n".join(f"{f['name']}{f['signature']}" for f in result["functions"]) or "none")
    for call in result["calls"]:
        mark = "OK" if call.get("ok") else "FAILED"
        with st.expander(f"{mark}: {call['label']}", expanded=call["label"].startswith("shareholding(")):
            st.code(json.dumps(call.get("result") or {"error": call.get("error")},
                               indent=2, default=str, ensure_ascii=False))


def render_yf_result(result: dict) -> None:
    if not result.get("installed"):
        st.error(result.get("note", "yfinance is not installed"))
        return
    if not result.get("found"):
        st.error("Yahoo returned nothing for this ticker.")
        for line in result.get("errors", []):
            st.caption(line)
        return
    st.text(f"Yahoo symbol: {result['symbol']} | statement values shown in: {result['unit']}")
    for line in result["readings"]:
        st.text(line)
    with st.expander("Annual statements (newest column first)", expanded=True):
        st.code(json.dumps(result["annual"], indent=2, default=str))
    with st.expander("Quarterly revenue (newest column first)", expanded=True):
        st.code(json.dumps(result["quarterly"], indent=2, default=str))
    with st.expander("Yahoo info fields"):
        st.code(json.dumps(result["info"], indent=2, default=str))
    with st.expander("Row names matched"):
        st.code(json.dumps(result["row_names_used"], indent=2, default=str))


def render_diagnostics_panel() -> None:
    st.subheader("Diagnostics")
    st.caption("Read-only. These probes change no score, record or report.")
    raw = st.text_input("Ticker", key="diag_ticker", placeholder="for example BAJAJ-AUTO")
    col_a, col_b = st.columns(2)
    run_dalal = col_a.button("Run dalal probe")
    run_yf = col_b.button("Run Yahoo probe")

    if run_dalal or run_yf:
        ticker = _diag_ticker(raw)
        if not ticker:
            st.error("Enter one valid ticker first.")
        elif run_dalal:
            with st.spinner("Asking dalal (this can take 10 to 40 seconds)..."):
                st.session_state["diag_result"] = dalal_probe.probe(ticker)
        else:
            with st.spinner("Asking Yahoo (this can take 10 to 40 seconds)..."):
                st.session_state["diag_yf"] = yf_probe.probe(ticker)

    if st.session_state.get("diag_yf"):
        st.markdown("**Yahoo probe**")
        render_yf_result(st.session_state["diag_yf"])
    if st.session_state.get("diag_result"):
        st.markdown("**dalal probe**")
        render_dalal_result(st.session_state["diag_result"])


def render_run_panel(horizon: int) -> None:
    raw = st.text_area(
        f"Tickers (1 to {config.GUI_MAX_TICKERS}, separated by commas or new lines)",
        value="FAKE", height=100,
    )
    if not config.has_secret("gemini_api_key"):
        st.caption("No Gemini key is set, so any ticker other than the four demo tickers will show as failed.")
    st.caption("Real tickers fetch financial data from Yahoo and BSE, which can take 10 to 40 seconds and "
               "may be limited on shared servers. A ticker with almost no data is shown as failed, not scored.")
    with st.expander("Demo tickers for the stub stages"):
        st.markdown(
            "- `FAKE`: normal run, status ok\n"
            "- `STALE429`: simulated 429 with a stored prior analysis, status stale, still ranked\n"
            "- `NOPRIOR429`: simulated 429 with no prior analysis, status partial\n"
            "- `FAILPERM`: simulated permanent error, status failed, listed but not ranked"
        )

    if st.button("Run", type="primary"):
        tickers, rejected = pipeline.normalise_tickers(raw, config.GUI_MAX_TICKERS)
        for token, reason in rejected:
            st.warning(f"Skipped {token}: {reason}")
        if not tickers:
            st.error("Enter at least one valid ticker.")
        else:
            result, crash = run_with_progress(tickers, horizon)
            if crash:
                st.error(f"The run crashed: {crash}")
            else:
                st.session_state["last_result"] = result

    if "last_result" in st.session_state:
        render_result(st.session_state["last_result"])


def main() -> None:
    st.title("Equity Growth Analyst")
    st.warning(
        "The store, the scorer, the filings reader, the upload box, the permanent-storage "
        "copy, the financial data fetch and the AI analysis are real. Any ticker other than "
        "the four demo tickers fetches financial data (Yahoo and BSE), reads your uploaded "
        "filings and calls Gemini. The demo tickers use fake data and all show the same total. "
        "Technicals and the Price-Impact score are not built yet."
    )

    try:
        notices = config.validate_config()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()
    for note in notices:
        if note.startswith(STALE_NOTICE_PREFIXES):
            continue  # out of date: the scorer is real now
        st.info(note)

    with st.sidebar:
        st.header("Run settings")
        horizon = st.slider(
            "Holding horizon (days)",
            config.HORIZON_MIN_DAYS, config.HORIZON_MAX_DAYS,
            config.DEFAULT_HORIZON_DAYS,
        )
        st.caption(f"Gemini key present: {'yes' if config.has_secret('gemini_api_key') else 'no'}")
        render_storage_status()
        try:
            st.caption(f"Stored analyses: {len(pipeline.default_store().list_tickers())}")
        except Exception:
            st.caption("Stored analyses: unavailable")
        st.caption(f"Code {config.CODE_VERSION} | spec {config.SPEC_VERSION}")

    tab_run, tab_files, tab_inputs, tab_saved, tab_diag = st.tabs(
        ["Run analysis", "Filings", "Inputs", "Saved analyses", "Diagnostics"])
    with tab_run:
        render_run_panel(horizon)
    with tab_files:
        render_filings_panel()
    with tab_inputs:
        render_inputs_panel()
    with tab_saved:
        render_saved_panel()
    with tab_diag:
        render_diagnostics_panel()


main()

