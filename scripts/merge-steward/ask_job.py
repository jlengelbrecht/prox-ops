"""The /ask command: an owner asks a question about a PR in a comment and the review model answers.

Steps, on either side of PR-Agent, in the order the workflow runs them:

  gate     GitHub-hosted, read-only, no secrets. Says whether this comment may start the runner job.
  prepare  On the steward runner. Checks the same things again from the event file, then writes
           the question and the context file.
  agent    On the steward runner. Runs PR-Agent's ask tool with publishing off and keeps the answer.
  collect  On the steward runner. Filters the answer into answer.json, the only thing uploaded.
  publish  GitHub-hosted, holds the steward app token. Posts the filtered answer as the app.

The comment, the PR and the model's answer are all untrusted. The question reaches PR-Agent as words
that cannot start with two dashes, and the answer reaches GitHub only through review.scrub. Nothing in this file passes any
of them to a shell. No part of it changes a verdict: the only write is one comment.

Environment: GITHUB_EVENT_PATH, GITHUB_REPOSITORY, GITHUB_TOKEN (read-only; the app token for publish).
"""

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

import classify
import review
import steward
from ghapi import GitHub, GitHubError

ASK_MARKER = "<!-- merge-steward-ask comment={comment} -->"
_MARKER = re.compile(r"^<!-- merge-steward-ask comment=(?P<comment>[0-9]+) -->$", re.MULTILINE)
_COMMAND = re.compile(r"/ask(?:[ \t]+|\r?\n)(?P<question>.+)", re.DOTALL)
_PLAIN_QUOTES = str.maketrans({'"': "\u201d", "\'": "\u2019", "\\": "/", "`": "\u2019"})
_OPTION = re.compile(r"(?:^|[\s\"'=(\[{,;:])-{2}", re.MULTILINE)
MAX_QUESTION, MAX_ANSWER_LINES, LINE_LIMIT = 1000, 40, 600
ASK_DIR, QUESTION_FILE, RAW_FILE, ANSWER_FILE = "steward-ask", "question.txt", "answer.raw", "answer.json"
# The only reasons a refusal may carry, so nothing from the event or an artifact is echoed.
REFUSALS = {
    "event": "not a new comment on a pull request",
    "author": "the comment is not from the repository owner",
    "bot": "the comment is not from a person",
    "command": "the comment is not an /ask with a question",
    "long": "the question is longer than 1000 characters",
    "option": "the question has a word that starts with two dashes; rephrase it",
    "closed": "the pull request is not open",
    "fork": "the pull request does not come from this repository",
    "scope": "the policy does not review this pull request",
}


class Refused(Exception):
    """The comment may not run an ask. args[0] is a key of REFUSALS."""


def question_of(body):
    """The question after /ask, control characters removed, or Refused."""
    found = _COMMAND.fullmatch((body or "").strip())
    if not found:
        raise Refused("command")
    text = "".join(c for c in found.group("question") if c in "\n\t" or c.isprintable()).strip()
    if not text:
        raise Refused("command")
    if len(text) > MAX_QUESTION:
        raise Refused("long")
    # PR-Agent joins its arguments into one string and splits it with shlex, and reads any word
    # that starts with -- as a setting. Quotes and backslashes are how shlex turns "-" "-x" or
    # -\\-x into --x, so they become typographic marks first and the split is plain whitespace.
    text = text.translate(_PLAIN_QUOTES)
    if _OPTION.search(text):
        raise Refused("option")
    # Belt and braces: split it the way PR-Agent does and look at every word.
    lexer = shlex.shlex("Question: " + text, posix=True)
    lexer.whitespace_split, lexer.quotes, lexer.commenters = True, '"', ""
    try:
        if any(word.startswith("--") for word in lexer):
            raise Refused("option")
    except ValueError:
        raise Refused("option") from None
    return text


def check_event(event, repo):
    """(PR number, comment id, question) for a comment that may ask, else Refused.

    The payload comes from GitHub, not from the commenter, but each field is checked as if it
    could be anything. The owner test uses GitHub's own author_association.
    """
    comment, issue, sender = event.get("comment"), event.get("issue"), event.get("sender")
    if not (event.get("action") == "created" and isinstance(comment, dict) and isinstance(issue, dict)
            and isinstance(issue.get("pull_request"), dict) and (event.get("repository") or {}).get("full_name") == repo):
        raise Refused("event")
    if issue.get("state") != "open":
        raise Refused("closed")
    user = comment.get("user") or {}
    if comment.get("author_association") != "OWNER" or not user.get("login") or user.get("login") != (sender or {}).get("login"):
        raise Refused("author")
    if user.get("type") != "User" or (sender or {}).get("type") != "User" or comment.get("performed_via_github_app"):
        raise Refused("bot")
    number, comment_id = issue.get("number"), comment.get("id")
    if type(number) is not int or type(comment_id) is not int:
        raise Refused("event")
    return number, comment_id, question_of(comment.get("body"))


