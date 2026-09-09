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
import re
import urllib.parse
from typing import Any, Callable, Mapping, Optional

from . import policy
from .policy import ALLOWED_REF, ALLOWED_REF_PATH, ALLOWED_REPOSITORY, Refuse, TreeState, Verdict, evaluate
from .record import ApiWriteError, Document, PutResult, Transport, canary_label, label_value, make_key_parts
from .record import _UrllibTransport as _Transport

LOG = logging.getLogger("flagger_recovery.gitwriter")

API_HOST = "api.github.com"  # the only host this writer may ever present the token to
DEFAULT_BASE_URL = f"https://{API_HOST}"
# ``compare`` returns its files once and caps them at 300 for the whole comparison, with no
# file total: a list at the cap cannot be told from a truncated one, so it is not complete.
COMPARE_FILE_CAP = 300
USER_AGENT, API_VERSION = "flagger-recovery", "2022-11-28"
FILE_MODE = "100644"  # a YAML manifest, never executable; a tree entry needs a mode
AUTHOR = {"name": "flagger-recovery", "email": "flagger-recovery@users.noreply.github.com"}

# The module-level switch, and the outer bound: ``corrector(enabled=)`` narrows it and
# cannot widen it. FRP-007b turns it on, with the credential and the Lease.
CORRECTIONS_ENABLED = False

# ``owner/name``, GitHub's own alphabet: a repository is interpolated into every request
# path, and ``correct()`` is only one of the entry points that builds one.
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

