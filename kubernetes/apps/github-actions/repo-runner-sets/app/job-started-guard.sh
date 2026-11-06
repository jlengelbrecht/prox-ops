#!/usr/bin/env bash
# Runner job-started hook: run only jobs from trusted events of this repository.
#
# The runner executes this script (ACTIONS_RUNNER_HOOK_JOB_STARTED) as a step
# before the first workflow step.
#
# A non-zero exit alone is NOT enough. The runner (actions/runner v2.338.2,
# src/Runner.Worker/StepsRunner.cs) only marks the job failed and keeps going:
# every later step whose condition holds still runs, and `if: always()` and
# `if: failure()` hold after a failed step. So a denied job is stopped by
# SIGKILLing the Runner.Worker process that is executing it. No further step
# can start; the listener sees the worker die abnormally, reports the job as
# failed (JobDispatcher.cs), and the ephemeral runner pod finishes.
#
# Why the check lives on the runner and not in the workflow: for pull_request
# events the workflow file is read from the pull request's own merge ref, so a
# fork author controls the YAML and can delete any `if:` guard or change
# `runs-on`. Repository settings ("require approval for fork workflows") depend
# on how GitHub classifies the contributor and can be changed by anyone with
# admin access. This script ships with the runner pod from a ConfigMap, so
# nothing in the job can skip or modify it.
#
# Allowlist; every other event is denied (one ::error:: line, then the worker is
# killed and the hook exits 1):
#   - push, workflow_dispatch, schedule, merge_group, release
#   - pull_request, pull_request_review, pull_request_review_comment whose head
#     repository is this repository (a null head repo is a deleted fork)
#   - workflow_run whose triggering run's head repository is this repository
#   - pull_request_target whose head repository is this repository, only on a
#     runner set that opts in with RUNNER_GUARD_ALLOW_PR_TARGET=true (see below)
#   - issue_comment, only on that same opted-in set, only a newly created comment on a
#     pull request that the repository owner wrote (author_association OWNER, both the
#     commenter and the sender a User). The payload does not say where the pull request
#     comes from, so a fork is not refused here: the workflow has to check the PR through
#     the API before the job does any work, and must never check the PR out.
# Denied examples: pull_request_target and issue_comment anywhere else, any other
# issue_comment, issues, repository_dispatch, and any event added to GitHub later. A missing or
# unparseable event payload is denied too (fail closed).
#
# The opt-in is set on the runner pod by the ResourceSet and is read only for
# pull_request_target, whose workflow file comes from the base branch, so a fork
# cannot remove the same-repository check. Those jobs hold secrets, which is why
# this stays off unless a runner set needs it, and why such a set must run only
# workflows that never check out the PR.
#
# Out of scope: an allowed event still runs a workflow from the base branch,
# and that workflow can check out an untrusted ref (a fork PR head, a branch
# named in a dispatch input). Not doing that is the workflow author's job;
# this guard only decides which events may start a job here.
set -euo pipefail

# Do not trust the inherited environment for the commands below.
export PATH=/usr/local/bin:/usr/bin:/bin
unset BASH_ENV ENV LD_PRELOAD LD_LIBRARY_PATH

# ancestor_pid <comm>: pid of the nearest ancestor process whose name is <comm>.
ancestor_pid() {
  local pid="$$" name key val ppid
  while [[ "$pid" =~ ^[0-9]+$ ]] && ((pid > 1)); do
    name=""
    read -r name <"/proc/${pid}/comm" 2>/dev/null || true
    if [[ "$name" == "$1" ]]; then
      printf '%s' "$pid"
      return 0
    fi
    ppid=""
    while read -r key val; do
      if [[ "$key" == "PPid:" ]]; then
        ppid="$val"
        break
      fi
    done <"/proc/${pid}/status" 2>/dev/null || true
    pid="$ppid"
  done
  return 1
}

deny() {
  local msg target
  msg="Self-hosted runner job guard blocked this job: $1"
  printf '::error::%s\n' "$msg"
  printf '%s\n' "$msg" >&2
  # Exiting non-zero only fails this step; `if: always()` steps would still run.
  # Kill the worker so nothing else can start. Fall back to the listener, which
  # takes the worker down with it.
  target="$(ancestor_pid Runner.Worker || ancestor_pid Runner.Listener || true)"
  if [[ -n "$target" ]]; then
    kill -KILL "$target" 2>/dev/null || true
  else
    printf '::error::job guard could not find the runner worker to terminate\n'
  fi
  exit 1
}

command -v jq >/dev/null 2>&1 || deny "jq is not available on the runner, cannot inspect the event"

event_name="${GITHUB_EVENT_NAME:-}"
event_path="${GITHUB_EVENT_PATH:-}"

[[ -n "$event_name" ]] || deny "GITHUB_EVENT_NAME is not set"
[[ -n "$event_path" && -f "$event_path" && -r "$event_path" ]] \
  || deny "event payload file is missing (GITHUB_EVENT_PATH='${event_path}')"
jq -e 'type == "object"' "$event_path" >/dev/null 2>&1 \
  || deny "event payload at ${event_path} is not a JSON object"

# require_same_repo <jq path to the head repository's full_name> <what>
require_same_repo() {
  local base_repo head_repo
  base_repo="$(jq -r '.repository.full_name // empty' "$event_path")"
  head_repo="$(jq -r "$1 // empty" "$event_path")"
  [[ -n "$base_repo" ]] || deny "${event_name} payload has no repository.full_name"
  [[ -n "$head_repo" ]] || deny "${event_name} $2 head repository is missing (deleted fork?)"
  [[ "$head_repo" == "$base_repo" ]] \
    || deny "${event_name} $2 comes from fork ${head_repo}, not ${base_repo}"
}

case "$event_name" in
  push | workflow_dispatch | schedule | merge_group | release)
    ;;
  pull_request | pull_request_review | pull_request_review_comment)
    require_same_repo '.pull_request.head.repo.full_name' "pull request"
    ;;
  workflow_run)
    require_same_repo '.workflow_run.head_repository.full_name' "triggering run"
    ;;
  pull_request_target)
    [[ "${RUNNER_GUARD_ALLOW_PR_TARGET:-}" == "true" ]] \
      || deny "event 'pull_request_target' is not allowed on this runner set"
    require_same_repo '.pull_request.head.repo.full_name' "pull request"
    ;;
  issue_comment)
    [[ "${RUNNER_GUARD_ALLOW_PR_TARGET:-}" == "true" ]] \
      || deny "event 'issue_comment' is not allowed on this runner set"
    jq -e '.action == "created"
      and (.issue.pull_request | type) == "object"
      and .comment.author_association == "OWNER"
      and .comment.user.type == "User"
      and .sender.type == "User"
      and (.repository.full_name | type) == "string"' "$event_path" >/dev/null 2>&1 \
      || deny "issue_comment is allowed here only for a new comment on a pull request by the repository owner"
    ;;
  *)
    deny "event '${event_name}' is not on the self-hosted runner allowlist"
    ;;
esac

exit 0
