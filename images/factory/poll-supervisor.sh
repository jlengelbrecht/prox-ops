#!/usr/bin/env bash
set -u

cadence="${AFK_POLL_CADENCE_SECONDS:-90}"
# Compare decimal text before Bash arithmetic can overflow.
if [[ ! "$cadence" =~ ^[1-9][0-9]*$ ]] ||
   (( ${#cadence} > 5 )) ||
   { (( ${#cadence} == 5 )) && [[ "$cadence" > 86400 ]]; }; then
  printf 'poll-supervisor: cadence must be a positive integer at most 86400\n' >&2
  exit 2
fi
child=''
group_ready=0
stopping=0
waiting=0
leader_start=''
stop_sent=0
stop_forced=0
stop_failure=0
declare -A owned_members=()
declare -a group_members=()
group_uncertain=0
# Ordinary passes have no deadline. Cancellation gives TERM three seconds,
# then KILL only for members whose original group identity is still proved.
stop_grace_seconds=3
# /proc/uptime is kernel monotonic time; wall-clock changes must not extend
# cancellation. Read milliseconds so the full grace period is observed.
monotonic_now=0
read_monotonic() {
  local uptime rest seconds fraction
  read -r uptime rest < /proc/uptime || return 1
  [[ "$uptime" =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
  seconds=${BASH_REMATCH[1]}
  fraction=${BASH_REMATCH[2]}000
  monotonic_now=$((10#$seconds * 1000 + 10#${fraction:0:3}))
}
on_stop() {
  stopping=1
  # Keep repeated signals from interrupting the final ownership wait.
  trap '' TERM INT
  stop_owned
}
trap on_stop TERM INT

stop_owned() {
  (( stop_sent )) && return 0
  if owned_leader; then
    # Record member PID/start-time pairs while the original leader anchors the
    # group. Bash may reap that leader before its descendants have exited.
    if ! scan_owned_group; then
      stop_failure=1
      return 1
    fi
    local pid
    for pid in "${group_members[@]}"; do
      if member_current "$pid" "${owned_members[$pid]}"; then
        kill -TERM -- "$pid" 2>/dev/null || true
      fi
    done
    stop_sent=1
    # The handshake may still have the group leader stopped.
    kill -CONT -- "$child" 2>/dev/null || true
  fi
}

member_current() {
  local pid=$1 expected=$2 stat rest
  local -a fields
  [[ -r "/proc/$pid/stat" ]] || return 1
  stat=$(<"/proc/$pid/stat") || return 1
  rest=${stat##*) }
  read -r -a fields <<< "$rest"
  [[ "${fields[2]:-}" == "$child" && "${fields[3]:-}" == "$child" &&
     "${fields[19]:-}" == "$expected" && "${fields[0]:-}" != Z* ]]
}

scan_owned_group() {
  local path pid stat rest start anchor=0
  local -a fields candidates=()
  group_members=()
  group_uncertain=0
  if owned_leader; then anchor=1; fi
  for pid in "${!owned_members[@]}"; do
    if member_current "$pid" "${owned_members[$pid]}"; then anchor=1; fi
  done
  for path in /proc/[0-9]*/stat; do
    pid=${path%/stat}; pid=${pid##*/}
    if ! stat=$(<"$path") 2>/dev/null; then
      [[ -e "/proc/$pid" ]] && group_uncertain=1
      continue
    fi
    rest=${stat##*) }
    read -r -a fields <<< "$rest"
    [[ "${fields[2]:-}" == "$child" && "${fields[3]:-}" == "$child" &&
       "${fields[0]:-}" != Z* ]] || continue
    start=${fields[19]:-}
    if [[ "$pid" == "$child" ]]; then
      [[ "$start" == "$leader_start" ]] || { group_uncertain=1; continue; }
    elif [[ -n "${owned_members[$pid]:-}" ]]; then
      [[ "$start" == "${owned_members[$pid]}" ]] || { group_uncertain=1; continue; }
    elif (( ! anchor )); then
      # Without a surviving known member, a reused group ID is possible.
      group_uncertain=1
      continue
    fi
    candidates+=("$pid")
    owned_members[$pid]=$start
  done
  for pid in "${candidates[@]}"; do
    if member_current "$pid" "${owned_members[$pid]}"; then
      group_members+=("$pid")
    fi
  done
  (( ! group_uncertain ))
}

owned_leader() {
  [[ -n "$child" && "$group_ready" == 1 && -n "$leader_start" ]] || return 1
  local stat rest
  [[ -r "/proc/$child/stat" ]] || return 1
  stat=$(<"/proc/$child/stat") || return 1
  rest=${stat##*) }
  local -a fields
  read -r -a fields <<< "$rest"
  # /proc stat fields 5, 6, 22: process group, session, start ticks.
  [[ "${fields[2]:-}" == "$child" &&
     "${fields[3]:-}" == "$child" &&
     "${fields[19]:-}" == "$leader_start" &&
     "${fields[0]:-}" != Z* ]]
}

launch_owned() {
  # The new session pauses before running the CLI. Verify its process group
  # while the leader is stopped; never signal before ownership proof.
  # Keep the group leader alive after the CLI exits until every in-group
  # descendant is inactive. Thus the next pass cannot outrun background work.
  setsid bash -c '
    trap "exit 143" TERM INT
    kill -STOP "$$"
    "$@" & job=$!
    wait "$job"; result=$?
    while :; do
      active=0
      for path in /proc/[0-9]*/stat; do
        pid=${path%/stat}; pid=${pid##*/}
        [[ "$pid" == "$$" ]] && continue
        stat=$(<"$path") 2>/dev/null || continue
        rest=${stat##*) }
        read -r -a fields <<< "$rest"
        if [[ "${fields[2]:-}" == "$$" && "${fields[3]:-}" == "$$" &&
              "${fields[0]:-}" != Z* ]]; then active=1; break; fi
      done
      (( active )) || break
      sleep 0.05
    done
    exit "$result"
  ' _ "$@" >/dev/null 2>&1 &
  child=$!
  group_ready=0
  leader_start=''
  stop_sent=0
  owned_members=()
  local state pgid stat rest launch_start='' cancel_deadline=-1
  local -a fields
  while :; do
    pgid=''; state=''
    read -r pgid state < <(ps -o pgid=,stat= -p "$child" 2>/dev/null) || true
    if [[ -n "$state" && -z "$launch_start" && -r "/proc/$child/stat" ]]; then
      stat=$(<"/proc/$child/stat") || stat=''
      rest=${stat##*) }
      read -r -a fields <<< "$rest"
      launch_start=${fields[19]:-}
    fi
    if [[ "$pgid" == "$child" && "$state" == T* ]]; then
      stat=$(<"/proc/$child/stat") || stat=''
      rest=${stat##*) }
      read -r -a fields <<< "$rest"
      leader_start=${fields[19]:-}
      if [[ -z "$leader_start" || ( -n "$launch_start" && "$leader_start" != "$launch_start" ) ]]; then
        printf 'poll-supervisor: launch_identity_unverified\n' >&2
        stop_failure=1
        return 1
      fi
      group_ready=1
      break
    fi
    if [[ "$state" == Z* || -z "$state" ]]; then
      wait "$child" 2>/dev/null || true
      child=''
      return 1
    fi
    if (( stopping )); then
      if ! read_monotonic; then
        printf 'poll-supervisor: monotonic_clock_unavailable\n' >&2
        stop_failure=1
        return 1
      fi
      if (( cancel_deadline < 0 )); then
        cancel_deadline=$((monotonic_now + stop_grace_seconds * 1000))
      elif (( monotonic_now >= cancel_deadline )); then
        # No group proof exists yet. Only the original direct child may be
        # signaled; never guess a negative PID at this phase.
        if [[ -n "$launch_start" && -r "/proc/$child/stat" ]]; then
          stat=$(<"/proc/$child/stat") || stat=''
          rest=${stat##*) }
          read -r -a fields <<< "$rest"
          if [[ "${fields[19]:-}" == "$launch_start" ]]; then
            kill -KILL -- "$child" 2>/dev/null || true
          fi
        fi
        printf 'poll-supervisor: cancellation_startup_unverified\n' >&2
        stop_failure=1
        return 1
      fi
    fi
    sleep 0.01
  done
  if (( stopping )); then stop_owned
  else kill -CONT -- "$child" 2>/dev/null || true
  fi
}

cancel_owned() {
  local deadline pid
  stop_owned
  if ! read_monotonic; then
    printf 'poll-supervisor: monotonic_clock_unavailable\n' >&2
    return 1
  fi
  deadline=$((monotonic_now + stop_grace_seconds * 1000))
  while :; do
    if ! scan_owned_group; then
      printf 'poll-supervisor: cancellation_ownership_uncertain\n' >&2
      return 1
    fi
    (( ${#group_members[@]} == 0 )) && break
    if ! read_monotonic; then return 1; fi
    if (( monotonic_now >= deadline )); then
      for pid in "${group_members[@]}"; do
        if member_current "$pid" "${owned_members[$pid]}"; then
          kill -KILL -- "$pid" 2>/dev/null || true
        fi
      done
      stop_forced=1
      printf 'poll-supervisor: cancellation_escalated=kill\n' >&2
      break
    fi
    sleep 0.05
  done
  # Check the whole group after escalation, including descendants whose
  # leader has exited. A zombie is not an active member.
  if ! read_monotonic; then return 1; fi
  deadline=$((monotonic_now + 2000))
  while :; do
    if ! scan_owned_group; then
      printf 'poll-supervisor: cancellation_ownership_uncertain\n' >&2
      return 1
    fi
    (( ${#group_members[@]} == 0 )) && break
    if ! read_monotonic; then return 1; fi
    if (( monotonic_now >= deadline )); then
      printf 'poll-supervisor: cancellation_group_active\n' >&2
      return 1
    fi
    sleep 0.05
  done
}

reap_owned() {
  local result=127 interrupted=0 first_wait_done=0
  if (( ! stopping )); then
    waiting=1
    wait "$child"
    result=$?
    first_wait_done=1
    waiting=0
    # The leader's natural exit is a group-drain barrier. Verify the result
    # before dropping the identity and allowing another pass.
    if (( ! stopping )); then
      if ! scan_owned_group || (( ${#group_members[@]} != 0 )); then
        printf 'poll-supervisor: pass_group_unverified\n' >&2
        stop_failure=1
        return 1
      fi
    fi
  fi
  if (( stopping )); then
    if ! cancel_owned; then stop_failure=1; return 1; fi
    if (( first_wait_done && result <= 128 )); then
      : # The first wait already returned the actual child status.
    else
      wait "$child"
      result=$?
      (( result == 127 )) && interrupted=1
    fi
  fi
  if (( interrupted )); then
    printf 'poll-supervisor: pass_interrupted at=%s\n' "$(date -u +%FT%TZ)" >&2
  else
    printf 'poll-supervisor: pass_exit=%d at=%s\n' "$result" "$(date -u +%FT%TZ)" >&2
  fi
  child=''
  group_ready=0
  leader_start=''
  stop_sent=0
  owned_members=()
}

while (( ! stopping )); do
  started=$(date +%s)
  (( stopping )) && break
  if ! launch_owned /opt/afk/bin/agent-flow-kit paperclip-poll; then
    (( stopping )) && break
    printf 'poll-supervisor: pass_interrupted at=%s\n' "$(date -u +%FT%TZ)" >&2
    break
  fi
  if ! reap_owned; then break; fi
  (( stopping )) && break
  now=$(date +%s)
  (( stopping )) && break
  delay=$((cadence - (now - started)))
  if (( delay > 0 )); then
    (( stopping )) && break
    if launch_owned sleep "$delay"; then
      if ! reap_owned; then break; fi
    fi
  fi
done
printf 'poll-supervisor: stopped\n' >&2
if (( stop_forced || stop_failure )); then
  printf 'poll-supervisor: stop_outcome=forced_or_unreaped\n' >&2
  exit 1
fi
