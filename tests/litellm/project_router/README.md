# Project router focused tests

Requires Python 3 and PyYAML 6.0.3 (the version used by repository CI). From the repository root, use a local virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML==6.0.3'
.venv/bin/python -m unittest discover -s tests/litellm/project_router -p 'test_*.py' -v
```

The tests use synthetic credentials and make no provider or cluster calls. Historical pinned-image qualification is described in the SUB164 summary; it is not a test prerequisite.
