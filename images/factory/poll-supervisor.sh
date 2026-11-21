#!/usr/bin/env bash
# Compatible entrypoint. Linux child ownership lives in the Python helper.
exec /usr/bin/python3 -I /opt/factory/poll-supervisor.py "$@"
