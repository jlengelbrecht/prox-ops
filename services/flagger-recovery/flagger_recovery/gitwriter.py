"""The Git correction writer — the one component that will ever hold write
authority over this repository, so it is written to be read as if it did.

Stdlib only, over ``record``'s urllib transport, which follows no redirect, so the
token is never re-sent to a host GitHub named. No shell and no external program is
anywhere in the path, which the AST gate in ``tests/test_policy.py`` enforces, and
the credential arrives as a zero-argument callable rather than from disk or the
environment, so no token is ever an attribute of this object. Reads before writes,
refusals before both: the pure bounds, the reads, the bounds again against a freshly
read cluster, the claim, and only then the three mutating calls. The README has the
refusal set, the claim's placement and what a refusal leaves behind.
"""

from __future__ import annotations

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

API_HOST = "api.github.com"  # the only host this writer may ever present the token to
DEFAULT_BASE_URL = f"https://{API_HOST}"
# ``compare`` pages its file list and GitHub caps it at 300; past that it is a
# truncation, which every caller here fails closed on.
COMPARE_PER_PAGE, COMPARE_PAGES = 100, 3
USER_AGENT, API_VERSION = "flagger-recovery", "2022-11-28"
FILE_MODE = "100644"  # a YAML manifest, never executable; a tree entry needs a mode
AUTHOR = {"name": "flagger-recovery", "email": "flagger-recovery@users.noreply.github.com"}

# The module-level switch, and the outer bound: ``corrector(enabled=)`` narrows it and
# cannot widen it. FRP-007b turns it on, with the credential and the Lease.
CORRECTIONS_ENABLED = False

