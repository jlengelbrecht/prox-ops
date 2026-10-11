#!/usr/bin/env bash
# Local self-test for job-started-guard.sh. Not deployed (not listed in
# kustomization.yaml). Needs bash and jq.
#   bash kubernetes/apps/github-actions/repo-runner-sets/app/job-started-guard.test.sh
set -euo pipefail

guard="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/job-started-guard.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

failures=0

# expect <pass|deny> <event name> <payload json or @missing> <description>
expect() {
  local want="$1" event="$2" payload="$3" desc="$4" path="$tmp/event.json" got
  rm -f "$path"
  if [[ "$payload" != "@missing" ]]; then
    printf '%s' "$payload" >"$path"
  fi
  if GITHUB_EVENT_NAME="$event" GITHUB_EVENT_PATH="$path" bash "$guard" >"$tmp/out" 2>&1; then
    got=pass
  else
    got=deny
  fi
  if [[ "$got" == "$want" ]]; then
    printf 'ok    %-5s %s\n' "$want" "$desc"
  else
    printf 'FAIL  want %s got %s: %s\n' "$want" "$got" "$desc"
    sed 's/^/      /' "$tmp/out"
    failures=$((failures + 1))
  fi
  if [[ "$got" == deny ]] && ! grep -q '^::error::' "$tmp/out"; then
    printf 'FAIL  deny without ::error:: line: %s\n' "$desc"
    failures=$((failures + 1))
  fi
}

base='"repository":{"full_name":"jlengelbrecht/ai-control-plane"}'
same='"pull_request":{"head":{"repo":{"full_name":"jlengelbrecht/ai-control-plane"}}}'
fork='"pull_request":{"head":{"repo":{"full_name":"someone/ai-control-plane"}}}'
gone='"pull_request":{"head":{"repo":null}}'
run_same='"workflow_run":{"head_repository":{"full_name":"jlengelbrecht/ai-control-plane"}}'
run_fork='"workflow_run":{"head_repository":{"full_name":"someone/ai-control-plane"}}'
run_gone='"workflow_run":{"head_repository":null}'

expect pass push "{$base}" "push"
expect pass workflow_dispatch "{$base}" "workflow_dispatch"
expect pass schedule "{$base}" "schedule"
expect pass merge_group "{$base}" "merge_group"
expect pass release "{$base}" "release"
expect pass workflow_run "{$base,$run_same}" "same-repo workflow_run"
expect pass pull_request "{$base,$same}" "same-repo pull_request"
expect pass pull_request_review "{$base,$same}" "same-repo pull_request_review"
expect pass pull_request_review_comment "{$base,$same}" "same-repo pull_request_review_comment"

expect deny pull_request_target "{$base,$same}" "pull_request_target from same repo"
expect deny pull_request_target "{$base,$fork}" "pull_request_target from fork"
expect deny pull_request "{$base,$fork}" "fork pull_request"
expect deny pull_request_review "{$base,$fork}" "fork pull_request_review"
expect deny pull_request_review_comment "{$base,$fork}" "fork pull_request_review_comment"
expect deny pull_request "{$base,$gone}" "deleted-fork pull_request (head repo null)"
expect deny pull_request "{$base}" "pull_request without pull_request object"
expect deny workflow_run "{$base,$run_fork}" "workflow_run from a fork head"
expect deny workflow_run "{$base,$run_gone}" "workflow_run with null head_repository"
expect deny workflow_run "{$base}" "workflow_run without workflow_run object"
expect deny issue_comment "{$base,$same}" "issue_comment"
expect deny issues "{$base}" "issues"
expect deny repository_dispatch "{$base}" "repository_dispatch"
expect deny some_future_event "{$base}" "unknown event"
expect deny push "@missing" "missing payload file"
expect deny push "not json" "unparseable payload"
expect deny push '[]' "payload is not an object"
expect deny "" "{$base}" "empty GITHUB_EVENT_NAME"

if ((failures > 0)); then
  printf '%d failure(s)\n' "$failures"
  exit 1
fi
printf 'all cases passed\n'
