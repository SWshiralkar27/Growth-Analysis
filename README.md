# Equity Growth Analyst

Version: Step 1, v0.1.0 (2026-11-01). Code version 2026-11-step1. Spec: README v3.1 DRAFT.

Python research engine for Indian listed equities. **Step 1 is a skeleton**: config, an orchestrator with stub stages, and a Streamlit front end. There is no real data, analysis or scoring yet.

## Run locally

    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    streamlit run app.py

## Run the tests (no network, no key, no Streamlit needed)

    python -m unittest discover -s tests -v

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository and push this folder to it (private if reports or filings are sensitive).
2. Go to share.streamlit.io, sign in with GitHub and choose "New app".
3. Pick the repository, the branch and `app.py` as the main file.
4. Open "Advanced settings" and paste your secrets (see `.streamlit/secrets.toml.example`). Never commit a real key.
5. Deploy.

## Important limits

- **Storage is temporary on Streamlit Community Cloud.** Files written to `data/` can vanish on restart or redeploy. Until a persistent backend is added behind `store.py`, treat the cloud app as a demo and run locally for real work.
- **Batch runs** (about 90 tickers overnight) do not belong on Streamlit Cloud. `batch_runner.py` will be a separate command-line script (run locally or on a scheduled GitHub Actions job).
- Secrets come from environment variables only. No key is ever written in code.

## Layout

    app.py            Streamlit front end (no logic)
    pipeline.py       orchestrator: stages, failure rules, ranking
    config.py         constants, paths, versions, start-up checks
    errors.py         typed errors (Transient, Permanent, Config)
    tests/            unit tests

## Demo tickers (stub stages)

`FAKE` ok, `STALE429` stale (prior analysis kept), `NOPRIOR429` partial, `FAILPERM` failed.