def check_pr(gh, number, policy):
    """The PR, if it is open, from this repository and in the policy's scope. Else Refused."""
    pr = gh.get(f"pulls/{number}")
    if pr.get("state") != "open":
        raise Refused("closed")
    if review.mode(pr, policy) == review.OFF:
        raise Refused("fork" if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != gh.repo else "scope")
    return pr


def load_event():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if not isinstance(event, dict):
        raise ValueError("the event payload is not an object")
    return event


def gate(gh, policy):
    """Outputs for the workflow. Anything that is not a clear yes is no."""
    try:
        number, comment_id, _ = check_event(load_event(), gh.repo)
        check_pr(gh, number, policy)
    except Refused as why:
        print(f"ask refused: {REFUSALS[why.args[0]]}")
        return {"ok": "false"}
    except (GitHubError, OSError, ValueError, KeyError) as err:  # the type only; never a response body
        print(f"ask gate failed: {type(err).__name__}", file=sys.stderr)
        return {"ok": "false"}
    return {"ok": "true", "pr": number, "comment": comment_id}


def prepare(gh, policy, out):
    """Write the question where the agent step reads it. Raises Refused on any doubt."""
    number, comment_id, question = check_event(load_event(), gh.repo)
    pr = check_pr(gh, number, policy)
    out.mkdir(parents=True, exist_ok=True)
    (out / QUESTION_FILE).write_text(question)
    model = policy["ai"]["model"]  # exactly the validated keys; the rest come last
    return {**model, "pr": number, "comment": comment_id, "url": pr["html_url"]}


def agent(url, question_file, raw_file):
    """Run PR-Agent's ask tool and keep the answer text. Needs the pr_agent package (runner only).

    Publishing is off, so the tool keeps its answer in memory; the formatted answer is taken
    from the method that builds it. PR-Agent joins its arguments into one string and splits it
    again, so the question reaches it as words: question_of has already refused any word that
    starts with two dashes, and the fixed prefix keeps the first word from being an option.
    """
    from pr_agent import cli  # imported here: the package is on the runner, not in the unit tests
    from pr_agent.tools.pr_questions import PRQuestions

    question = Path(question_file).read_text()
    kept, build = [], PRQuestions._prepare_pr_answer  # the method that builds the answer

    def capture(self):
        kept.append(self.prediction)
        return build(self)

    PRQuestions._prepare_pr_answer = capture
    try:
        code = cli.run(["--pr_url", url, "ask", f"Question: {question}"])
    except Exception as err:  # the type only: the message can quote the PR or the model
        print(f"ask failed: {type(err).__name__}", file=sys.stderr)
        return 1
    if code or not kept or not isinstance(kept[-1], str):
        return 1
    Path(raw_file).write_text(kept[-1])
    return 0


_HEADING = re.compile(r"^\s*#+\s*")
_RULE = re.compile(r"^\s*[=_*-]{3,}\s*$")
_LIST =re.compile(r"^\s*(?:[-*+]|[0-9]{1,3}[.)])\s+")


def flat(line):
    """A line without the block markdown that would let the model write a heading, rule or list.

    The comment is the steward app's own, so a heading planted by a prompt injection (a fake
    verdict heading, say) would read as the steward speaking. Headings, setext underlines and
    horizontal rules are dropped, and list markers become a plain bullet, so what is left
    renders as an ordinary paragraph line. Asterisks go too, so bold text cannot imitate the
    steward's "Verdict:" line either. Applying it twice gives the same line.
    """
    if _RULE.match(line):
        return ""
    bullet = False
    for _ in range(4):
        before = line
        if _LIST.match(line):
            bullet, line = True, _LIST.sub("", line, count=1)
        line = _HEADING.sub("", line, count=1)
        if line == before:
            break
    line = line.replace("*", "").strip()
    # A line with no letter or digit is only markup: a one or two character setext underline
    # ("=", "--"), an empty list item, a stray rule. None of it carries meaning.
    if not any(c.isalnum() for c in line):
        return ""
    return f"\u2022 {line}" if bullet else line


