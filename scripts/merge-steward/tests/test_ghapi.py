"""Rate limits: retries, conditional requests, the app token for reads, and what is logged."""

import email.message
import io
import json
import urllib.error

import pytest

import ghapi
from ghapi import GitHub, GitHubError, SplitGitHub

TOKEN = "ghs_" + "T" * 36
NOW = 1_000_000


def headers_of(**pairs):
    message = email.message.Message()
    for name, value in pairs.items():
        message[name.replace("_", "-")] = str(value)
    return message


class Response:
    def __init__(self, body, **headers):
        self.body, self.headers = json.dumps(body).encode(), headers_of(**headers)

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def refusal(code, text="", **headers):
    return urllib.error.HTTPError("https://api.github.com/x", code, "refused", headers_of(**headers), io.BytesIO(text.encode()))


class Script:
    """Stands in for urlopen: each call takes the next scripted outcome and records the request."""

    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def client(monkeypatch):
    sleeps = []

    def make(*outcomes, budget=300):
        script = Script(*outcomes)
        monkeypatch.setattr("urllib.request.urlopen", script)
        gh = GitHub(TOKEN, "o/r", retry_budget=budget, sleep=sleeps.append, clock=lambda: NOW)
        return gh, script, sleeps

    return make


# Conditional requests.

def test_an_unchanged_get_is_answered_with_a_304_and_costs_nothing(client):
    gh, script, _ = client(Response({"a": 1}, ETag='"v1"'), urllib.error.HTTPError("u", 304, "same", headers_of(), None))
    first = gh.get("pulls/7")
    second = gh.get("pulls/7")
    assert first == second == {"a": 1}
    assert "If-None-Match" not in script.requests[0].headers and script.requests[1].get_header("If-none-match") == '"v1"'


def test_a_304_gives_a_fresh_copy_so_one_caller_cannot_change_what_the_next_sees(client):
    gh, _, _ = client(Response({"labels": []}, ETag='"v1"'), urllib.error.HTTPError("u", 304, "same", headers_of(), None))
    gh.get("pulls/7")["labels"].append("changed")
    assert gh.get("pulls/7") == {"labels": []}


def test_a_changed_resource_replaces_the_cached_copy(client):
    gh, script, _ = client(Response({"v": 1}, ETag='"v1"'), Response({"v": 2}, ETag='"v2"'),
                           urllib.error.HTTPError("u", 304, "same", headers_of(), None))
    assert (gh.get("pulls/7"), gh.get("pulls/7"), gh.get("pulls/7")) == ({"v": 1}, {"v": 2}, {"v": 2})
    assert script.requests[2].get_header("If-none-match") == '"v2"'


def test_pages_are_cached_by_url_and_keep_their_link_header(client):
    nxt = '<https://api.github.com/repos/o/r/pulls?page=2>; rel="next"'
    gh, script, _ = client(Response([1], ETag='"p1"', Link=nxt), Response([2], ETag='"p2"'),
                           urllib.error.HTTPError("u", 304, "same", headers_of(), None),
                           urllib.error.HTTPError("u", 304, "same", headers_of(), None))
    assert gh.paginate("pulls") == [1, 2] and gh.paginate("pulls") == [1, 2]
    assert len(script.requests) == 4


def test_writes_are_never_conditional_or_cached(client):
    gh, script, _ = client(Response({"id": 1}, ETag='"w"'), Response({"id": 2}, ETag='"w"'))
    gh.post("issues/7/comments", {"body": "x"})
    gh.post("issues/7/comments", {"body": "x"})
    assert len(script.requests) == 2 and all("If-none-match" not in r.headers for r in script.requests)


def test_a_response_without_an_etag_is_not_cached(client):
    gh, script, _ = client(Response({"a": 1}), Response({"a": 2}))
    assert (gh.get("x"), gh.get("x")) == ({"a": 1}, {"a": 2})


# Retries.

def test_a_429_is_retried_after_retry_after(client):
    gh, script, sleeps = client(refusal(429, retry_after=17), Response({"ok": True}))
    assert gh.get("pulls/7") == {"ok": True} and sleeps == [17] and len(script.requests) == 2


def test_a_secondary_limit_403_with_retry_after_is_retried(client):
    gh, _, sleeps = client(refusal(403, "You have exceeded a secondary rate limit.", retry_after=30), Response({"ok": 1}))
    assert gh.get("pulls/7") == {"ok": 1} and sleeps == [30]


def test_a_used_up_primary_limit_waits_until_the_reset(client):
    gh, _, sleeps = client(refusal(403, "API rate limit exceeded", x_ratelimit_remaining=0, x_ratelimit_reset=NOW + 42),
                           Response({"ok": 1}))
    assert gh.get("pulls/7") == {"ok": 1} and sleeps == [43]


def test_a_403_that_only_talks_about_a_rate_limit_backs_off(client):
    gh, _, sleeps = client(refusal(403, "You have exceeded a secondary rate limit"), Response({"ok": 1}))
    assert gh.get("x") == {"ok": 1} and sleeps == [5]


