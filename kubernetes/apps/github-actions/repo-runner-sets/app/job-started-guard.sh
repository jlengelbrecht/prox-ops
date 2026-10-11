#!/usr/bin/env bash
# Runner job-started hook: run only jobs from trusted events of this repository.
#
# The runner executes this script (ACTIONS_RUNNER_HOOK_JOB_STARTED) before the
# first workflow step. A non-zero exit fails the job before any user code runs.
#
# Why the check lives on the runner and not in the workflow: for pull_request
# events the workflow file is read from the pull request's own merge ref, so a
# fork author controls the YAML and can delete any `if:` guard or change
# `runs-on`. Repository settings ("require approval for fork workflows") depend
# on how GitHub classifies the contributor and can be changed by anyone with
# admin access. This script ships with the runner pod from a ConfigMap, so
# nothing in the job can skip or modify it.
#
# Allowlist; every other event is denied (exit 1, one ::error:: line):
#   - push, workflow_dispatch, schedule, merge_group, release
#   - pull_request, pull_request_review, pull_request_review_comment whose head
#     repository is this repository (a null head repo is a deleted fork)
#   - workflow_run whose triggering run's head repository is this repository
# Denied examples: pull_request_target, issue_comment, issues,
# repository_dispatch, and any event added to GitHub later. A missing or
# unparseable event payload is denied too (fail closed).
#
# Out of scope: an allowed event still runs a workflow from the base branch,
# and that workflow can check out an untrusted ref (a fork PR head, a branch
# named in a dispatch input). Not doing that is the workflow author's job;
# this guard only decides which events may start a job here.
set -euo pipefail

deny() {
  printf '::error::Self-hosted runner job guard blocked this job: %s\n' "$1"
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
  *)
    deny "event '${event_name}' is not on the self-hosted runner allowlist"
    ;;
esac

exit 0
