"""
test_persistence.py
Version: Step 6, v0.6.0 (2026-11-03).
Tests for persistence.py and for how the pipeline uses it. GitHub is replaced by an
in-memory fake that behaves like its file API (create needs no sha, update needs the
current sha, a wrong sha is a conflict). These tests prove the logic; only the live app
can prove the real GitHub connection.
Run:  python -m unittest discover -s . -p "test_*.py" -v
"""
import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import config
import persistence as P
import pipeline
from store import Store
from test_pdf_extractor import make_pdf

TOKEN = "SECRETTOKEN123"
D = date(2026, 5, 12)


class FakeGitHub:
    """Callable transport that emulates the parts of GitHub's API we use."""

    def __init__(self):
        self.files = {}          # path -> bytes
        self.requests = []       # (method, url, headers, body)
        self.hook = None         # hook(method, url) -> (status, bytes) | None; may raise OSError
        self.corrupt = {}        # path -> bytes returned instead of the real content
        self.truncated = False
        self.commits = 0

    def __call__(self, method, url, headers, body=None):
        self.requests.append((method, url, dict(headers), body))
        if self.hook:
            r = self.hook(method, url)
            if r is not None:
                return r
        rest = url.split("/repos/me/data/", 1)[1]
        if rest.startswith("git/trees/"):
            tree = [{"path": p, "type": "blob", "sha": P.git_blob_sha(d), "size": len(d)}
                    for p, d in sorted(self.files.items())]
            return 200, json.dumps({"truncated": self.truncated, "tree": tree}).encode()
        path = urllib.parse.unquote(rest[len("contents/"):].split("?")[0])
        raw = headers.get("Accept", "").endswith("raw+json")
        if method == "GET":
            if path not in self.files:
                return 404, b'{"message":"Not Found"}'
            data = self.files[path]
            if raw:
                return 200, self.corrupt.get(path, data)
            return 200, json.dumps({"type": "file", "sha": P.git_blob_sha(data)}).encode()
        payload = json.loads(body)
        exists = path in self.files
        if method == "PUT":
            if exists and "sha" not in payload:
                return 422, b'{"message":"sha was not supplied"}'
            if exists and payload["sha"] != P.git_blob_sha(self.files[path]):
                return 409, b'{"message":"does not match"}'
            self.files[path] = base64.b64decode(payload["content"])
            self.commits += 1
            return (200 if exists else 201), b'{"content":{}}'
        if method == "DELETE":
            if not exists:
                return 404, b'{"message":"Not Found"}'
            if payload["sha"] != P.git_blob_sha(self.files[path]):
                return 409, b'{"message":"does not match"}'
            del self.files[path]
            return 200, b"{}"
        return 500, b'{"message":"unsupported"}'

    def calls(self, method):
        return [r for r in self.requests if r[0] == method]


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "data"
        self.fake = FakeGitHub()
        self.mirror = P.GitHubMirror("me/data", TOKEN, http=self.fake, root=self.root, tree_ttl=0)

    def write(self, rel, data=b"hello\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class HashTests(unittest.TestCase):
    def test_matches_gits_own_hash(self):
        self.assertEqual(P.git_blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


class PushTests(MirrorCase):
    def test_new_file_is_created_and_unchanged_file_is_not_re_uploaded(self):
        self.write("store/TCS.json", b'{"a": 1}')
        self.assertEqual(self.mirror.push_file("store/TCS.json").status, "pushed")
        self.assertEqual(self.fake.files["store/TCS.json"], b'{"a": 1}')
        again = self.mirror.push_file("store/TCS.json")
        self.assertEqual(again.status, "unchanged")
        self.assertEqual(self.fake.commits, 1)
        self.assertEqual(len(self.fake.calls("PUT")), 1)

    def test_changed_file_is_updated_using_the_current_sha(self):
        self.write("store/TCS.json", b"one")
        self.mirror.push_file("store/TCS.json")
        self.write("store/TCS.json", b"two")
        res = self.mirror.push_file("store/TCS.json")
        self.assertEqual(res.status, "pushed")
        self.assertEqual(self.fake.files["store/TCS.json"], b"two")
        self.assertEqual(self.fake.commits, 2)

    def test_a_conflict_is_retried_once_with_a_fresh_sha(self):
        self.fake.files["store/TCS.json"] = b"remote"
        self.write("store/TCS.json", b"local")
        state = {"n": 0}

        def hook(method, url):
            if method == "PUT" and state["n"] == 0:
                state["n"] += 1
                return 409, b'{"message":"does not match"}'
        self.fake.hook = hook
        self.assertEqual(self.mirror.push_file("store/TCS.json").status, "pushed")
        self.assertEqual(self.fake.files["store/TCS.json"], b"local")

    def test_names_with_special_characters_are_encoded(self):
        self.write("filings/M&M_2026_05_12_AnnualReport.pdf", b"%PDF-1.4")
        self.assertEqual(self.mirror.push_file("filings/M&M_2026_05_12_AnnualReport.pdf").status, "pushed")
        self.assertIn("filings/M&M_2026_05_12_AnnualReport.pdf", self.fake.files)
        self.assertTrue(any("M%26M" in r[1] for r in self.fake.requests))

    def test_missing_local_file_fails_without_raising(self):
        res = self.mirror.push_file("store/NOPE.json")
        self.assertFalse(res.ok)
        self.assertEqual(self.fake.requests, [])


class SafetyTests(MirrorCase):
    def test_unsafe_paths_are_refused_before_any_network_call(self):
        for rel in ("../x.json", "/abs/store/x.json", "store/../x.json", "store/.hidden.json",
                    "other/x.json", "", "store\\x.json", "./store/x.json"):
            for fn in (self.mirror.push_file, self.mirror.delete_file, self.mirror.pull_file):
                res = fn(rel)
                self.assertFalse(res.ok, rel)
                self.assertEqual(res.status, "refused", rel)
        self.assertEqual(self.fake.requests, [])

    def test_token_is_only_in_the_authorization_header(self):
        self.write("store/TCS.json")
        self.mirror.push_file("store/TCS.json")
        self.mirror.list_remote()
        for method, url, headers, body in self.fake.requests:
            self.assertNotIn(TOKEN, url)
            self.assertNotIn(TOKEN.encode(), body or b"")
            self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")

    def test_bad_repository_or_missing_token_is_rejected(self):
        for repo, token in (("bad", TOKEN), ("a/b/c", TOKEN), ("me/data", "")):
            with self.assertRaises(ValueError):
                P.GitHubMirror(repo, token)


class FailureTests(MirrorCase):
    def test_http_errors_are_reported_not_raised_and_hide_the_token(self):
        self.write("store/TCS.json")
        self.fake.hook = lambda m, u: (401, b'{"message":"Bad credentials"}')
        res = self.mirror.push_file("store/TCS.json")
        self.assertFalse(res.ok)
        self.assertIn("401", res.message)
        self.assertNotIn(TOKEN, res.message)

    def test_network_failure_is_reported_and_hides_the_token(self):
        self.write("store/TCS.json")

        def hook(method, url):
            raise OSError(f"connection reset {TOKEN}")
        self.fake.hook = hook
        res = self.mirror.push_file("store/TCS.json")
        self.assertFalse(res.ok)
        self.assertIn("network error", res.message)
        self.assertNotIn(TOKEN, res.message)
        self.assertEqual(self.mirror.log.summary()["failed"], 1)
        self.assertNotIn(TOKEN, self.mirror.log.summary()["last_error"])

    def test_a_failed_backup_never_touches_local_data(self):
        path = self.write("store/TCS.json", b"keep me")
        self.fake.hook = lambda m, u: (500, b'{"message":"boom"}')
        self.mirror.push_file("store/TCS.json")
        self.assertEqual(path.read_bytes(), b"keep me")


class DeleteTests(MirrorCase):
    def test_delete_removes_the_remote_file_and_absent_is_fine(self):
        self.fake.files["filings/TCS_2026_05_12_AnnualReport.pdf"] = b"x"
        self.assertEqual(self.mirror.delete_file("filings/TCS_2026_05_12_AnnualReport.pdf").status, "deleted")
        self.assertEqual(self.fake.files, {})
        self.assertEqual(self.mirror.delete_file("filings/TCS_2026_05_12_AnnualReport.pdf").status, "absent")

    def test_a_failed_delete_leaves_the_remote_file(self):
        self.fake.files["filings/TCS.pdf"] = b"x"

        def hook(method, url):
            if method == "DELETE":
                return 403, b'{"message":"forbidden"}'
        self.fake.hook = hook
        self.assertFalse(self.mirror.delete_file("filings/TCS.pdf").ok)
        self.assertIn("filings/TCS.pdf", self.fake.files)


class PullTests(MirrorCase):
    def test_restores_only_missing_files_and_never_overwrites(self):
        self.fake.files.update({"store/A.json": b"remote A", "store/B.json": b"remote B",
                                "filings/X.pdf": b"%PDF"})
        self.write("store/A.json", b"local A")
        res = self.mirror.pull_missing(("store/",))
        self.assertEqual(res.restored, ["store/B.json"])
        self.assertEqual(res.skipped, 1)
        self.assertTrue(res.ok)
        self.assertEqual((self.root / "store/A.json").read_bytes(), b"local A")
        self.assertEqual((self.root / "store/B.json").read_bytes(), b"remote B")
        self.assertFalse((self.root / "filings/X.pdf").exists())

    def test_a_download_that_fails_the_integrity_check_is_discarded(self):
        self.fake.files["store/B.json"] = b"real"
        self.fake.corrupt["store/B.json"] = b"tampered"
        res = self.mirror.pull_missing(("store/",))
        self.assertFalse(res.ok)
        self.assertIn("integrity", res.errors[0])
        self.assertFalse((self.root / "store/B.json").exists())
        self.assertEqual(os.listdir(self.root / "store") if (self.root / "store").exists() else [], [])

    def test_ticker_filings_only_for_that_ticker(self):
        self.fake.files.update({
            "filings/TCS_2026_05_12_AnnualReport.pdf": b"%PDF-a", "filings/TCS.pdf": b"%PDF-b",
            "filings/INFY_2026_05_12_AnnualReport.pdf": b"%PDF-c", "filings/TCSX_2026_05_12_Results.pdf": b"%PDF-d"})
        res = self.mirror.pull_ticker_filings("TCS")
        self.assertEqual(sorted(res.restored), ["filings/TCS.pdf", "filings/TCS_2026_05_12_AnnualReport.pdf"])
        self.assertFalse((self.root / "filings/INFY_2026_05_12_AnnualReport.pdf").exists())
        self.assertFalse(self.mirror.pull_ticker_filings("bad/ticker").ok)

    def test_a_truncated_listing_is_an_error_not_a_partial_restore(self):
        self.fake.files["store/A.json"] = b"x"
        self.fake.truncated = True
        res = self.mirror.pull_missing(("store/",))
        self.assertFalse(res.ok)
        self.assertIn("too large", res.errors[0])

    def test_listing_is_cached_briefly_and_refreshed_after_a_push(self):
        mirror = P.GitHubMirror("me/data", TOKEN, http=self.fake, root=self.root, tree_ttl=60)
        self.fake.files["store/A.json"] = b"x"
        mirror.list_remote()
        mirror.list_remote()
        trees = [r for r in self.fake.requests if "/git/trees/" in r[1]]
        self.assertEqual(len(trees), 1)
        self.write("store/B.json", b"y")
        mirror.push_file("store/B.json")
        mirror.list_remote()
        trees = [r for r in self.fake.requests if "/git/trees/" in r[1]]
        self.assertEqual(len(trees), 2)


class FactoryAndLogTests(unittest.TestCase):
    def test_make_mirror_needs_repo_and_token(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(P.make_mirror())
        with mock.patch.dict(os.environ, {"GITHUB_DATA_REPO": "me/data"}, clear=True):
            self.assertIsNone(P.make_mirror())
        with mock.patch.dict(os.environ, {"GITHUB_DATA_REPO": "me/data", "GITHUB_DATA_TOKEN": TOKEN,
                                          "GITHUB_DATA_BRANCH": "backup"}, clear=True):
            m = P.make_mirror()
        self.assertEqual((m.repo, m.branch), ("me/data", "backup"))

    def test_invalid_repo_name_turns_storage_off_instead_of_crashing(self):
        with mock.patch.dict(os.environ, {"GITHUB_DATA_REPO": "not a repo", "GITHUB_DATA_TOKEN": TOKEN}, clear=True):
            self.assertIsNone(P.make_mirror())

    def test_sync_log_counts(self):
        log = P.SyncLog()
        log.record("push", "a", True)
        log.record("push", "b", False, "boom")
        self.assertEqual(log.summary(), {"ok": 1, "failed": 1, "last_error": "push b: boom"})


class PipelineBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name) / "data"
        for name, value in (("DATA_DIR", base), ("STORE_DIR", base / "store"),
                            ("FILINGS_DIR", base / "filings"), ("FILINGS_FOLDER", str(base / "filings"))):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = FakeGitHub()
        self.mirror = P.GitHubMirror("me/data", TOKEN, http=self.fake, root=base, tree_ttl=0)
        pipeline.set_mirror(self.mirror)
        self.addCleanup(pipeline.reset_mirror)
        self.store = Store(config.STORE_DIR)

    def run_tickers(self, tickers):
        stages, _ = pipeline.make_stub_stages(store=self.store)
        return pipeline.run_pipeline(tickers, 30, stages)

    def test_a_successful_save_is_copied_to_permanent_storage(self):
        run = self.run_tickers(["FAKE"])
        self.assertEqual(run.results[0].status, "ok")
        self.assertEqual(list(self.fake.files), ["store/FAKE.json"])
        self.assertFalse(any("NOT BACKED UP" in l for l in run.results[0].breakdown.labels))

    def test_a_failed_backup_keeps_status_ok_and_says_so(self):
        self.fake.hook = lambda m, u: (401, b'{"message":"Bad credentials"}')
        run = self.run_tickers(["FAKE"])
        r = run.results[0]
        self.assertEqual(r.status, "ok")
        self.assertTrue(any(l.startswith("NOT BACKED UP") for l in r.breakdown.labels))
        self.assertTrue(self.store.path_for("FAKE").exists())

    def test_stale_and_failed_tickers_are_not_backed_up(self):
        self.run_tickers(["STALE429", "NOPRIOR429", "FAILPERM"])
        self.assertEqual(self.fake.files, {})

    def test_a_crashing_backup_stage_is_contained(self):
        import dataclasses
        stages, _ = pipeline.make_stub_stages(store=self.store)

        def boom(ticker):
            raise RuntimeError("kaboom")
        run = pipeline.run_pipeline(["FAKE"], 30, dataclasses.replace(stages, backup=boom))
        self.assertEqual(run.results[0].status, "ok")
        self.assertTrue(any("NOT BACKED UP: RuntimeError" in l for l in run.results[0].breakdown.labels))

    def test_records_are_restored_after_a_restart(self):
        self.run_tickers(["FAKE"])
        before = self.store.path_for("FAKE").read_bytes()
        self.store.path_for("FAKE").unlink()                      # simulate the app restarting empty
        res = pipeline.restore_from_backup()
        self.assertEqual(res.restored, ["store/FAKE.json"])
        self.assertEqual(self.store.path_for("FAKE").read_bytes(), before)
        self.assertEqual(self.store.load("FAKE")["ticker"], "FAKE")

    def test_uploaded_filings_are_copied_and_removal_is_mirrored(self):
        res = pipeline.upload_filing("TCS", D, "Annual Report", b"%PDF-1.4 test")
        self.assertEqual(res.backup_note, "backed up")
        self.assertIn(f"filings/{res.name}", self.fake.files)
        self.assertTrue(pipeline.delete_filing("TCS", res.name))
        self.assertEqual(self.fake.files, {})

    def test_a_failed_filing_backup_still_saves_locally_and_warns(self):
        self.fake.hook = lambda m, u: (500, b'{"message":"boom"}')
        res = pipeline.upload_filing("TCS", D, "Annual Report", b"%PDF-1.4 test")
        self.assertTrue(res.backup_note.startswith("not backed up"))
        self.assertTrue((Path(config.FILINGS_FOLDER) / res.name).exists())

    def test_filings_come_back_on_demand_after_a_restart(self):
        pdf_path = Path(self._tmp.name) / "src.pdf"
        make_pdf(pdf_path, ["Order book at a record backlog", "Independent Auditor's Report"])
        res = pipeline.upload_filing("TCS", D, "Annual Report", pdf_path.read_bytes())
        (Path(config.FILINGS_FOLDER) / res.name).unlink()          # simulate the restart
        self.assertEqual([f.name for f in pipeline.stored_filings("TCS")], [res.name])
        (Path(config.FILINGS_FOLDER) / res.name).unlink()
        ctx = pipeline.filings_context_stage("TCS")
        self.assertEqual(ctx["documents"], [res.name])
        self.assertGreater(ctx["meta"]["pages_included"], 0)

    def test_status_reports_counts(self):
        self.run_tickers(["FAKE"])
        info = pipeline.storage_status()
        self.assertTrue(info["enabled"])
        self.assertEqual(info["repo"], "me/data")
        self.assertEqual(info["failed"], 0)
        self.assertGreaterEqual(info["ok"], 1)


class StorageOffTests(unittest.TestCase):
    def setUp(self):
        pipeline.reset_mirror()
        self.addCleanup(pipeline.reset_mirror)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_everything_works_with_storage_off(self):
        self.assertFalse(pipeline.storage_status()["enabled"])
        self.assertIsNone(pipeline.restore_from_backup())
        run = pipeline.run_pipeline(["FAKE"], 30)
        self.assertEqual(run.results[0].status, "ok")
        self.assertFalse(any("BACKED UP" in l for l in run.results[0].breakdown.labels))
        pipeline.ensure_local_filings("TCS")      # no-op, no error

    def test_upload_note_is_empty_when_storage_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "FILINGS_FOLDER", tmp), mock.patch.object(config, "DATA_DIR", Path(tmp)):
                res = pipeline.upload_filing("TCS", D, "Annual Report", b"%PDF-1.4 test")
        self.assertEqual(res.backup_note, "")


if __name__ == "__main__":
    unittest.main()