class _StaleLiveView(Exception):
    """``live_for`` answered with the same view twice, so the second ``evaluate`` pass
    would re-check freshness against the first's memoised reads — no re-read at all.
    Raised before anything is claimed or written."""

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
        # Validated here, not only in ``correct()``: the read helpers are public and
        # would otherwise build ``/repos/<anything>`` out of a constructor argument.
        if not _REPOSITORY_RE.match(repository):
            raise ValueError(f"GitWriter requires an owner/name repository (got {repository!r})")
        self._token = token
        self._repository = repository
        self._repository_path = "/".join(
            urllib.parse.quote(segment, safe="") for segment in repository.split("/"))
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self.dry_run = dry_run
        self._transport = transport or _Transport()
        self.requests = self.mutations = 0

    def _call(self, method: str, path: str,
              payload: Optional[Mapping[str, Any]] = None) -> tuple[int, Any, bytes]:
        """``(status, decoded, raw)``; the raw body so a failure can say what GitHub said."""
        headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
                   "X-GitHub-Api-Version": API_VERSION}
        # No credential, no header. Between correction windows the token Secret is not in
        # the cluster, and a ``Bearer `` with nothing after it is a 401 rather than the
        # anonymous read of a public repository that a dry run then runs on.
        credential = self._token()
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        body = None
        if payload is not None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
            self.mutations += 1
        self.requests += 1
        status, raw = self._transport.request(
            method, f"{self._base_url}/repos/{self._repository_path}{path}",
            headers=headers, body=body, ca_file=None, timeout=self._timeout)
        try:
            return status, (json.loads(raw) if raw else {}), raw
        except ValueError:
            return status, {}, raw

    def _failed(self, method: str, path: str, status: int, raw: bytes) -> ApiWriteError:
        # Carrying what GitHub answered: its own error document, which holds nothing of
        # the request that produced it and so never the token. The full URL is never
        # logged; ``server.queued_corrector`` only parses it to tell a GitHub read
        # apart from a GitHub write or a Kubernetes call.
        url = f"{self._base_url}/repos/{self._repository_path}{path}"
        return ApiWriteError(method, url, status, raw[:512])

    def _read(self, path: str) -> Mapping[str, Any]:
        status, payload, raw = self._call("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise self._failed("GET", path, status, raw)
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
            raise self._failed("GET", route, status, raw)
        # A directory answers a JSON array; a symlink or a submodule answers a type that
        # is not "file". None is a blob to install, and this filter is the whole reason
        # restating mode 100644 cannot be a silent type change.
        if not isinstance(payload, dict) or payload.get("type") != "file":
            return ""
        return str(payload.get("sha") or "")

    def compare(self, base: str, head: str) -> tuple[str, tuple[str, ...], bool]:
        """``(status, changed paths, complete)``: GitHub's own identical/ahead/behind/
        diverged, ``""`` when it cannot compare the two at all, and whether the file
        list came back whole — a truncated one is refused, never worked around. One
        request: paging this endpoint pages the *commits*, which no bound here reads,
        while the files come back once and stop at ``COMPARE_FILE_CAP``. A list that
        reaches the cap is therefore not complete, and refusing a comparison that is
        genuinely that wide is the fail-closed answer."""
        quote = urllib.parse.quote
        route = f"/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        status, payload, raw = self._call("GET", route)
        if status == 404:
            return "", (), True
        if status != 200 or not isinstance(payload, dict):
            raise self._failed("GET", route, status, raw)
        files = tuple(str(entry.get("filename") or "") for entry in payload.get("files") or ())
        return str(payload.get("status") or ""), files, len(files) < COMPARE_FILE_CAP

    def snapshot(self, proposal: Mapping[str, Any], verdict: Verdict) -> TreeState:
        """Everything ``policy.evaluate``'s freshness bounds compare. All GETs."""
        failed = str(proposal["failed_source_sha"])
        head_sha, restore = self.read_ref(), verdict.restore_sha
        state, since, whole = ("identical", (), True) if head_sha == failed else self.compare(failed, head_sha)
        # The failed release's own scope, which the proposal could not read and which
        # ``evaluate`` bounds to the correction's one target. Both forms restore the
        # promoted revision, so this one comparison is that release's whole diff — and
        # it is also the restore revision's on-branch bound, the committed bytes being
        # that revision's, so an object GitHub serves but cannot place in this branch's
        # history fails here. Truncated at the cap it proves neither, and ``evaluate``
        # refuses, the same fail-closed answer the file list since the failure gets.
        undone_state, undone, undone_whole = self.compare(restore, failed)
        if head_sha in ("", restore):
            on_branch = head_sha == restore
        elif head_sha == failed:
            on_branch = undone_state in ("identical", "ahead")  # the same request, just made
        else:
            on_branch = self.compare(restore, head_sha)[0] in ("identical", "ahead")
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
            raise self._failed("POST", path, status, raw)
        # A 2xx with no sha is a create whose result cannot be used; passing an empty
        # tree sha on would earn a confusing 422 from the next call instead.
        sha = str(payload.get("sha") or "") if isinstance(payload, dict) else ""
        if not sha:
            raise self._failed("POST", path, status, raw)
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
        # Resolved now, not at process start: the ExternalSecret can sync after this pod
        # started, and the next attempt must not require a restart to pick it up.
        if not self._token():
            LOG.warning("correction for %r refused: no credential is readable right now",
                        proposal.get("template_hash"))
            return Correction(Refuse(policy.CREDENTIAL_UNAVAILABLE), tree_body, commit_body, tree.head_sha)
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

        views: list[Any] = []  # enforced, not documented: see _StaleLiveView

        def live() -> Any:
            view = live_for(str(proposal.get("namespace") or ""), str(proposal.get("canary_name") or ""))
            if views and view is views[-1]:
                raise _StaleLiveView()
            views.append(view)
            return view

        def claim(verdict: Verdict) -> bool:
            marker = {"status": policy.STATUS_IN_PROGRESS, "started_at": clock(), "ref": ALLOWED_REF,
                      "target_path": verdict.target_path, "restore_sha": verdict.restore_sha,
                      "failed_source_sha": str(proposal.get("failed_source_sha") or "")}
            return store.put_document(
                _document(policy.KIND_CORRECTION, proposal, marker)) is not PutResult.DUPLICATE

        try:
            with lock():
                result = writer.correct(proposal, live, claim=claim, dry_run=dry_run)
        except policy.LockUnavailable as exc:
            # Somebody else is writing. Nothing was read from GitHub and nothing was
            # claimed, so the next delivery — or the reconcile pass — retries cleanly.
            LOG.warning("correction for %r refused: %s", proposal.get("template_hash"), exc)
            return Correction(Refuse(policy.LEASE_HELD))
        except _StaleLiveView:
            LOG.error("correction for %r refused: the live view was not fresh — live_for "
                      "answered with the same object twice", proposal.get("template_hash"))
            return Correction(Refuse(policy.SUPERSEDED))
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
