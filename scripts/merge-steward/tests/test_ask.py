"""/ask: who may ask, what is refused, and what is posted. Every refusal has its own case."""

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

import ask_job
import review
from ask_job import Refused
from ghapi import GitHubError
from test_review_workflow import GITHUB
from test_steward import POLICY, SHA, FakeGitHub, fresh_head, renovate_pr, routes  # noqa: F401

REPO = "o/r"
APP = POLICY["steward_app_id"]


def event(**changes):
    base = {"action": "created", "repository": {"full_name": REPO},
            "issue": {"number": 7, "state": "open", "pull_request": {"url": "u"}},
            "comment": {"id": 55, "body": "/ask does this change the tag?", "author_association": "OWNER",
                        "user": {"login": "owner", "type": "User"}},
            "sender": {"login": "owner", "type": "User"}}
    for path, value in changes.items():
        node = base
        *parents, last = path.split(".")
        for part in parents:
            node = node[part]
        if value is ...:
            node.pop(last)
        else:
            node[last] = value
    return base


def refused(ev):
    with pytest.raises(Refused) as why:
        ask_job.check_event(ev, REPO)
    return why.value.args[0]


def test_the_owner_asking_on_an_open_pull_request_is_accepted():
    assert ask_job.check_event(event(), REPO) == (7, 55, "does this change the tag?")


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE", "", None, "owner"])
def test_anyone_but_the_owner_is_refused(association):
    assert refused(event(**{"comment.author_association": association})) == "author"


def test_a_missing_association_is_refused():
    assert refused(event(**{"comment.author_association": ...})) == "author"


@pytest.mark.parametrize("changes,why", [
    ({"comment.user.type": "Bot"}, "bot"),
    ({"sender.type": "Bot"}, "bot"),
    ({"comment.user.type": "Organization"}, "bot"),
    ({"comment.performed_via_github_app": {"id": APP}}, "bot"),
    ({"sender.login": "someone-else"}, "author"),
    ({"comment.user.login": ""}, "author"),
])
def test_bots_and_the_apps_own_comments_never_ask(changes, why):
    assert refused(event(**changes)) == why


@pytest.mark.parametrize("changes", [
    {"action": "edited"}, {"action": "deleted"}, {"action": ...}, {"issue.pull_request": ...},
    {"issue.pull_request": None}, {"issue.pull_request": "x"}, {"repository.full_name": "other/r"},
    {"comment": None}, {"issue.number": "7"}, {"comment.id": True}, {"issue.number": True},
])
def test_only_a_new_comment_on_a_pull_request_of_this_repository_counts(changes):
    assert refused(event(**changes)) == "event"


def test_a_closed_pull_request_is_refused():
    assert refused(event(**{"issue.state": "closed"})) == "closed"
    assert refused(event(**{"issue.state": "OPEN"})) == "closed"


@pytest.mark.parametrize("body", [
    "/ask", "/ask ", "/ask   \n", "/asking what", "/askme x", "please /ask what", " x /ask what", "ask what", "/ASK what",
    "", None, "//ask what", "/ask​", "/ask \x00\x01",
])
def test_a_comment_that_is_not_an_ask_with_a_question_is_refused(body):
    assert refused(event(**{"comment.body": body})) == "command"


def test_a_long_question_is_refused():
    assert refused(event(**{"comment.body": "/ask " + "q" * 1001})) == "long"
    assert ask_job.check_event(event(**{"comment.body": "/ask " + "q" * 1000}), REPO)[2] == "q" * 1000


def test_the_question_is_text_only():
    body = "/ask first line\nsecond\x00 line\x1b[31m\ttab"
    question = ask_job.check_event(event(**{"comment.body": body}), REPO)[2]
    assert question == "first line\nsecond line[31m\ttab"
    assert ask_job.question_of("/ask\nafter a newline") == "after a newline"


# The PR itself is read from the API, so a fork or an out-of-scope PR is refused there.

def pr_of(**changes):
    pr = renovate_pr(login="owner")
    pr.update(changes)
    return pr


