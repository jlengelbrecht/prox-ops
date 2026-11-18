#!/usr/bin/env bash
set -u

cadence="${AFK_POLL_CADENCE_SECONDS:-90}"
if [[ ! "$cadence" =~ ^[1-9][0-9]*$ ]]; then
  printf 'poll-supervisor: cadence must be a positive integer\n' >&2
  exit 2
fi
child=''
stopping=0
on_stop() {
  stopping=1
  if [[ -n "$child" ]]; then
    kill -TERM -- "-$child" 2>/dev/null || true
  fi
}
trap on_stop TERM INT

while (( ! stopping )); do
  started=$(date +%s)
  # setsid gives this pass its own process group. Never signal unrelated PIDs.
  setsid /opt/afk/bin/agent-flow-kit paperclip-poll >/dev/null 2>&1 &
  child=$!
  wait "$child"
  result=$?
  if (( stopping )); then
    # A trapped signal interrupts wait; reap the owned leader before exit.
    wait "$child" 2>/dev/null || true
  fi
  child=''
  printf 'poll-supervisor: pass_exit=%d at=%s\n' "$result" "$(date -u +%FT%TZ)" >&2
  (( stopping )) && break
  now=$(date +%s)
  delay=$((cadence - (now - started)))
  if (( delay > 0 )); then
    setsid sleep "$delay" &
    child=$!
    wait "$child" || true
    if (( stopping )); then
      wait "$child" 2>/dev/null || true
    fi
    child=''
  fi
done
printf 'poll-supervisor: stopped\n' >&2
