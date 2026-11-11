"""Closed startup boundary for the offline, synthetic pinned-image fixture."""

import json
import os
from pathlib import Path
import sys

SCHEMA = "litellm-grok-image-result/2"
CLASSES = {"AssertionError", "ImportError", "ModuleNotFoundError", "RuntimeError",
           "SystemExit", "KeyboardInterrupt", "FileNotFoundError", "PermissionError",
           "ValueError", "TypeError", "KeyError", "AttributeError", "OSError"}


def _name(value):
    name = type(value).__name__
    return name if name in CLASSES else "OtherError"


def _write(path, stage, kind, predicate):
    record = {"schema": SCHEMA, "checks": {}, "failures": {}, "fatal": {
        "stage": stage, "class": kind, "predicate": predicate}}
    temporary = path.with_suffix(".bootstrap.tmp")
    with temporary.open("x") as handle:
        json.dump(record, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def main(path):
    stage = "bootstrap"
    try:
        _write(path, stage, "NotStarted", "fixture_not_started")
        stage = "imports"
        import grok_pinned_image_integration as fixture
        stage = "proxy_runtime"
        code = fixture.main(path)
        if not path.is_file():
            raise RuntimeError("fixture_receipt_missing")
        return code
    except BaseException as exc:
        kind = _name(exc)
        predicate = "fixture_exception" if stage != "bootstrap" else "receipt_unwritable"
        if stage != "bootstrap":
            try:
                _write(path, getattr(sys.modules.get("grok_pinned_image_integration"),
                                     "STAGE", stage), kind, predicate)
            except BaseException as write_exc:
                stage, kind, predicate = "receipt_write", _name(write_exc), "receipt_unwritable"
        return 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
