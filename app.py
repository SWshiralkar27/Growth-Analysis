"""
app.py
Version: Step 2, v0.2.0 (2026-11-02). Code version 2026-11-step2.
Spec: README v3.1 DRAFT, sections 5.3 and 13 (Streamlit replaces the desktop
GUI; this is a spec change awaiting recording in v3.2).

Purpose: Streamlit front end. It collects tickers and a horizon, runs the
pipeline in a worker thread and shows progress from the main thread. It holds
no scoring, fetching or report logic.

Run locally:  streamlit run app.py
"""
from __future__ import annotations

import queue
import threading

import pandas as pd
import streamlit as st

import config
import pipeline
from errors import ConfigError

st.set_page_config(page_title="Equity Growth Analyst", layout="wide")


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
    st.caption(
        f"Horizon {result.horizon_days} days | code {result.code_version} | "
        f"spec {result.spec_version} | prompt {result.prompt_version} | "
        f"finished {result.finished_at}"
    )


def main() -> None:
    st.title("Equity Growth Analyst")
    st.warning(
        "Skeleton: every stage except the store is a stub. There is no real "
        "data, analysis or score yet; the table below only proves the flow, "
        "the failure rules, the ranking and the saved records."
    )

    try:
        notices = config.validate_config()
    except ConfigError as exc:
        st.error(f"Configuration error: {exc}")
        st.stop()
    for note in notices:
        st.info(note)

    with st.sidebar:
        st.header("Run settings")
        horizon = st.slider(
            "Holding horizon (days)",
            config.HORIZON_MIN_DAYS, config.HORIZON_MAX_DAYS,
            config.DEFAULT_HORIZON_DAYS,
        )
        st.caption(f"Gemini key present: {'yes' if config.has_secret('gemini_api_key') else 'no'}")
        try:
            st.caption(f"Stored analyses: {len(pipeline.default_store().list_tickers())}")
        except Exception:
            st.caption("Stored analyses: unavailable")
        st.caption(f"Code {config.CODE_VERSION} | spec {config.SPEC_VERSION}")

    raw = st.text_area(
        f"Tickers (1 to {config.GUI_MAX_TICKERS}, separated by commas or new lines)",
        value="FAKE", height=100,
    )
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


main()
