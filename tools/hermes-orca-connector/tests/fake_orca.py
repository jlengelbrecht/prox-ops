#!/usr/bin/env python3
"""Synthetic Orca CLI peer for tests. Never a real runtime.

Reads a scenario JSON file named by ``FAKE_ORCA_SCENARIO`` and answers one
command per invocation. Appends every argv it receives to ``FAKE_ORCA_LOG``
(one JSON line per call) so tests can assert call counts and exact flags, and
writes its pid to ``FAKE_ORCA_PIDFILE`` when set so reaping can be verified.

Scenario entry forms, keyed by the command path ("project list", ...):
  {"response": {...}, "exit": 0}          print the JSON, exit (default 0)
  {"mode": "hang"}                         sleep; the adapter must time out
  {"mode": "fork_hang"}                    spawn a sleeping descendant that keeps the
                                           pipes (its pid is recorded too), then hang
  {"mode": "flood", "stream": "stdout"|"stderr", "bytes": N}   oversized output
  {"mode": "garbage", "text": "..."}       non-JSON stdout
  {"select": "cursor"|"run"|"limit", "responses": {"<value>": entry, "": entry}}
  {"sequence": [entry, ...]}               nth call gets nth entry (last repeats)
"""

import json
import os
import sys
import time

KNOWN = {
    ("status",), ("agent-context",), ("host", "list"), ("project", "list"), ("repo", "list"),
    ("worktree", "list"), ("worktree", "ps"), ("terminal", "list"), ("orchestration", "run-list"),
    ("orchestration", "task-list"), ("orchestration", "worker-list"), ("orchestration", "gate-list"),
}


def main() -> int:
    pidfile = os.environ.get("FAKE_ORCA_PIDFILE")
    if pidfile:
        with open(pidfile, "a") as fh:
            fh.write("%d\n" % os.getpid())
    args = sys.argv[1:]
    command = tuple(args[:2]) if tuple(args[:2]) in KNOWN else tuple(args[:1])
    log = os.environ.get("FAKE_ORCA_LOG")
    prior = 0
    if log:
        if os.path.exists(log):
            with open(log) as fh:
                prior = sum(1 for line in fh if tuple(json.loads(line)[: len(command)]) == command)
        with open(log, "a") as fh:
            fh.write(json.dumps(args) + "\n")
    flags = {}
    rest = args[len(command):]
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("--"):
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                flags[tok[2:]] = rest[i + 1]
                i += 2
                continue
            flags[tok[2:]] = True
        i += 1
    with open(os.environ["FAKE_ORCA_SCENARIO"]) as fh:
        scenario = json.load(fh)
    entry = scenario.get(" ".join(command))
    if entry is None:
        print(json.dumps({"ok": False, "error": {"code": "unknown_command", "message": "x"}}))
        return 1
    return respond(entry, flags, prior)


def respond(entry: dict, flags: dict, nth: int) -> int:
    while "select" in entry or "sequence" in entry:
        if "sequence" in entry:
            seq = entry["sequence"]
            entry = seq[min(nth, len(seq) - 1)]
        else:
            value = flags.get(entry["select"], "")
            entry = entry["responses"].get(str(value), entry["responses"].get(""))
            if entry is None:
                print(json.dumps({"ok": False, "error": {"code": "unexpected_flag_value"}}))
                return 1
    mode = entry.get("mode")
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "fork_hang":
        child = os.fork()
        if child == 0:
            time.sleep(60)  # a helper holding stdout/stderr open after the adapter gave up
            return 0
        pidfile = os.environ.get("FAKE_ORCA_PIDFILE")
        if pidfile:
            with open(pidfile, "a") as fh:
                fh.write("%d\n" % child)
        time.sleep(60)
        return 0
    if mode == "flood":
        stream = sys.stderr if entry.get("stream") == "stderr" else sys.stdout
        chunk = ("CANARY_FLOOD_TOKEN_" + "x" * 1000) * 64
        written = 0
        while written < entry["bytes"]:
            stream.write(chunk)
            written += len(chunk)
        stream.flush()
        return 0
    if mode == "garbage":
        sys.stdout.write(entry["text"])
        return entry.get("exit", 0)
    sys.stdout.write(json.dumps(entry["response"]))
    return entry.get("exit", 0)


if __name__ == "__main__":
    sys.exit(main())
