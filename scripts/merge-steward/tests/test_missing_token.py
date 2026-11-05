"""A missing app token is reported plainly: the advisory step skips, the verdict step fails."""

import pytest

import steward


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "read-only")
    monkeypatch.delenv("STEWARD_APP_TOKEN", raising=False)

    class NoCalls:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"unexpected GitHub call: {name}")

    monkeypatch.setattr(steward, "GitHub", NoCalls)


@pytest.mark.parametrize("value", [None, ""])
def test_the_advisory_step_skips_without_a_token(env, monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv("STEWARD_APP_TOKEN", value)
    assert steward.main(["--pr", "7", "--advisory-only", "--review-dir", "x"]) == 0
    out = capsys.readouterr()
    assert "STEWARD_APP_TOKEN is not set" in out.out and "KeyError" not in out.err


@pytest.mark.parametrize("value", [None, ""])
def test_the_verdict_step_fails_clearly_without_a_token(env, monkeypatch, capsys, value):
    if value is not None:
        monkeypatch.setenv("STEWARD_APP_TOKEN", value)
    assert steward.main(["--pr", "7"]) == 1
    assert "STEWARD_APP_TOKEN is not set" in capsys.readouterr().err


def test_a_dry_run_needs_no_token(env, monkeypatch):
    class Reads:
        def __init__(self, *a, **k):
            pass

        def get(self, path):
            raise steward.GitHubError(404, path)

    monkeypatch.setattr(steward, "GitHub", Reads)
    with pytest.raises(steward.GitHubError):  # got as far as reading the PR: no token check in the way
        steward.main(["--pr", "7", "--dry-run", "--advisory-only"])
