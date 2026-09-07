"""The Git correction writer — the one component that will ever hold write
authority over this repository, so it is written to be read as if it did.

Stdlib only, borrowing ``record``'s urllib transport rather than opening a second
HTTP path here. No shell and no external program is anywhere in the path, which
the AST gate in ``tests/test_policy.py`` enforces, and the credential is never
read from disk or the environment: ``GitWriter`` takes a zero-argument callable,
so the only secret it can reach is the one its caller handed it and no token is
ever an attribute of the object.

Reads before writes, refusals before both: ``correct()`` runs every bound in
``policy.evaluate`` that needs no network, then the reads, then ``evaluate`` again
with the snapshot, and only then the mutating calls. Every non-2xx raises
``ApiWriteError`` bar the ref update, whose failure is the expected
non-fast-forward and refuses instead. The README has the rest: dry-run, the reused
blob sha, the switch, and the refusal set.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import urllib.parse
from typing import Any, Callable, Mapping, Optional

from . import policy
from .policy import ALLOWED_REF, ALLOWED_REF_PATH, ALLOWED_REPOSITORY, Refuse, TreeState, Verdict, evaluate
from .record import ApiWriteError, Document, PutResult, Transport, canary_label, label_value, make_key_parts
from .record import _UrllibTransport as _Transport

LOG = logging.getLogger("flagger_recovery.gitwriter")

DEFAULT_BASE_URL = "https://api.github.com"
USER_AGENT, API_VERSION = "flagger-recovery", "2022-11-28"
FILE_MODE = "100644"  # a YAML manifest, never executable; a tree entry needs a mode
AUTHOR = {"name": "flagger-recovery", "email": "flagger-recovery@users.noreply.github.com"}

# The module-level switch. Off means ``corrector()`` refuses before it looks at
# anything, so merging this changes nothing about a running receiver; FRP-007b
# turns it on, together with the credential and the Lease.
CORRECTIONS_ENABLED = False

@dataclasses.dataclass(frozen=True)
class Correction:
    """What the writer did, or would have done: ``commit_sha`` is empty on a refusal
    and on a dry run, whose ``commit`` carries every field but the tree sha."""

    verdict: Verdict
    tree: Optional[Mapping[str, Any]] = None
    commit: Optional[Mapping[str, Any]] = None
    head_sha: str = ""
    commit_sha: str = ""
    dry_run: bool = False

class GitWriter:
    """The GitHub Git Data API, bounded to one repository and one ref. ``transport`` is
    the seam tests record requests through; ``requests``/``mutations`` count attempts,
    so a test can assert that a refusal cost none."""

    def __init__(self, token: Callable[[], str], *, repository: str = ALLOWED_REPOSITORY,
                 base_url: str = DEFAULT_BASE_URL, timeout: float = 10.0, dry_run: bool = True,
                 transport: Optional[Transport] = None) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(f"GitWriter requires an https:// base_url (got {base_url!r})")
        self._token = token
        self._repository = repository
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.dry_run = dry_run
        self._transport = transport or _Transport()
        self.requests = self.mutations = 0

    def _call(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> tuple[int, Any]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
                   "X-GitHub-Api-Version": API_VERSION, "Authorization": f"Bearer {self._token()}"}
        body = None
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
            self.mutations += 1
        self.requests += 1
        status, raw = self._transport.request(
            method, f"{self._base_url}/repos/{self._repository}{path}",
            headers=headers, body=body, ca_file=None, timeout=self._timeout)
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}

    def _read(self, path: str) -> Mapping[str, Any]:
        status, payload = self._call("GET", path)
        if status != 200:
            raise ApiWriteError("GET", path, status, b"")
        return payload

    def read_ref(self) -> str:  # the branch head commit sha
        return str(self._read(f"/git/ref/{ALLOWED_REF_PATH}").get("object", {}).get("sha") or "")

    def read_commit(self, sha: str) -> str:  # a commit's tree sha
        return str(self._read(f"/git/commits/{urllib.parse.quote(sha, safe='')}").get("tree", {}).get("sha") or "")

    def read_blob_sha(self, ref: str, path: str) -> str:
        """The blob sha of ``path`` at ``ref``; empty when absent or not a file, which
        refuses upstream and never licenses a create."""
        status, payload = self._call(
            "GET", f"/contents/{urllib.parse.quote(path)}?{urllib.parse.urlencode({'ref': ref})}")
        if status == 404:
            return ""
        if status != 200:
            raise ApiWriteError("GET", path, status, b"")
        return str(payload.get("sha") or "") if payload.get("type") == "file" else ""

    def compare(self, base: str, head: str) -> tuple[str, tuple[str, ...]]:
        """``(status, changed paths)``; status is GitHub's own identical/ahead/behind/diverged."""
        quote = urllib.parse.quote
        payload = self._read(f"/compare/{quote(base, safe='')}...{quote(head, safe='')}")
        return str(payload.get("status") or ""), tuple(
            str(entry.get("filename") or "") for entry in payload.get("files") or ())

    def snapshot(self, proposal: Mapping[str, Any], verdict: Verdict) -> TreeState:
        """Everything ``policy.evaluate``'s freshness bounds compare. All GETs."""
        failed = str(proposal["failed_source_sha"])
        head_sha = self.read_ref()
        state, since = ("identical", ()) if head_sha == failed else self.compare(failed, head_sha)
        return TreeState(
            head_sha=head_sha,
            head_tree_sha=self.read_commit(head_sha) if head_sha else "",
            blob_at_head=self.read_blob_sha(head_sha, verdict.target_path) if head_sha else "",
            blob_at_failed=self.read_blob_sha(failed, verdict.target_path),
            blob_at_restore=self.read_blob_sha(verdict.restore_sha, verdict.target_path),
            head_is_descendant=state in ("identical", "ahead"),
            paths_since_failed=since,
            paths_undone=self.compare(verdict.restore_sha, failed)[1]
            if verdict.form == policy.REVERT_COMMIT else ())

    def _create(self, path: str, body: Mapping[str, Any]) -> str:
        status, payload = self._call("POST", path, body)
        if status not in (200, 201):
            raise ApiWriteError("POST", path, status, b"")
        return str(payload.get("sha") or "")

    def correct(self, proposal: Mapping[str, Any], live: Any) -> Correction:
        """Evaluate, read, re-evaluate, write. Safe to call directly: the bounds are
        re-checked here, never assumed from the caller."""
        if self._repository != ALLOWED_REPOSITORY:
            return Correction(Refuse(policy.WRONG_REPOSITORY))
        verdict = evaluate(proposal, live)
        if not verdict:
            return Correction(verdict)
        tree = self.snapshot(proposal, verdict)
        verdict = evaluate(proposal, live, tree)
        if not verdict:
            return Correction(verdict, head_sha=tree.head_sha)
        tree_body = {"base_tree": tree.head_tree_sha, "tree": [
            {"path": verdict.target_path, "mode": FILE_MODE, "type": "blob", "sha": verdict.blob_sha}]}
        commit_body = {"message": policy.commit_message(proposal, verdict), "tree": "",
                       "parents": [tree.head_sha], "author": AUTHOR, "committer": AUTHOR}
        if self.dry_run:
            return Correction(verdict, tree_body, commit_body, tree.head_sha, dry_run=True)
        commit_body = {**commit_body, "tree": self._create("/git/trees", tree_body)}
        commit_sha = self._create("/git/commits", commit_body)
        # Never force. A non-2xx here is the non-fast-forward this design is built
        # around: the branch moved under us, so refuse and re-evaluate from scratch
        # next time rather than retry against a stale parent.
        status, _ = self._call("PATCH", f"/git/refs/{ALLOWED_REF_PATH}", {"sha": commit_sha, "force": False})
        if not 200 <= status < 300:
            return Correction(Refuse(policy.BRANCH_MOVED), tree_body, commit_body, tree.head_sha)
        return Correction(verdict, tree_body, commit_body, tree.head_sha, commit_sha)