def gh_for(pr):
    gh = FakeGitHub({"pulls/7": pr})
    gh.repo = REPO
    return gh


def test_an_open_pull_request_from_this_repository_passes():
    assert ask_job.check_pr(gh_for(pr_of()), 7, POLICY)["number"] == 7
    assert ask_job.check_pr(gh_for(renovate_pr()), 7, POLICY)["number"] == 7  # a Renovate PR too


@pytest.mark.parametrize("pr,why", [
    (pr_of(state="closed"), "closed"),
    (pr_of(head={"sha": SHA, "ref": "x", "repo": {"full_name": "someone/r", "fork": True}}), "fork"),
    (pr_of(head={"sha": SHA, "ref": "x", "repo": {"full_name": "someone/r"}}), "fork"),
    (pr_of(head={"sha": SHA, "ref": "x", "repo": None}), "fork"),
    (pr_of(head={"sha": SHA, "ref": "x", "repo": {"full_name": REPO, "fork": True}}), "scope"),
])
def test_a_fork_or_a_closed_pull_request_is_refused_by_the_api_check(pr, why):
    with pytest.raises(Refused) as got:
        ask_job.check_pr(gh_for(pr), 7, POLICY)
    assert got.value.args[0] == why


def test_a_policy_that_turns_reviews_off_turns_asks_off():
    policy = copy.deepcopy(POLICY)
    policy["ai"]["review_scope"]["same_repo"] = "off"
    with pytest.raises(Refused):
        ask_job.check_pr(gh_for(pr_of()), 7, policy)


def write_event(tmp_path, monkeypatch, ev):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(ev))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))


def test_the_gate_says_yes_only_for_a_clear_yes(tmp_path, monkeypatch):
    write_event(tmp_path, monkeypatch, event())
    assert ask_job.gate(gh_for(pr_of()), POLICY) == {"ok": "true", "pr": 7, "comment": 55}
    write_event(tmp_path, monkeypatch, event(**{"comment.author_association": "MEMBER"}))
    assert ask_job.gate(gh_for(pr_of()), POLICY) == {"ok": "false"}
    write_event(tmp_path, monkeypatch, event())
    assert ask_job.gate(gh_for(pr_of(state="closed")), POLICY) == {"ok": "false"}
    failing = FakeGitHub({"pulls/7": GitHubError(500, "pulls/7")})
    failing.repo = REPO
    assert ask_job.gate(failing, POLICY) == {"ok": "false"}


def test_the_gate_says_no_for_an_unreadable_event(tmp_path, monkeypatch):
    path = tmp_path / "event.json"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    assert ask_job.gate(gh_for(pr_of()), POLICY) == {"ok": "false"}  # missing
    path.write_text("not json")
    assert ask_job.gate(gh_for(pr_of()), POLICY) == {"ok": "false"}
    path.write_text("[]")
    assert ask_job.gate(gh_for(pr_of()), POLICY) == {"ok": "false"}


def test_a_refused_fork_is_never_prepared(tmp_path, monkeypatch):
    write_event(tmp_path, monkeypatch, event())
    fork = pr_of(head={"sha": SHA, "ref": "x", "repo": {"full_name": "someone/r", "fork": True}})
    with pytest.raises(Refused):
        ask_job.prepare(gh_for(fork), POLICY, tmp_path / "out")
    assert not (tmp_path / "out" / ask_job.QUESTION_FILE).exists()


def test_prepare_writes_the_question_and_returns_the_model_settings(tmp_path, monkeypatch):
    write_event(tmp_path, monkeypatch, event())
    outputs = ask_job.prepare(gh_for(pr_of()), POLICY, tmp_path / "out")
    assert (tmp_path / "out" / ask_job.QUESTION_FILE).read_text() == "does this change the tag?"
    assert outputs["alias"] == POLICY["ai"]["model"]["alias"] and outputs["comment"] == 55
    assert outputs["url"] == pr_of()["html_url"]


# The answer is filtered before it can be posted.