def test_the_backoff_grows_between_attempts(client):
    gh, _, sleeps = client(*[refusal(403, "rate limit") for _ in range(3)], Response({"ok": 1}))
    gh.get("x")
    assert sleeps == [5, 10, 20]


def test_a_permission_403_raises_at_once_and_is_not_retried(client):
    gh, script, sleeps = client(refusal(403, "Resource not accessible by integration", x_ratelimit_remaining=4000))
    with pytest.raises(GitHubError) as err:
        gh.get("pulls/7")
    assert err.value.status == 403 and err.value.permission_denied and not err.value.rate_limited
    assert sleeps == [] and len(script.requests) == 1


def test_a_403_with_nothing_to_wait_for_is_a_permission_failure(client):
    gh, script, sleeps = client(refusal(403))
    with pytest.raises(GitHubError) as err:
        gh.get("x")
    assert err.value.permission_denied and sleeps == [] and len(script.requests) == 1


@pytest.mark.parametrize("code", [400, 401, 404, 422, 500, 502])
def test_other_errors_raise_at_once_and_log_nothing(client, capsys, code):
    gh, script, sleeps = client(refusal(code))
    with pytest.raises(GitHubError) as err:
        gh.get("pulls/7")
    assert err.value.status == code and not err.value.rate_limited and sleeps == [] and len(script.requests) == 1
    assert capsys.readouterr().err == ""


def test_a_reset_too_far_away_is_not_waited_for(client):
    gh, script, sleeps = client(refusal(403, "rate limit", x_ratelimit_remaining=0, x_ratelimit_reset=NOW + 2400))
    with pytest.raises(GitHubError) as err:
        gh.get("x")
    assert err.value.rate_limited and sleeps == [] and len(script.requests) == 1


def test_waiting_stays_inside_the_budget(client):
    gh, script, sleeps = client(*[refusal(429, retry_after=100) for _ in range(5)], budget=250)
    with pytest.raises(GitHubError) as err:
        gh.get("x")
    assert err.value.rate_limited and sleeps == [100, 100] and sum(sleeps) <= 250 and len(script.requests) == 3


def test_the_budget_is_shared_by_every_request_of_the_client(client):
    gh, _, sleeps = client(refusal(429, retry_after=90), Response({}), refusal(429, retry_after=90), Response({}),
                           refusal(429, retry_after=90), budget=200)
    gh.get("a")
    gh.get("b")
    with pytest.raises(GitHubError):
        gh.get("c")
    assert sleeps == [90, 90]


def test_the_number_of_attempts_is_bounded(client):
    gh, script, sleeps = client(*[refusal(429, retry_after=1) for _ in range(20)], budget=10_000)
    with pytest.raises(GitHubError) as err:
        gh.get("x")
    assert err.value.rate_limited and len(script.requests) == ghapi.MAX_ATTEMPTS + 1


def test_a_nonsense_retry_after_falls_back_to_the_backoff(client):
    gh, _, sleeps = client(refusal(429, retry_after="soon"), Response({}))
    gh.get("x")
    assert sleeps == [5]


def test_writes_are_retried_too_after_a_rate_limit(client):
    gh, script, sleeps = client(refusal(403, "secondary rate limit", retry_after=3), Response({"id": 1}))
    assert gh.post("issues/7/comments", {"body": "x"}) == {"id": 1} and sleeps == [3]


def test_a_non_api_pagination_link_is_still_refused(client):
    gh, script, _ = client()
    with pytest.raises(GitHubError):
        gh.request("GET", "https://evil.example/x")
    assert script.requests == []


# What is logged.

def test_a_rate_limit_logs_the_headers_and_never_the_token(client, capsys):
    gh, _, _ = client(refusal(403, "rate limit", x_ratelimit_remaining=0, x_ratelimit_resource="core",
                              x_ratelimit_reset=NOW + 9, retry_after=9), Response({}))
    gh.get("pulls/7?per_page=100")
    err = capsys.readouterr().err
    assert "status=403" in err and "resource=core" in err.replace("x-ratelimit-resource=", "resource=")
    assert "x-ratelimit-remaining=0" in err and "retry-after=9" in err and "kind=rate-limit" in err
    assert "path=pulls/7" in err and "per_page" not in err and TOKEN not in err and "Bearer" not in err


def test_a_permission_refusal_is_logged_as_one(client, capsys):
    gh, _, _ = client(refusal(403, "Resource not accessible", x_ratelimit_resource="core", x_ratelimit_remaining=4999))
    with pytest.raises(GitHubError):
        gh.get("x")
    err = capsys.readouterr().err
    assert "kind=permission" in err and "x-ratelimit-remaining=4999" in err and TOKEN not in err


def test_nothing_is_logged_when_a_request_succeeds(client, capsys):
    gh, _, _ = client(Response({}, ETag='"a"'), urllib.error.HTTPError("u", 304, "same", headers_of(), None))
    gh.get("x")
    gh.get("x")
    assert capsys.readouterr().err == ""


