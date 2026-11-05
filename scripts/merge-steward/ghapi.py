"""Minimal GitHub REST client (stdlib only). Errors raise; callers fail closed.

Three things keep a burst of workflow runs from exhausting the rate limit and failing closed
for the wrong reason:

- A GET that was answered before is repeated with If-None-Match. A 304 does not count against
  the rate limit, so a poll that finds nothing changed is free.
- A 403 or 429 that is a rate limit is retried after the wait GitHub names (retry-after, or the
  x-ratelimit-reset time), within a budget so the job cannot outlive its timeout. A 403 that is
  a missing permission is never retried and raises at once, as before.
- On those two statuses, and only those, the rate-limit headers are printed. Never the token.
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request

# One wait, and all waiting in one process, in seconds; and how often a request is repeated.
MAX_SINGLE_WAIT, DEFAULT_BUDGET, MAX_ATTEMPTS = 120, 200, 5
CACHE_LIMIT = 512
# The headers worth printing about a refusal. Their values are numbers and a resource name.
_SHOWN = ("x-ratelimit-resource", "x-ratelimit-remaining", "retry-after")


class GitHubError(Exception):
    def __init__(self, status, path, rate_limited=False):
        super().__init__(f"GitHub API returned {status} for {path}")
        self.status = status
        self.rate_limited = rate_limited

    @property
    def permission_denied(self):
        """A 403 that is not a rate limit: the token may not do this."""
        return self.status == 403 and not self.rate_limited


def is_rate_limit(status, headers, body):
    """True for a refusal that waiting can fix: 429, or a 403 that names a limit.

    A 403 counts when it carries retry-after, says the primary limit is used up
    (x-ratelimit-remaining 0), or its message speaks of a rate limit (a secondary limit).
    Anything else is a missing permission.
    """
    if status == 429:
        return True
    if status != 403:
        return False
    return ("retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in (body or "").lower())


def wait_seconds(headers, attempt, now):
    """How long to wait before retrying a rate-limited request."""
    try:
        if "retry-after" in headers:
            return max(int(headers["retry-after"]), 1)
        if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
            return max(int(headers["x-ratelimit-reset"]) - int(now), 1) + 1
    except ValueError:
        pass
    return min(5 * 2 ** attempt, MAX_SINGLE_WAIT)


class GitHub:
    def __init__(self, token, repo, api="https://api.github.com", retry_budget=DEFAULT_BUDGET,
                 sleep=time.sleep, clock=time.time):
        self.token, self.repo, self.api = token, repo, api
        self.retry_budget, self.sleep, self.clock = retry_budget, sleep, clock
        self.cache = {}  # url -> (etag, raw body, link header), for GETs

    def _log_refusal(self, status, path, kind, headers, wait):
        shown = " ".join(f"{name}={headers.get(name, '-')}" for name in _SHOWN)
        print(f"github refusal: status={status} kind={kind} {shown} wait={wait}s path={path.split('?')[0]}",
              file=sys.stderr)

    def request(self, method, path, body=None):
        if path.startswith("https://"):
            if not path.startswith(self.api + "/"):  # only follow pagination links to the API host
                raise GitHubError("refused", "non-API URL")
            url = path
        else:
            url = f"{self.api}/repos/{self.repo}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(MAX_ATTEMPTS + 1):
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            cached = self.cache.get(url) if method == "GET" else None
            if cached:
                headers["If-None-Match"] = cached[0]
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    link = resp.headers.get("Link", "")
                    etag = resp.headers.get("ETag")
                if method == "GET" and etag:
                    if len(self.cache) >= CACHE_LIMIT:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[url] = (etag, raw, link)
                return (json.loads(raw) if raw else None), link
            except urllib.error.HTTPError as err:
                if err.code == 304 and cached:
                    return (json.loads(cached[1]) if cached[1] else None), cached[2]
                if err.code not in (403, 429):
                    raise GitHubError(err.code, path.split("?")[0]) from None
                seen = {k.lower(): v for k, v in err.headers.items()}
                text = err.read(2000).decode("utf-8", "replace") if err.fp else ""
                if not is_rate_limit(err.code, seen, text):
                    self._log_refusal(err.code, path, "permission", seen, 0)
                    raise GitHubError(err.code, path.split("?")[0]) from None
                wait = wait_seconds(seen, attempt, self.clock())
                give_up = attempt >= MAX_ATTEMPTS or wait > MAX_SINGLE_WAIT or wait > self.retry_budget
                self._log_refusal(err.code, path, "rate-limit", seen, 0 if give_up else wait)
                if give_up:
                    raise GitHubError(err.code, path.split("?")[0], rate_limited=True) from None
                self.retry_budget -= wait
                self.sleep(wait)

    def get(self, path):
        return self.request("GET", path)[0]

    def paginate(self, path, key=None):
        """Follow Link headers. key selects the list inside an object response."""
        sep = "&" if "?" in path else "?"
        url, items = f"{path}{sep}per_page=100", []
        while url:
            data, link = self.request("GET", url)
            items.extend(data[key] if key else data)
            url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
        return items

    def post(self, path, body):
        return self.request("POST", path, body)[0]

    def patch(self, path, body):
        return self.request("PATCH", path, body)[0]

    def delete(self, path):
        return self.request("DELETE", path)[0]


# Reads the steward app's token is asked to make first. Everything else, and every write, goes
# with the workflow token. The app token is minted with pull-requests and checks only, so these
# are the reads those two permissions cover. An endpoint the app cannot read falls back to the
# workflow token and is not tried again in this process.
_APP_READS = (
    ("pulls", re.compile(r"^pulls(?:[/?]|$)")),
    ("check-runs", re.compile(r"^commits/[0-9a-f]{40}/check-runs(?:[/?]|$)")),
    ("issue-comments", re.compile(r"^issues/[0-9]+/(?:comments|timeline)(?:[/?]|$)")),
)


def read_class(path):
    """Which group of reads a repository path belongs to for the app token, or None."""
    return next((name for name, pattern in _APP_READS if pattern.match(path)), None)


class SplitGitHub:
    """Reads on the steward app's own rate limit where its permissions allow; writes on base.

    base is the workflow token's client; it makes every write, so labels stay on the workflow
    token. reader is the app token's client, or None (a dry run), which makes this a plain base.
    """

    def __init__(self, base, reader=None):
        self.base, self.reader = base, reader
        self.repo, self.api = base.repo, base.api
        self.fallen_back = set()  # read classes the app token was refused

    def _read(self, method, path, *args):
        group = read_class(path)
        if self.reader is not None and group and group not in self.fallen_back:
            try:
                return getattr(self.reader, method)(path, *args)
            except GitHubError as err:
                # Only a refusal of the token itself falls back: a missing permission, a bad
                # token, or the app's own rate limit. A 404 or a 5xx means the same to both.
                if not (err.permission_denied or err.status == 401 or err.rate_limited):
                    raise
                if not err.rate_limited:
                    self.fallen_back.add(group)
                print(f"app token refused for {group} reads ({err.status}); using the workflow token",
                      file=sys.stderr)
        return getattr(self.base, method)(path, *args)

    def get(self, path):
        return self._read("get", path)

    def paginate(self, path, key=None):
        return self._read("paginate", path, key)

    def request(self, method, path, body=None):
        return self.base.request(method, path, body)

    def post(self, path, body):
        return self.base.post(path, body)

    def patch(self, path, body):
        return self.base.patch(path, body)

    def delete(self, path):
        return self.base.delete(path)