def _failed(method: str, path: str, status: int, raw: bytes) -> ApiWriteError:
    # Carrying what GitHub answered: its own error document, which holds nothing of
    # the request that produced it and so never the token.
    return ApiWriteError(method, path, status, raw[:512])

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
        # Parsed, not prefix-matched: "https://api.github.com@attacker.example" starts
        # with the right characters, resolves to the wrong host, and takes the token there.
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != "https" or parsed.hostname != API_HOST or parsed.port is not None
                or parsed.username is not None or parsed.password is not None or parsed.query
                or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError(f"GitWriter requires an https://{API_HOST} base_url (got {base_url!r})")
        self._token = token
        self._repository = repository
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.dry_run = dry_run
        self._transport = transport or _Transport()
        self.requests = self.mutations = 0

    def _call(self, method: str, path: str,
              payload: Optional[Mapping[str, Any]] = None) -> tuple[int, Any, bytes]:
        """``(status, decoded, raw)``; the raw body so a failure can say what GitHub said."""
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
            return status, (json.loads(raw) if raw else {}), raw
        except ValueError:
            return status, {}, raw

    def _read(self, path: str) -> Mapping[str, Any]:
        status, payload, raw = self._call("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise _failed("GET", path, status, raw)
        return payload

    def read_ref(self) -> str:  # the branch head commit sha
        return str(self._read(f"/git/ref/{ALLOWED_REF_PATH}").get("object", {}).get("sha") or "")

    def read_commit(self, sha: str) -> str:  # a commit's tree sha
        return str(self._read(f"/git/commits/{urllib.parse.quote(sha, safe='')}").get("tree", {}).get("sha") or "")

    def read_blob_sha(self, ref: str, path: str) -> str:
        """The blob sha of ``path`` at ``ref``; empty when absent or not a file, which
        refuses upstream and never licenses a create."""
        route = f"/contents/{urllib.parse.quote(path)}?{urllib.parse.urlencode({'ref': ref})}"
        status, payload, raw = self._call("GET", route)
        if status == 404:
            return ""
        if status != 200:
            raise _failed("GET", route, status, raw)
        # A directory answers a JSON array; a symlink or a submodule answers a type that
        # is not "file". None is a blob to install, and this filter is the whole reason
        # restating mode 100644 cannot be a silent type change.
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return ""
        return str(payload.get("sha") or "")

    def compare(self, base: str, head: str) -> tuple[str, tuple[str, ...], bool]:
        """``(status, changed paths, complete)``: GitHub's own identical/ahead/behind/
        diverged, ``""`` when it cannot compare the two at all, and whether the file
        list came back whole — a truncated one is refused, never worked around."""
        quote = urllib.parse.quote
        route = f"/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        state, files, complete = "", [], False
        for page in range(1, COMPARE_PAGES + 1):
            status, payload, raw = self._call("GET", f"{route}?per_page={COMPARE_PER_PAGE}&page={page}")
            if status == 404:
                return "", (), True
            if status != 200 or not isinstance(payload, dict):
                raise _failed("GET", route, status, raw)
            state = str(payload.get("status") or "")
            batch = [str(entry.get("filename") or "") for entry in payload.get("files") or ()]
            files.extend(batch)
            reported = payload.get("total_files")
            complete = (len(batch) < COMPARE_PER_PAGE
                        and (not isinstance(reported, int) or len(files) >= reported))
            if complete:
                break
        return state, tuple(files), complete

    def snapshot(self, proposal: Mapping[str, Any], verdict: Verdict) -> TreeState:
        """Everything ``policy.evaluate``'s freshness bounds compare. All GETs."""
        failed = str(proposal["failed_source_sha"])
        head_sha = self.read_ref()
        state, since, whole = ("identical", (), True) if head_sha == failed else self.compare(failed, head_sha)
        # The restore revision decides what bytes get committed, so place it on the
        # branch: the head is it, or descends from it. A revert asks the same of the
        # failed revision, keeping that comparison's status and not only its file list.
        on_branch, undone, undone_whole = head_sha == verdict.restore_sha, (), True
        if not on_branch and head_sha:
            on_branch = self.compare(verdict.restore_sha, head_sha)[0] in ("identical", "ahead")
        if verdict.form == policy.REVERT_COMMIT:
            undone_state, undone, undone_whole = self.compare(verdict.restore_sha, failed)
            on_branch = on_branch and undone_state in ("identical", "ahead")
        return TreeState(
            head_sha=head_sha,
            head_tree_sha=self.read_commit(head_sha) if head_sha else "",
            blob_at_head=self.read_blob_sha(head_sha, verdict.target_path) if head_sha else "",
            blob_at_failed=self.read_blob_sha(failed, verdict.target_path),
            blob_at_restore=self.read_blob_sha(verdict.restore_sha, verdict.target_path),
            head_is_descendant=state in ("identical", "ahead"), restore_on_branch=on_branch,
            paths_complete=whole and undone_whole, paths_since_failed=since, paths_undone=undone)

    def _create(self, path: str, body: Mapping[str, Any]) -> str:
        status, payload, raw = self._call("POST", path, body)
        if status not in (200, 201):
            raise _failed("POST", path, status, raw)
        # A 2xx with no sha is a create whose result cannot be used; passing an empty
        # tree sha on would earn a confusing 422 from the next call instead.
        sha = str(payload.get("sha") or "") if isinstance(payload, dict) else ""
        if not sha:
            raise _failed("POST", path, status, raw)
        return sha

    def correct(self, proposal: Mapping[str, Any], live_for: Callable[[], Any], *,
                claim: Optional[Callable[[Verdict], bool]] = None,
                dry_run: Optional[bool] = None) -> Correction:
        """Evaluate, read, re-evaluate, claim, write. Safe to call directly: the bounds
        are re-checked here, never assumed from the caller. ``live_for`` builds a live
        view, once per ``evaluate`` pass — a view memoises its reads, so sharing one would
        have the second pass re-check freshness against the snapshot the first took, which
        is no re-read at all. ``claim`` is called with the passing verdict immediately
        before the first mutating call and refuses ``already-corrected`` when it answers
        False. ``dry_run`` is read once by the caller, not off the attribute twice."""
        if self._repository != ALLOWED_REPOSITORY:
            return Correction(Refuse(policy.WRONG_REPOSITORY))
        dry = self.dry_run if dry_run is None else dry_run
        verdict = evaluate(proposal, live_for())
        if not verdict:
            return Correction(verdict)
        tree = self.snapshot(proposal, verdict)
        verdict = evaluate(proposal, live_for(), tree)
        if not verdict:
            return Correction(verdict, head_sha=tree.head_sha)
        tree_body = {"base_tree": tree.head_tree_sha, "tree": [
            {"path": verdict.target_path, "mode": FILE_MODE, "type": "blob", "sha": verdict.blob_sha}]}
        commit_body = {"message": policy.commit_message(proposal, verdict), "tree": "",
                       "parents": [tree.head_sha], "author": AUTHOR, "committer": AUTHOR}
        if dry:
            return Correction(verdict, tree_body, commit_body, tree.head_sha, dry_run=True)
        if claim is not None and not claim(verdict):
            return Correction(Refuse(policy.ALREADY_CORRECTED), head_sha=tree.head_sha)
        commit_body = {**commit_body, "tree": self._create("/git/trees", tree_body)}
        commit_sha = self._create("/git/commits", commit_body)
        # Never force. A non-2xx here is the non-fast-forward this design is built
        # around: the branch moved under us, so refuse and re-evaluate from scratch
        # next time rather than retry against a stale parent.
        status = self._call("PATCH", f"/git/refs/{ALLOWED_REF_PATH}", {"sha": commit_sha, "force": False})[0]
        if not 200 <= status < 300:
            return Correction(Refuse(policy.BRANCH_MOVED), tree_body, commit_body, tree.head_sha)
        return Correction(verdict, tree_body, commit_body, tree.head_sha, commit_sha)

def _parts(proposal: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(proposal.get(field) or "") for field in ("namespace", "canary_name", "template_hash"))

def _key(kind: str, proposal: Mapping[str, Any]) -> str:
    return make_key_parts(*_parts(proposal), kind)

def _document(kind: str, proposal: Mapping[str, Any], payload: Mapping[str, Any]) -> Document:
    namespace, canary, template_hash = _parts(proposal)
    return Document(
        kind=kind, key=_key(kind, proposal),
        labels={"flagger-recovery/canary": canary_label(namespace, canary),
                "flagger-recovery/template-hash": label_value(template_hash or "none")},
        payload=payload)

def corrector(store: Any, writer: GitWriter, live_for: Callable[[str, str], Any], *, clock: Callable[[], str],
              lock: Callable[[], Any], enabled: Optional[bool] = None) -> Callable[..., Correction]:
    """Bind a store, a writer and a per-canary live view into the callable the receiver
    and the reconcile pass hand a proposal payload to. The order below is the whole of
    AC4 and the README explains it. ``lock`` is the design's Lease — a callable returning
    a context manager, and required: a mutual exclusion a caller can forget into a no-op
    is indistinguishable from one nobody configured. ``enabled`` narrows the switch."""

    def _correct(proposal: Mapping[str, Any]) -> Correction:
        if not (CORRECTIONS_ENABLED and (True if enabled is None else enabled)):
            return Correction(Refuse(policy.DISABLED))
        # An already-claimed proposal is answered before any GitHub call. The claim is
        # written below, once every read-side refusal has passed and the first mutating
        # call is next, so a transient refusal leaves no marker to suppress a retry.
        if store.get_document(policy.KIND_CORRECTION, _key(policy.KIND_CORRECTION, proposal)) is not None:
            return Correction(Refuse(policy.ALREADY_CORRECTED))
        dry_run = writer.dry_run

        def live() -> Any:
            return live_for(str(proposal.get("namespace") or ""), str(proposal.get("canary_name") or ""))

        def claim(verdict: Verdict) -> bool:
            marker = {"status": policy.STATUS_IN_PROGRESS, "started_at": clock(), "ref": ALLOWED_REF,
                      "target_path": verdict.target_path, "restore_sha": verdict.restore_sha,
                      "failed_source_sha": str(proposal.get("failed_source_sha") or "")}
            return store.put_document(
                _document(policy.KIND_CORRECTION, proposal, marker)) is not PutResult.DUPLICATE

        with lock():
            result = writer.correct(proposal, live, claim=claim, dry_run=dry_run)
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
