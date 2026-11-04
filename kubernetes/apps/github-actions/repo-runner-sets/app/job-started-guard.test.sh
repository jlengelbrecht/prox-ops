#!/usr/bin/env bash
# Local self-test for job-started-guard.sh. Not deployed (not listed in
# kustomization.yaml). Needs bash and jq.
#   bash kubernetes/apps/github-actions/repo-runner-sets/app/job-started-guard.test.sh
set -euo pipefail

guard="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/job-started-guard.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

failures=0

# The guard kills the nearest ancestor named Runner.Worker on deny, exactly as
# in the runner pod. Each case therefore runs the guard under a stand-in worker
# (a copy of bash named Runner.Worker) that, like the real StepsRunner, goes on
# to run a later `if: always()` step after the hook step ends. The marker file
# is that later step: a pass must reach it, a deny must not.
mkdir "$tmp/bin"
cp "$(command -v bash)" "$tmp/bin/Runner.Worker"

# expect <pass|deny> <event name> <payload json or @missing> <description>
expect() {
  local want="$1" event="$2" payload="$3" desc="$4" path="$tmp/event.json" got
  rm -f "$path" "$tmp/always-step-ran"
  if [[ "$payload" != "@missing" ]]; then
    printf '%s' "$payload" >"$path"
  fi
  # Run in a subshell so bash's own "Killed" job message stays out of the output.
  {
    (
      export GITHUB_EVENT_NAME="$event" GITHUB_EVENT_PATH="$path" GUARD="$guard" OUT="$tmp/out" MARK="$tmp/always-step-ran"
      "$tmp/bin/Runner.Worker" -c 'bash "$GUARD" >"$OUT" 2>&1; echo "hook exit $?" >>"$OUT"; touch "$MARK"'
    ) || true
  } 2>/dev/null
  if [[ -e "$tmp/always-step-ran" ]]; then
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

# Without a Runner.Worker or Runner.Listener ancestor the guard cannot kill
# anything; it still exits 1 with an error line. Not reachable inside a runner.
rm -f "$tmp/event.json"
if GITHUB_EVENT_NAME=push GITHUB_EVENT_PATH="$tmp/event.json" bash "$guard" >"$tmp/out" 2>&1; then
  printf 'FAIL  no-worker deny exited 0\n'
  failures=$((failures + 1))
elif grep -q '^::error::' "$tmp/out"; then
  printf 'ok    deny  no worker ancestor: exits 1 with ::error::\n'
else
  printf 'FAIL  no-worker deny without ::error:: line\n'
  failures=$((failures + 1))
fi

if ((failures > 0)); then
  printf '%d failure(s)\n' "$failures"
  exit 1
fi
printf 'all cases passed\n'