def clean_answer(text):
    """The model's answer as safe lines, cleaned until a pass changes nothing.

    scrub decodes entities and removes backslashes and asterisks before flat() looks at a line,
    so one pass can leave text from which the next pass removes a marker that has just appeared.
    Repeating to a fixed point (at most four passes) means the stored answer and the posted one
    are the same text.
    """
    text = str(text or "")
    for _ in range(4):
        cleaned = _clean_once(text)
        if cleaned == text:
            break
        text = cleaned
    return text


def _clean_once(text):
    """One pass: each line through scrub and flat, blank lines kept, length bounded."""
    lines = []
    for raw in str(text or "").replace("\r", "").split("\n")[:MAX_ANSWER_LINES]:
        raw = raw.strip()
        # scrub cuts to the limit before clean() escapes & as &amp;, which can grow a line five times.
        lines.append(flat(review.scrub(raw, LINE_LIMIT)[:LINE_LIMIT]) if raw else "")
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def collect(out, status, comment_id):
    """answer.json from the raw answer. Every run that reached the agent leaves one."""
    result = {"comment": comment_id}
    raw = out / RAW_FILE
    if status != "ok":
        result.update(available=False, reason=status if status in review.REASONS else "failed")
    elif not raw.is_file():
        result.update(available=False, reason="no-output")
    else:
        answer = clean_answer(raw.read_text(errors="replace"))
        if answer:
            result.update(available=True, answer=answer)
        else:
            result.update(available=False, reason="no-output")
    (out / ANSWER_FILE).write_text(json.dumps(result))
    print(json.dumps({"comment": comment_id, "available": result["available"], "reason": result.get("reason"),
                      "answer_chars": len(result.get("answer", ""))}))
    return result


def reply(result):
    """The comment body for an answer.json, validated again: an artifact is data, whoever wrote it."""
    comment = result.get("comment") if isinstance(result, dict) else None
    if type(comment) is not int:
        return None
    head = [ASK_MARKER.format(comment=comment), "### AI answer (advisory)", ""]
    if result.get("available") is True and isinstance(result.get("answer"), str):
        body = clean_answer(result["answer"])
        return "\n".join(head + [body or "The model gave an empty answer.", "",
                                 "Answers come from the review model reading the PR. They change no check."])
    reason = review.REASONS.get(result.get("reason"), review.REASONS["failed"])
    return "\n".join(head + [f"No answer: {reason}."])


def publish(gh, app_gh, policy, answer_dir):
    """Post the answer once per question comment, as the app. Returns True when a comment was posted."""
    number, comment_id, _ = check_event(load_event(), gh.repo)
    check_pr(gh, number, policy)
    try:
        result = json.loads((Path(answer_dir) / ANSWER_FILE).read_text())
    except (OSError, ValueError):
        result = {"comment": comment_id, "available": False, "reason": "missing"}
    if not isinstance(result, dict) or result.get("comment") != comment_id:
        print("ask answer belongs to another comment; not posting", file=sys.stderr)
        return False
    body = reply(result)
    for existing in gh.paginate(f"issues/{number}/comments"):
        found = _MARKER.search(existing.get("body") or "")
        if (found and int(found.group("comment")) == comment_id
                and (existing.get("performed_via_github_app") or {}).get("id") == policy["steward_app_id"]):
            print("ask already answered; not posting again")
            return False
    app_gh.post(f"issues/{number}/comments", {"body": body})
    return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["gate", "prepare", "agent", "collect", "publish"])
    parser.add_argument("--out", help="the ask directory")
    parser.add_argument("--url")
    parser.add_argument("--status", default="failed", help="what happened to PR-Agent")
    parser.add_argument("--comment", type=int, default=0)
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else None
    if args.step == "agent":
        return agent(args.url, out / QUESTION_FILE, out / RAW_FILE)
    if args.step == "collect":
        collect(out, args.status, args.comment)
        return 0
    policy = classify.load_policy(steward.POLICY)
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    outputs = {}
    if args.step == "gate":
        outputs = gate(gh, policy)
    elif args.step == "prepare":
        try:
            outputs = prepare(gh, policy, out)
        except Refused as why:
            print(f"::error::ask refused: {REFUSALS[why.args[0]]}")
            return 1
    else:
        app_gh = GitHub(os.environ["STEWARD_APP_TOKEN"], os.environ["GITHUB_REPOSITORY"])
        publish(gh, app_gh, policy, args.out)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
