"""Minimal GitHub REST client (stdlib only). Errors raise; callers fail closed."""

import json
import urllib.error
import urllib.request


class GitHubError(Exception):
    def __init__(self, status, path):
        super().__init__(f"GitHub API returned {status} for {path}")
        self.status = status


class GitHub:
    def __init__(self, token, repo, api="https://api.github.com"):
        self.token, self.repo, self.api = token, repo, api

    def request(self, method, path, body=None):
        if path.startswith("https://"):
            if not path.startswith(self.api + "/"):  # only follow pagination links to the API host
                raise GitHubError("refused", "non-API URL")
            url = path
        else:
            url = f"{self.api}/repos/{self.repo}/{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None, resp.headers.get("Link", "")
        except urllib.error.HTTPError as err:
            raise GitHubError(err.code, path.split("?")[0]) from None

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
