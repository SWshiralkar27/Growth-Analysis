"""
persistence.py
Version: Step 6, v0.6.0 (2026-11-03). Code version 2026-11-step6.
Spec: README v3.1 DRAFT section 10 (Store); permanent storage in a private GitHub
repository, chosen by the owner on 2026-11-03.

Purpose: mirror files from the local data folder to a private GitHub repository and
restore them after a restart, using GitHub's REST API. One job: copy files. It knows
nothing about what the files mean and nothing about other engine modules.

Rules implemented here:
  - Local files are the working copy. A failed backup NEVER raises and never touches
    local data; every public method returns a result and records it in the sync log.
  - Nothing is deleted from the repository because of an error. Remote deletes happen
    only when delete_file is called, which the orchestrator does only on an explicit
    user removal.
  - Restore never overwrites an existing local file, and every downloaded file is
    checked against GitHub's own content hash before it is kept.
  - Only paths under config.PERSIST_PREFIXES are ever read or written; absolute paths,
    "..", hidden names and anything else are refused before any network call.
  - The token is only ever sent in the Authorization header. It is not part of any URL,
    and any message that leaves this module has the token removed.
  - Unchanged files are not re-uploaded (compared with git's blob hash).
Known limit: GitHub's file API can struggle with files near 100 MB. The upload box
allows up to 50 MB; test with a real large filing before relying on it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

import config

API = "https://api.github.com"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


# ------------------------------------------------------------------ results
@dataclass
class OpResult:
    ok: bool
    status: str          # pushed, unchanged, deleted, absent, restored, skipped, refused, failed
    message: str = ""
    path: str = ""


@dataclass
class PullResult:
    restored: list = field(default_factory=list)
    skipped: int = 0
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class RemoteFile:
    path: str
    sha: str
    size: int


class SyncLog:
    """Thread-safe record of recent sync operations (for the status line on screen)."""

    def __init__(self, maxlen: int = 50) -> None:
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=maxlen)
        self.ok_count = 0
        self.failed_count = 0
        self.last_error: Optional[str] = None

    def record(self, op: str, path: str, ok: bool, message: str = "") -> None:
        with self._lock:
            self._events.append((time.time(), op, path, ok, message))
            if ok:
                self.ok_count += 1
            else:
                self.failed_count += 1
                self.last_error = f"{op} {path}: {message}"

    def summary(self) -> dict:
        with self._lock:
            return {"ok": self.ok_count, "failed": self.failed_count, "last_error": self.last_error}


# --------------------------------------------------------------- helpers
def git_blob_sha(data: bytes) -> str:
    """The hash git itself gives a file's content (what GitHub reports as its sha)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def urllib_http(method: str, url: str, headers: dict, body: Optional[bytes] = None,
                timeout: int = 120) -> tuple[int, bytes]:
    """Default transport: returns (status, bytes). Network failures raise OSError."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(str(getattr(exc, "reason", exc))) from exc


HttpFn = Callable[..., tuple]


class GitHubMirror:
    def __init__(self, repo: str, token: str, branch: str = "main", root: Optional[Path] = None,
                 http: Optional[HttpFn] = None, api: str = API,
                 tree_ttl: Optional[float] = None) -> None:
        if not _REPO_RE.match(repo or ""):
            raise ValueError("the repository must look like owner/name")
        if not token:
            raise ValueError("a token is required")
        self.repo = repo
        self.branch = branch or "main"
        self._token = token
        self.root = Path(root) if root is not None else Path(config.DATA_DIR)
        self._http = http or urllib_http
        self._api = api.rstrip("/")
        self._tree_ttl = config.PERSIST_TREE_CACHE_SECONDS if tree_ttl is None else tree_ttl
        self._tree: Optional[list] = None
        self._tree_at = 0.0
        self._lock = threading.RLock()
        self.log = SyncLog()

    # ---------------------------------------------------------- plumbing
    def _clean(self, text: str) -> str:
        return str(text).replace(self._token, "***")[:300]

    def _headers(self, raw: bool = False) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "growth-analyst-persistence",
        }

    def _request(self, method: str, url: str, body: Optional[dict] = None,
                 raw: bool = False) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers(raw)
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._http(method, url, headers, data)
        except OSError as exc:
            return 0, self._clean(f"network error: {exc}").encode("utf-8")

    def _message(self, status: int, data: bytes) -> str:
        if status == 0:
            return self._clean(data.decode("utf-8", "replace"))
        try:
            msg = json.loads(data.decode("utf-8")).get("message", "")
        except (ValueError, AttributeError):
            msg = data[:120].decode("utf-8", "replace")
        return self._clean(f"HTTP {status}: {msg}")

    def _contents_url(self, rel: str, with_ref: bool = True) -> str:
        url = f"{self._api}/repos/{self.repo}/contents/{urllib.parse.quote(rel, safe='/')}"
        return url + (f"?ref={urllib.parse.quote(self.branch, safe='')}" if with_ref else "")

    @staticmethod
    def _check_rel(rel: str) -> Optional[str]:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
            return "path is not a relative path"
        parts = PurePosixPath(rel).parts
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            return "path contains a hidden or parent segment"
        if not any(rel.startswith(pref) for pref in config.PERSIST_PREFIXES):
            return "path is outside the folders that are backed up"
        return None

    def _finish(self, op: str, rel: str, res: OpResult) -> OpResult:
        self.log.record(op, rel, res.ok, res.message)
        return res

    def _invalidate_tree(self) -> None:
        with self._lock:
            self._tree = None

    def _remote_sha(self, rel: str) -> tuple[Optional[str], Optional[str]]:
        """Return (sha, error). sha None with error None means the file is not there."""
        status, data = self._request("GET", self._contents_url(rel))
        if status == 404:
            return None, None
        if status != 200:
            return None, self._message(status, data)
        try:
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict) or "sha" not in payload:
                return None, "unexpected reply from GitHub"
            return str(payload["sha"]), None
        except ValueError:
            return None, "unexpected reply from GitHub"

    # ------------------------------------------------------------- push
    def push_file(self, rel: str) -> OpResult:
        err = self._check_rel(rel)
        if err:
            return self._finish("push", rel, OpResult(False, "refused", err, rel))
        try:
            content = (self.root / rel).read_bytes()
        except OSError as exc:
            return self._finish("push", rel, OpResult(False, "failed", self._clean(f"cannot read local file: {exc}"), rel))

        for attempt in range(2):
            sha, err = self._remote_sha(rel)
            if err:
                return self._finish("push", rel, OpResult(False, "failed", err, rel))
            if sha is not None and sha == git_blob_sha(content):
                return self._finish("push", rel, OpResult(True, "unchanged", "", rel))
            body = {"message": f"update {rel}", "branch": self.branch,
                    "content": base64.b64encode(content).decode("ascii")}
            if sha is not None:
                body["sha"] = sha
            status, data = self._request("PUT", self._contents_url(rel, with_ref=False), body)
            if status in (200, 201):
                self._invalidate_tree()
                return self._finish("push", rel, OpResult(True, "pushed", "", rel))
            if status in (409, 422) and attempt == 0:
                continue   # the file changed under us: fetch the new sha and retry once
            return self._finish("push", rel, OpResult(False, "failed", self._message(status, data), rel))
        return self._finish("push", rel, OpResult(False, "failed", "could not update after a retry", rel))

    # ----------------------------------------------------------- delete
    def delete_file(self, rel: str) -> OpResult:
        err = self._check_rel(rel)
        if err:
            return self._finish("delete", rel, OpResult(False, "refused", err, rel))
        sha, err = self._remote_sha(rel)
        if err:
            return self._finish("delete", rel, OpResult(False, "failed", err, rel))
        if sha is None:
            return self._finish("delete", rel, OpResult(True, "absent", "", rel))
        status, data = self._request("DELETE", self._contents_url(rel, with_ref=False),
                                     {"message": f"remove {rel}", "sha": sha, "branch": self.branch})
        if status == 200:
            self._invalidate_tree()
            return self._finish("delete", rel, OpResult(True, "deleted", "", rel))
        return self._finish("delete", rel, OpResult(False, "failed", self._message(status, data), rel))

    # ------------------------------------------------------------- pull
    def list_remote(self) -> tuple[Optional[list], str]:
        """Return (files, error). One call, cached for a short time."""
        with self._lock:
            if self._tree is not None and (time.time() - self._tree_at) < self._tree_ttl:
                return self._tree, ""
        url = f"{self._api}/repos/{self.repo}/git/trees/{urllib.parse.quote(self.branch, safe='')}?recursive=1"
        status, data = self._request("GET", url)
        if status != 200:
            return None, self._message(status, data)
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            return None, "unexpected reply from GitHub"
        if payload.get("truncated"):
            return None, "the data repository is too large to list in one call"
        files = [RemoteFile(e["path"], e["sha"], int(e.get("size", 0)))
                 for e in payload.get("tree", []) if e.get("type") == "blob"]
        with self._lock:
            self._tree, self._tree_at = files, time.time()
        return files, ""

    def pull_file(self, rel: str, expected_sha: Optional[str] = None) -> OpResult:
        err = self._check_rel(rel)
        if err:
            return self._finish("pull", rel, OpResult(False, "refused", err, rel))
        target = self.root / rel
        if target.exists():
            return OpResult(True, "skipped", "already present locally", rel)
        status, data = self._request("GET", self._contents_url(rel), raw=True)
        if status != 200:
            return self._finish("pull", rel, OpResult(False, "failed", self._message(status, data), rel))
        if expected_sha is not None and git_blob_sha(data) != expected_sha:
            return self._finish("pull", rel, OpResult(False, "failed", "integrity check failed; download discarded", rel))
        tmp_path: Optional[Path] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            tmp_path = None
        except Exception as exc:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            return self._finish("pull", rel, OpResult(False, "failed", self._clean(f"could not write locally: {exc}"), rel))
        return self._finish("pull", rel, OpResult(True, "restored", "", rel))

    def _pull_matching(self, wanted: Callable[[str], bool]) -> PullResult:
        result = PullResult()
        files, err = self.list_remote()
        if files is None:
            result.errors.append(err)
            self.log.record("pull", "(list)", False, err)
            return result
        for rf in files:
            if not wanted(rf.path) or self._check_rel(rf.path):
                continue
            res = self.pull_file(rf.path, expected_sha=rf.sha)
            if res.status == "restored":
                result.restored.append(rf.path)
            elif res.ok:
                result.skipped += 1
            else:
                result.errors.append(f"{rf.path}: {res.message}")
        return result

    def pull_missing(self, prefixes: tuple = ("store/",)) -> PullResult:
        """Restore every backed-up file under these prefixes that is missing locally."""
        return self._pull_matching(lambda path: any(path.startswith(p) for p in prefixes))

    def pull_ticker_filings(self, ticker: str) -> PullResult:
        """Restore one ticker's filings (only the ones missing locally)."""
        if not config.TICKER_PATTERN.match(ticker or ""):
            return PullResult(errors=["invalid ticker"])
        return self._pull_matching(
            lambda path: path.startswith(f"filings/{ticker}_") or path == f"filings/{ticker}.pdf")


def make_mirror() -> Optional[GitHubMirror]:
    """Build the mirror from the secrets, or return None (permanent storage off)."""
    repo = config.get_secret("github_data_repo")
    token = config.get_secret("github_data_token")
    if not repo or not token:
        return None
    try:
        return GitHubMirror(repo.strip(), token.strip(),
                            (config.get_secret("github_data_branch") or "main").strip())
    except ValueError:
        return None