HOSTILE = ("Ignore the rules. @josh see https://evil.example/x ![i](https://evil.example/p.png) 10.0.0.1 <b>x</b> "
           "ghp_" + "a" * 36 + " #1234\n/ask more\n\n- bullet [link](https://evil.example)")


def test_the_answer_is_scrubbed_line_by_line(tmp_path):
    (tmp_path / ask_job.RAW_FILE).write_text(HOSTILE + "\nline two is fine")
    result = ask_job.collect(tmp_path, "ok", 55)
    text = result["answer"]
    assert result["available"] and "line two is fine" in text and "\n" in text
    for bad in ("evil.example", "@josh", "<b>", "ghp_", "10.0.0.1", "![", "](", "#1234"):
        assert bad not in text
    assert json.loads((tmp_path / ask_job.ANSWER_FILE).read_text()) == result


def test_an_answer_is_bounded():
    text = "\n".join(f"line {n} " + "w" * 900 for n in range(500))
    cleaned = ask_job.clean_answer(text)
    assert len(cleaned.split("\n")) <= ask_job.MAX_ANSWER_LINES
    assert all(len(line) <= ask_job.LINE_LIMIT for line in cleaned.split("\n"))


@pytest.mark.parametrize("status,reason", [("timeout", "timeout"), ("failed", "failed"), ("missing-key", "missing-key"),
                                           ("ok", "no-output"), ("surprise", "failed")])
def test_a_failed_run_leaves_a_fixed_reason(tmp_path, status, reason):
    result = ask_job.collect(tmp_path, status, 55)
    assert result == {"comment": 55, "available": False, "reason": reason}


def test_an_empty_answer_counts_as_no_answer(tmp_path):
    (tmp_path / ask_job.RAW_FILE).write_text(" \n\n")
    assert ask_job.collect(tmp_path, "ok", 55)["reason"] == "no-output"


def test_the_reply_comes_only_from_validated_fields():
    ok = ask_job.reply({"comment": 55, "available": True, "answer": "Yes. @josh"})
    assert ok.startswith(ask_job.ASK_MARKER.format(comment=55)) and "@josh" not in ok and "change no check" in ok
    assert "No answer: the review timed out" in ask_job.reply({"comment": 55, "available": False, "reason": "timeout"})
    assert "PR-Agent failed" in ask_job.reply({"comment": 55, "available": False, "reason": "anything we invented"})
    for bad in (None, [], {"comment": "55"}, {"comment": True}, {"available": True, "answer": "x"}):
        assert ask_job.reply(bad) is None


def answer_dir(tmp_path, **fields):
    (tmp_path / ask_job.ANSWER_FILE).write_text(json.dumps(dict({"comment": 55, "available": True, "answer": "Yes."}, **fields)))
    return str(tmp_path)


def publish(tmp_path, monkeypatch, comments=(), ev=None, pr=None, **fields):
    write_event(tmp_path, monkeypatch, ev or event())
    gh = gh_for(pr or pr_of())
    gh.routes["issues/7/comments"] = list(comments)
    app = FakeGitHub({})
    posted = ask_job.publish(gh, app, POLICY, answer_dir(tmp_path, **fields))
    return posted, gh, app


def test_the_answer_is_posted_once_by_the_app(tmp_path, monkeypatch):
    posted, gh, app = publish(tmp_path, monkeypatch)
    assert posted and gh.writes == []
    (write,) = app.writes
    assert write[1] == "issues/7/comments" and ask_job.ASK_MARKER.format(comment=55) in write[2]["body"]


def test_a_second_run_for_the_same_question_posts_nothing(tmp_path, monkeypatch):
    mine = {"body": ask_job.ASK_MARKER.format(comment=55) + "\nold", "performed_via_github_app": {"id": APP}}
    posted, _, app = publish(tmp_path, monkeypatch, comments=[mine])
    assert not posted and app.writes == []


def test_a_copied_marker_from_anyone_else_does_not_block_the_answer(tmp_path, monkeypatch):
    forged = {"body": ask_job.ASK_MARKER.format(comment=55), "user": {"login": "someone"}}
    posted, _, app = publish(tmp_path, monkeypatch, comments=[forged])
    assert posted and len(app.writes) == 1