def _document(kind: str, proposal: Mapping[str, Any], payload: Mapping[str, Any]) -> Document:
    namespace, canary, template_hash = (str(proposal.get(field) or "")
                                        for field in ("namespace", "canary_name", "template_hash"))
    return Document(
        kind=kind, key=make_key_parts(namespace, canary, template_hash, kind),
        labels={"flagger-recovery/canary": canary_label(namespace, canary),
                "flagger-recovery/template-hash": label_value(template_hash or "none")},
        payload=payload)

def corrector(store: Any, writer: GitWriter, live_for: Callable[[str, str], Any], *, clock: Callable[[], str],
              lock: Optional[Callable[[], Any]] = None,
              enabled: Optional[bool] = None) -> Callable[..., Correction]:
    """Bind a store, a writer and a per-canary live view into the callable the receiver
    and the reconcile pass hand a proposal payload to. The order below is the whole of
    AC4 and the README explains it; ``lock`` is the seam for the design's Lease, a
    callable returning a context manager, no-op here because a Lease needs RBAC this
    story does not add."""

    def _correct(proposal: Mapping[str, Any]) -> Correction:
        if not (CORRECTIONS_ENABLED if enabled is None else enabled):
            return Correction(Refuse(policy.DISABLED))
        live = live_for(str(proposal.get("namespace") or ""), str(proposal.get("canary_name") or ""))
        verdict = evaluate(proposal, live)
        if not verdict:
            return Correction(verdict)
        marker = {"status": policy.STATUS_IN_PROGRESS, "started_at": clock(), "ref": ALLOWED_REF,
                  "target_path": verdict.target_path, "restore_sha": verdict.restore_sha,
                  "failed_source_sha": str(proposal.get("failed_source_sha") or "")}
        claimed = writer.dry_run or store.put_document(_document(policy.KIND_CORRECTION, proposal, marker))
        if claimed is PutResult.DUPLICATE:
            return Correction(Refuse(policy.ALREADY_CORRECTED))
        with (lock or contextlib.nullcontext)():
            result = writer.correct(proposal, live)
        if result.commit_sha:
            store.put_document(_document(policy.KIND_CORRECTION_RESULT, proposal, {
                "status": policy.STATUS_WRITTEN, "finished_at": clock(), "ref": ALLOWED_REF,
                "target_path": result.verdict.target_path, "restore_sha": result.verdict.restore_sha,
                "old_source_sha": result.head_sha, "new_source_sha": result.commit_sha,
                "failed_source_sha": str(proposal.get("failed_source_sha") or "")}))
        LOG.info("correction for %r: %s", proposal.get("template_hash"),
                 result.verdict.reason or result.commit_sha or "dry-run")
        return result

    return _correct
