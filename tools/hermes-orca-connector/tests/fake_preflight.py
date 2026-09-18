#!/usr/bin/env python3
"""Harmless synthetic preflight. Stands in for the real secret guard in tests.

Exit code comes from ``FAKE_PREFLIGHT_EXIT`` (default 0 = PASS). Appends its
argv to ``FAKE_PREFLIGHT_LOG`` so tests can assert it ran before every command
with the exact expected arguments.
"""

import json
import os
import sys

log = os.environ.get("FAKE_PREFLIGHT_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")
code = int(os.environ.get("FAKE_PREFLIGHT_EXIT", "0"))
print("result=%s" % ("PASS" if code == 0 else "BLOCKED"))
sys.exit(code)