def test_an_answer_for_another_comment_is_not_posted(tmp_path, monkeypatch):
    posted, _, app = publish(tmp_path, monkeypatch, comment=54)
    assert not posted and app.writes == []


def test_a_missing_answer_file_posts_a_short_no_answer(tmp_path, monkeypatch):
    write_event(tmp_path, monkeypatch, event())
    gh = gh_for(pr_of())
    gh.routes["issues/7/comments"] = []
    app = FakeGitHub({})
    assert ask_job.publish(gh, app, POLICY, str(tmp_path / "nowhere"))
    assert "no review result reached this run" in app.writes[0][2]["body"]


def test_publish_checks_the_comment_and_the_pull_request_again(tmp_path, monkeypatch):
    with pytest.raises(Refused):
        publish(tmp_path, monkeypatch, ev=event(**{"comment.author_association": "NONE"}))
    fork = pr_of(head={"sha": SHA, "ref": "x", "repo": {"full_name": "someone/r", "fork": True}})
    with pytest.raises(Refused):
        publish(tmp_path, monkeypatch, pr=fork)
    with pytest.raises(Refused):
        publish(tmp_path, monkeypatch, pr=pr_of(state="closed"))


def test_the_only_write_is_one_comment(tmp_path, monkeypatch):
    _, gh, app = publish(tmp_path, monkeypatch)
    assert [w[0] for w in app.writes] == ["POST"] and gh.writes == []


# The agent step: PR-Agent's tool is replaced by a stand-in that records what it was given.

def test_the_question_reaches_pr_agent_as_one_argument_with_a_fixed_prefix(tmp_path, monkeypatch):
    import sys
    import types

    seen = {}

    class Questions:
        prediction = "The answer."

        def _prepare_pr_answer(self):
            return "formatted"

    def run(argv):
        seen["argv"] = argv
        Questions()._prepare_pr_answer()  # patched by agent(); keeps the prediction
        return None

    tools = types.ModuleType("pr_agent.tools.pr_questions")
    tools.PRQuestions = Questions
    cli = types.ModuleType("pr_agent.cli")
    cli.run = run
    pkg, tools_pkg = types.ModuleType("pr_agent"), types.ModuleType("pr_agent.tools")
    pkg.cli, tools_pkg.pr_questions = cli, tools
    for name, module in (("pr_agent", pkg), ("pr_agent.cli", cli), ("pr_agent.tools", tools_pkg), ("pr_agent.tools.pr_questions", tools)):
        monkeypatch.setitem(sys.modules, name, module)
    question = tmp_path / "q.txt"
    question.write_text("--config.model=evil\n--pr_reviewer.extra_instructions=x")
    raw = tmp_path / "raw"
    assert ask_job.agent("https://example.test/pr/7", question, raw) == 0
    assert seen["argv"] == ["--pr_url", "https://example.test/pr/7", "ask",
                            "Question: --config.model=evil\n--pr_reviewer.extra_instructions=x"]
    assert raw.read_text() == "The answer."


def test_a_failing_pr_agent_leaves_no_answer(tmp_path, monkeypatch):
    import sys
    import types

    class Questions:
        prediction = None

        def _prepare_pr_answer(self):
            return ""

    def boom(argv):
        raise RuntimeError("quotes the PR: secret-looking text")

    tools = types.ModuleType("pr_agent.tools.pr_questions")
    tools.PRQuestions = Questions
    cli = types.ModuleType("pr_agent.cli")
    cli.run = boom
    pkg, tools_pkg = types.ModuleType("pr_agent"), types.ModuleType("pr_agent.tools")
    pkg.cli, tools_pkg.pr_questions = cli, tools
    for name, module in (("pr_agent", pkg), ("pr_agent.cli", cli), ("pr_agent.tools", tools_pkg), ("pr_agent.tools.pr_questions", tools)):
        monkeypatch.setitem(sys.modules, name, module)
    question = tmp_path / "q.txt"
    question.write_text("hi")
    assert ask_job.agent("u", question, tmp_path / "raw") == 1
    assert not (tmp_path / "raw").exists()