def test_the_error_message_has_no_response_body_or_headers(client):
    gh, _, _ = client(refusal(403, "secret-looking body ghp_" + "a" * 36))
    with pytest.raises(GitHubError) as err:
        gh.get("pulls/7")
    assert "ghp_" not in str(err.value) and str(err.value) == "GitHub API returned 403 for pulls/7"


# The app token for reads.

class Recorder:
    repo, api = "o/r", "https://api.github.com"

    def __init__(self, name, log, fail=None):
        self.name, self.log, self.fail = name, log, fail

    def _do(self, method, path):
        self.log.append((self.name, method, path))
        if self.fail:
            raise self.fail
        return [] if method == "paginate" else {}

    def get(self, path):
        return self._do("get", path)

    def paginate(self, path, key=None):
        return self._do("paginate", path)

    def request(self, method, path, body=None):
        return self._do("request", path)

    def post(self, path, body):
        return self._do("post", path)

    def patch(self, path, body):
        return self._do("patch", path)

    def delete(self, path):
        return self._do("delete", path)


SHA = "a" * 40


def test_reads_the_app_can_make_use_the_app_and_everything_else_the_workflow_token():
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log))
    gh.get("pulls/7")
    gh.paginate("pulls/7/files")
    gh.paginate("pulls?state=open")
    gh.paginate(f"commits/{SHA}/check-runs?filter=latest", key="check_runs")
    gh.paginate("issues/7/comments")
    gh.paginate("issues/7/timeline")
    gh.paginate(f"commits/{SHA}/status", key="statuses")
    gh.get(f"commits/{SHA}")
    gh.get(f"commits/{SHA}/pulls")
    gh.get("contents/kubernetes/a.yaml?ref=" + SHA)
    gh.paginate("activity?ref=refs%2Fheads%2Fx")
    gh.get("collaborators/someone/permission")
    assert [entry[0] for entry in log] == ["app"] * 6 + ["workflow"] * 6


def test_every_write_stays_on_the_workflow_token():
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log))
    gh.post("issues/7/labels", {"labels": ["x"]})
    gh.delete("issues/7/labels/x")
    gh.patch("issues/comments/1", {"body": "x"})
    gh.request("POST", "labels", {})
    assert {entry[0] for entry in log} == {"workflow"}


def test_without_an_app_client_everything_is_the_workflow_token():
    log = []
    gh = SplitGitHub(Recorder("workflow", log), None)
    gh.get("pulls/7")
    gh.paginate(f"commits/{SHA}/check-runs")
    assert {entry[0] for entry in log} == {"workflow"}


def test_a_refused_permission_falls_back_once_and_is_remembered(capsys):
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log, fail=GitHubError(403, "pulls/7")))
    gh.get("pulls/7")
    gh.get("pulls/8")
    gh.get(f"commits/{SHA}/check-runs")
    assert log == [("app", "get", "pulls/7"), ("workflow", "get", "pulls/7"), ("workflow", "get", "pulls/8"),
                   ("app", "get", f"commits/{SHA}/check-runs"),
                   ("workflow", "get", f"commits/{SHA}/check-runs")]
    err = capsys.readouterr().err
    assert "app token refused for pulls reads" in err and "check-runs" in err


def test_a_bad_app_token_falls_back_too():
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log, fail=GitHubError(401, "pulls/7")))
    gh.get("pulls/7")
    assert [entry[0] for entry in log] == ["app", "workflow"]


def test_the_apps_own_rate_limit_falls_back_without_being_remembered():
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log, fail=GitHubError(403, "pulls/7", rate_limited=True)))
    gh.get("pulls/7")
    gh.get("pulls/8")
    assert [entry[0] for entry in log] == ["app", "workflow", "app", "workflow"]


@pytest.mark.parametrize("status", [404, 500, 422])
def test_other_app_errors_are_not_masked_by_a_fallback(status):
    log = []
    gh = SplitGitHub(Recorder("workflow", log), Recorder("app", log, fail=GitHubError(status, "pulls/7")))
    with pytest.raises(GitHubError) as err:
        gh.get("pulls/7")
    assert err.value.status == status and [entry[0] for entry in log] == ["app"]


def test_the_workflow_token_failing_after_a_fallback_still_raises():
    log = []
    base = Recorder("workflow", log, fail=GitHubError(403, "pulls/7"))
    gh = SplitGitHub(base, Recorder("app", log, fail=GitHubError(403, "pulls/7")))
    with pytest.raises(GitHubError) as err:
        gh.get("pulls/7")
    assert err.value.permission_denied


def test_read_classes_do_not_match_lookalike_paths():
    assert ghapi.read_class("pulls/7") == "pulls" and ghapi.read_class("pulls?state=open") == "pulls"
    assert ghapi.read_class(f"commits/{SHA}/check-runs?filter=all") == "check-runs"
    for other in ("pullsx", "contents/pulls/x", "commits/abc/check-runs", f"commits/{SHA}/status", "issues/7/labels",
                  "issues/x/comments", "repos/o/r/pulls"):
        assert ghapi.read_class(other) is None
