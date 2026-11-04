# Equity Growth Analyst

Version: Step 7, v0.7.0 (2026-11-04). Code version 2026-11-step7. Spec: README v3.1 DRAFT.

Python research engine for Indian listed equities. **Steps 1 to 7 are a skeleton with a real store, scorer, filings reader, upload box, permanent storage and AI analysis**: config, an orchestrator, an atomic file store, the recovered v2.1 scorer, the filings reader, a filing upload panel, a copy to a private GitHub data repository, the Gemini analysis and a Streamlit front end. Data fetching is still a stub, so every financial signal is scored at the prior.

## Run locally

    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    streamlit run app.py

## Run the tests (no network, no key, no Streamlit needed)

    python -m unittest discover -s . -p "test_*.py" -v

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository and push this folder to it (private if reports or filings are sensitive).
2. Go to share.streamlit.io, sign in with GitHub and choose "New app".
3. Pick the repository, the branch and `app.py` as the main file.
4. Open "Advanced settings" and paste your secrets (see `.streamlit/secrets.toml.example`). Never commit a real key.
5. Deploy.

## Uploading filings

Open the **Filings** tab, enter a ticker, choose the document type and date, pick a PDF (up to 50 MB) and tap **Save file**. Files are stored as `TICKER_YYYY_MM_DD_Type.pdf`. Nothing is ever overwritten; to replace a file, remove it first. Uploaded files are temporary on Streamlit Community Cloud until persistent storage is added.

## Permanent storage (private GitHub data repository)

Streamlit Cloud's disk is temporary, so the app can copy saved analyses and uploaded filings to a **second, private** GitHub repository and bring them back after a restart.

1. Create a new **private** repository (for example `Growth-Analysis-Data`) with a README so it has a `main` branch.
2. Create a fine-grained access token limited to **that repository only**, with **Contents: Read and write**, and an expiry date.
3. In the Streamlit app's **Settings, Secrets**, add these three lines (never put them in the code repository):

        GITHUB_DATA_REPO = "yourname/Growth-Analysis-Data"
        GITHUB_DATA_TOKEN = "paste the token here"
        GITHUB_DATA_BRANCH = "main"

Without these secrets the app works exactly as before, with temporary storage. A failed backup never stops an analysis; it is shown on screen as "not backed up". Nothing is deleted from the data repository except when you tap Remove on a filing. Very large PDFs (near 50 MB) may fail to copy; test with a real one.

## The AI analysis (Gemini)

Add your key in the Streamlit secrets (never in the code repository):

    GEMINI_API_KEY = "paste the key here"

Optional: `GEMINI_MODEL` (default `gemini-3.5-flash`) and `GEMINI_THINKING_LEVEL` (default `low`). Any ticker other than the four demo tickers reads its uploaded filings and calls Gemini, at most 5 calls a minute. A 429 or 503 is retried; if it persists the ticker keeps its previous analysis and is marked stale. Without a key those tickers show as failed. Identical inputs are not sent twice in a session.

## Important limits

- **Local storage is temporary on Streamlit Community Cloud.** Files written to `data/` vanish on restart or redeploy. Turn on permanent storage (above) to keep them.
- **Batch runs** (about 90 tickers overnight) do not belong on Streamlit Cloud. `batch_runner.py` will be a separate command-line script (run locally or on a scheduled GitHub Actions job).
- The store keeps one JSON file per ticker. A save is written to a temp file and swapped in, so a crash or an error never damages a prior record.
- Secrets come from environment variables only. No key is ever written in code.

## Layout

    app.py            Streamlit front end (no logic)
    pipeline.py       orchestrator: stages, failure rules, ranking
    config.py         constants, paths, versions, start-up checks
    errors.py         typed errors (Transient, Permanent, Config, Store)
    store.py          atomic per-ticker records (data/store/)
    score_card.py     the recovered v2.1 quality and governance scorer (unchanged logic)
    pdf_extractor.py  reads filing PDFs and builds the AI context (unchanged logic)
    filings.py        saves, lists and removes uploaded filing PDFs (atomic, never overwrites)
    persistence.py    copies saved records and filings to a private GitHub repository and restores them
    gemini_analyser.py  asks Gemini for a structured analysis and verifies every quote (prompt and schema rebuilt)
    rate_limiter.py   spaces Gemini calls (5 a minute)
    test_step1.py     tests for config, tickers, pipeline
    test_store.py     tests for the store and the 429 rule
    test_score_card.py  tests for the scorer (ported from the old test_patches.py)
    test_pdf_extractor.py  tests for the filings reader and context builder
    test_filings.py   tests for the upload engine
    test_persistence.py  tests for the permanent-storage copy (uses a fake GitHub)
    test_gemini_analyser.py  tests for the analyser, retries, cache and the pipeline stage (uses a fake Gemini)

## Demo tickers (stub stages)

`FAKE` ok, `STALE429` stale (prior analysis kept), `NOPRIOR429` partial, `FAILPERM` failed.