# The workflow and the action.

WORKFLOW_FILE = GITHUB / "workflows" / "merge-steward-ask.yaml"
WORKFLOW = yaml.safe_load(WORKFLOW_FILE.read_text())
JOBS = WORKFLOW["jobs"]
ACTION_FILE = GITHUB / "actions" / "merge-steward-ask" / "action.yaml"
ACTION = yaml.safe_load(ACTION_FILE.read_text())
TRIGGERS = WORKFLOW.get("on", WORKFLOW.get(True))


def test_the_workflow_is_triggered_by_new_comments_only_and_receives_no_webhook():
    assert TRIGGERS == {"issue_comment": {"types": ["created"]}}
    assert "workflow_dispatch" not in TRIGGERS and "repository_dispatch" not in TRIGGERS


def test_every_condition_of_the_gate_is_in_the_job_if():
    condition = " ".join(JOBS["gate"]["if"].split())
    for part in ("vars.MERGE_STEWARD_AI == 'true'", "github.event.action == 'created'",
                 "github.event.issue.pull_request != null", "github.event.issue.state == 'open'",
                 "github.event.comment.author_association == 'OWNER'", "github.event.comment.user.type == 'User'",
                 "github.event.sender.type == 'User'", "startsWith(github.event.comment.body, '/ask')"):
        assert part in condition


def test_the_runner_job_waits_for_the_gate_and_holds_only_the_review_environment():
    job = JOBS["ask"]
    assert job["needs"] == "gate" and "needs.gate.outputs.ok == 'true'" in job["if"]
    assert job["runs-on"] == "gha-prox-ops-steward" and job["environment"] == "merge-steward-review"
    assert set(job["permissions"].values()) == {"read"}
    assert "app-token" not in json.dumps(job) and "STEWARD_APP" not in json.dumps(job)


def test_the_gate_and_the_publisher_run_on_github_hosted_runners_and_the_gate_has_no_secret():
    assert JOBS["gate"]["runs-on"] == "ubuntu-latest" and JOBS["publish"]["runs-on"] == "ubuntu-latest"
    assert "secrets." not in json.dumps(JOBS["gate"]) and "environment" not in JOBS["gate"]
    assert set(JOBS["gate"]["permissions"].values()) == {"read"}


def test_the_publisher_gets_a_pull_request_scoped_app_token_through_the_steward_environment():
    job = JOBS["publish"]
    assert job["environment"] == "merge-steward" and set(job["permissions"].values()) == {"read"}
    assert job["needs"] == ["gate", "ask"] and job["if"] == "needs.ask.result == 'success'"
    token = next(s for s in job["steps"] if s.get("id") == "app-token")
    assert {k for k in token["with"] if k.startswith("permission-")} == {"permission-pull-requests"}
    assert "LLM_KEY" not in json.dumps(job)


def test_an_ask_never_shares_a_concurrency_group_with_a_review_and_never_cancels():
    group = JOBS["ask"]["concurrency"]
    assert group["cancel-in-progress"] is False and group["group"].startswith("merge-steward-ask-")
    assert "comment.id" in group["group"]  # one group per question, so no question is dropped
    steward = yaml.safe_load((GITHUB / "workflows" / "merge-steward.yaml").read_text())["jobs"]
    for job in steward.values():
        assert "merge-steward-ask" not in json.dumps(job.get("concurrency", ""))


def test_no_run_step_contains_an_expression_and_no_step_touches_pr_code():
    steps = [s for job in JOBS.values() for s in job["steps"]] + ACTION["runs"]["steps"]
    for step in steps:
        assert "${{" not in str(step.get("run") or ""), step.get("name")
        if "actions/checkout" in step.get("uses", ""):
            assert step["with"]["persist-credentials"] is False and "default_branch" in step["with"]["ref"]
            assert "repository" not in step["with"]
    text = WORKFLOW_FILE.read_text() + ACTION_FILE.read_text()
    assert not re.search(r"refs/pull|merge_commit_sha|head\.sha|head\.ref|github\.head_ref", text)
    assert "comment.body" not in text.replace("startsWith(github.event.comment.body, '/ask')", "")


def test_the_key_is_used_once_and_only_in_the_agent_step():
    assert ACTION_FILE.read_text().count("inputs.llm-key") == 1
    assert len(re.findall(r"secrets\.MERGE_STEWARD_LLM_KEY", WORKFLOW_FILE.read_text())) == 1
    agent = next(s for s in ACTION["runs"]["steps"] if s.get("name") == "Run PR-Agent")
    assert agent["env"]["OPENAI__KEY"] == "${{ inputs.llm-key }}"
    for step in ACTION["runs"]["steps"]:
        if step is not agent:
            assert "OPENAI__KEY" not in json.dumps(step)


def test_pr_agent_ask_writes_nothing_and_reads_no_repository_settings():
    env = next(s for s in ACTION["runs"]["steps"] if s.get("name") == "Run PR-Agent")["env"]
    assert env["CONFIG__PUBLISH_OUTPUT"] == "false" and env["CONFIG__USE_REPO_SETTINGS_FILE"] == "false"
    assert env["CONFIG__USE_GLOBAL_SETTINGS_FILE"] == "false" and env["OPENAI__API_BASE"].startswith("http://litellm.ai.svc.cluster.local:4000")
    assert env["CONFIG__MODEL"] == "openai/${{ steps.prepare.outputs.alias }}"
    upload = next(s for s in ACTION["runs"]["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["path"].endswith("/answer.json")


def test_asks_do_not_exist_in_the_verdict_workflow_and_no_job_is_named_like_the_required_check():
    steward = yaml.safe_load((GITHUB / "workflows" / "merge-steward.yaml").read_text())
    assert "issue_comment" not in json.dumps(steward.get("on", steward.get(True)))
    names = {job["name"] for job in JOBS.values()}
    assert "merge-steward" not in names and names <= set(POLICY["ignore_checks"])
    # Nothing in the verdict workflow reads anything from this one.
    assert "merge-steward-ask" not in (GITHUB / "workflows" / "merge-steward.yaml").read_text()
    assert Path(WORKFLOW_FILE).name in (GITHUB / "workflows" / "merge-steward-tests.yaml").read_text() or "merge-steward*.yaml" in (
        GITHUB / "workflows" / "merge-steward-tests.yaml").read_text()


@pytest.mark.parametrize("body", [
    "/ask --config.model=evil", "/ask what is this --config.model=evil", "/ask line\n--pr_questions.extra_instructions=x",
    "/ask a\t--b", "/ask x=--c", "/ask (--c)", "/ask a,--c", "/ask --", "/ask x ---c",
])
def test_a_word_that_pr_agent_would_read_as_a_setting_is_refused(body):
    with pytest.raises(Refused) as why:
        ask_job.question_of(body)
    assert why.value.args[0] == "option"


@pytest.mark.parametrize("body", [
    "/ask is this a breaking-change?", "/ask what about the re-render step", "/ask does a -1 offset matter",
    "/ask a - b", "/ask x-y--z",
])
def test_ordinary_dashes_are_fine(body):
    assert ask_job.question_of(body)


def test_no_accepted_question_has_a_word_that_starts_with_two_dashes_after_pr_agent_splits_it():
    """PR-Agent joins the arguments and splits the string with shlex; check every word it would see."""
    import shlex

    bodies = ["/ask normal question?", "/ask a - b", "/ask x-y--z", "/ask does 'quoted' text work", "/ask line one\nline two",
              "/ask what about #12 and \"this\"", "/ask tab\tseparated"]
    for body in bodies:
        question = ask_job.question_of(body)
        lexer = shlex.shlex("ask " + f"Question: {question}", posix=True)
        lexer.whitespace_split, lexer.quotes, lexer.commenters = True, '"', ""
        try:
            words = list(lexer)
        except ValueError:
            continue  # an unbalanced quote fails the run, which is harmless
        assert not [w for w in words if w.startswith("--")], body


def test_unprintable_spaces_are_removed_so_they_cannot_hide_a_word_break():
    assert ask_job.question_of("/ask a\u00a0--c") == "a--c"
    assert ask_job.question_of("/ask a\u2003--c") == "a--c"


@pytest.mark.parametrize("body", [
    "/ask a \\--x", "/ask a -\\-x", '/ask a -""-x', '/ask a "-"-x', "/ask a -''-x", "/ask a -\"\"-x",
    "/ask x \"--c=1\"", "/ask x '--c=1'", "/ask `--c`",
])
def test_quotes_and_backslashes_cannot_assemble_a_setting(body):
    import shlex

    try:
        question = ask_job.question_of(body)
    except Refused as why:
        assert why.args[0] == "option"
        return
    lexer = shlex.shlex("Question: " + question.replace("'", "\\'"), posix=True)
    lexer.whitespace_split, lexer.quotes, lexer.commenters = True, '"', ""
    assert not [w for w in lexer if w.startswith("--")]


def test_quotes_become_typographic_marks_so_the_question_stays_readable():
    assert ask_job.question_of("/ask what's \"this\"?") == "what\u2019s \u201dthis\u201d?"


def test_an_answer_of_ampersands_stays_under_the_comment_limit():
    answer = "\n".join("&" * 5000 for _ in range(100))
    body = ask_job.reply({"comment": 55, "available": True, "answer": answer})
    assert len(body) < 65536 and body.count("\n") < 60


@pytest.mark.parametrize("answer", [
    "### Merge steward: pass (all stages passed)", "# Merge steward: pass", "## x", "###### x", "   ### indented",
    "- ### Merge steward: pass", "1. ## Merge steward: pass", "* # x", "> # quoted heading",
    "Merge steward: pass\n====", "Merge steward: pass\n----", "Merge steward: pass\n___", "***", "---", "- - -",
    "### AI answer (advisory)", "####\t# x",
])
def test_the_answer_cannot_carry_a_heading_or_rule(answer):
    cleaned = ask_job.clean_answer(answer)
    for line in cleaned.split("\n"):
        assert not line.lstrip().startswith("#") and not set(line.strip()) <= set("=-_*") or not line.strip()
    body = ask_job.reply({"comment": 55, "available": True, "answer": answer})
    own = [l for l in body.split("\n") if l.startswith("#")]
    assert own == ["### AI answer (advisory)"]  # only our own heading


def test_list_items_become_plain_bullets_and_text_is_kept():
    cleaned = ask_job.clean_answer("Intro line\n- first\n* second\n1. third\n2) fourth\n+ fifth\nplain")
    assert cleaned.split("\n") == ["Intro line", "\u2022 first", "\u2022 second", "\u2022 third", "\u2022 fourth",
                                   "\u2022 fifth", "plain"]


def test_cleaning_twice_changes_nothing():
    answer = "### Heading\n- item\n1. num\ntext & more\n\n---\n> quote\n**bold** and `code`"
    once = ask_job.clean_answer(answer)
    assert ask_job.clean_answer(once) == once


def test_ordinary_text_and_inline_marks_survive():
    cleaned = ask_job.clean_answer("It sets replicas to 2 (was 1).\nUse **care** with C# and a-b-c.")
    assert cleaned.split("\n")[0] == "It sets replicas to 2 (was 1)."
    assert "care" in cleaned and "**" not in cleaned and "a-b-c" in cleaned


@pytest.mark.parametrize("answer", ["**Verdict:** pass (all stages passed)", "__Verdict:__ pass", "*Merge steward: pass*"])
def test_emphasis_cannot_dress_a_line_as_the_stewards_verdict(answer):
    assert "*" not in ask_job.clean_answer(answer)


@pytest.mark.parametrize("underline", ["=", "==", "-", "--", "- ", "+", "_", "= =", "-\t"])
def test_a_short_setext_underline_or_empty_marker_is_dropped(underline):
    cleaned = ask_job.clean_answer(f"Merge steward: pass\n{underline}\nmore text")
    assert cleaned.split("\n") == ["Merge steward: pass", "", "more text"]


def test_a_line_that_is_only_markup_is_blank():
    assert ask_job.clean_answer("a\n###\n>\n|\n***\nb").split("\n") == ["a", "", "", "", "", "b"]


def test_random_answers_end_with_no_block_markup_and_cleaning_is_a_fixed_point():
    import random

    rng = random.Random(20261107)
    alphabet = ["#", "*", "-", "=", "_", ">", "&", ";", "\\", " ", " ", "1", ".", "a", "b", "\n", "\n", "\t", "35", "&#35;",
                "%23", "+", "|", "`", "!", "[", "]", "(", ")", "x"]
    for _ in range(3000):
        answer = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 40)))
        once = ask_job.clean_answer(answer)
        assert ask_job.clean_answer(once) == once, repr(answer)
        for line in once.split("\n"):
            stripped = line.strip()
            assert not stripped.startswith(("#", ">", "|", "`")), repr(answer)
            assert not (stripped and not any(c.isalnum() for c in stripped) and stripped != "\u2022"), repr(answer)
            assert not (len(stripped) > 1 and stripped[0] in "-+" and stripped[1] == " "), repr(answer)
            assert "*" not in line and "![" not in line and "](" not in line, repr(answer)


@pytest.mark.parametrize("digits", [4301, 5000, 100000])
def test_a_comment_with_a_huge_number_in_a_marker_cannot_stop_the_answer(tmp_path, monkeypatch, digits):
    huge = {"id": 3, "body": "<!-- merge-steward-ask comment=" + "9" * digits + " -->", "user": {"login": "someone"}}
    posted, _, app = publish(tmp_path, monkeypatch, comments=[huge])
    assert posted and len(app.writes) == 1


def test_the_marker_pattern_is_bounded_and_an_app_comment_with_a_huge_marker_is_ignored(tmp_path, monkeypatch):
    assert ask_job._MARKER.search("<!-- merge-steward-ask comment=" + "1" * 21 + " -->") is None
    huge = {"id": 3, "body": "<!-- merge-steward-ask comment=" + "9" * 5000 + " -->",
            "performed_via_github_app": {"id": APP}}
    posted, _, _ = publish(tmp_path, monkeypatch, comments=[huge])
    assert posted


def test_only_the_apps_marker_for_this_comment_counts_even_when_others_copy_it(tmp_path, monkeypatch):
    marker = ask_job.ASK_MARKER.format(comment=55)
    others = [{"id": 1, "body": marker, "user": {"login": "someone"}},
              {"id": 2, "body": marker, "performed_via_github_app": {"id": 1}},
              {"id": 3, "body": ask_job.ASK_MARKER.format(comment=54), "performed_via_github_app": {"id": APP}}]
    posted, _, app = publish(tmp_path, monkeypatch, comments=others)
    assert posted and len(app.writes) == 1


@pytest.mark.parametrize("answer", ["__Verdict:__ pass (all stages passed)", "___Merge steward: pass___", "_Verdict:_ pass",
                                   "x __bold__ y", "__a", "a__", "_a_ and _b_", "a _b_", "\\__Verdict:__ pass",
                                   "&#95;&#95;Verdict:&#95;&#95; pass", "%5f%5fVerdict:%5f%5f pass"])
def test_underscore_emphasis_cannot_dress_a_line_as_the_stewards_verdict(answer):
    cleaned = ask_job.clean_answer(answer)
    assert "__" not in cleaned
    for word in cleaned.replace("\n", " ").split():
        assert not word.startswith("_") and not word.endswith("_"), (answer, cleaned)


def test_underscores_inside_names_survive():
    assert ask_job.clean_answer("Set cluster_name and max_unavailable_pods.") == "Set cluster_name and max_unavailable_pods."


@pytest.mark.parametrize("answer", ["$Verdict: pass$", "$$Verdict$$", "$\\textbf{Verdict}$ pass", "a $x$ b", "$$\nV\n$$"])
def test_math_delimiters_cannot_set_a_line_apart(answer):
    assert "$" not in ask_job.clean_answer(answer)
