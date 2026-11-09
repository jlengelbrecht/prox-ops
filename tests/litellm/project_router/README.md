# Project router focused tests

Requires Python 3 and PyYAML 6.0.3 (the version used by repository CI). From the repository root, use a local virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'PyYAML==6.0.3'
.venv/bin/python -m unittest discover -s tests/litellm/project_router -p 'test_*.py' -v
```

The tests use synthetic credentials and make no provider or cluster calls. Historical pinned-image qualification is described in the SUB164 summary; it is not a test prerequisite.

The launcher supports the native Claude installation at `~/.local/bin/claude` pointing to a regular executable under `~/.local/share/claude/versions/<version>`, or that version path directly. The home and installation directories must be owned by the current user and not group or world writable. It resolves the installed version before the auth diagnostic and uses that same canonical executable for smoke and interactive launch. Relative PATH entries, absolute project executables, and symlinks into projects fail preflight. Install or update the native CLI in this layout before using the launcher; arbitrary npm or project-local Claude wrappers are not accepted.

The launcher smoke test accepts only the committed `smoke-fixture.txt` and its exact cedar proof content, from a working directory containing that file. Claude Code 2.1.319 documents that `--print` skips the workspace trust dialog. The smoke command uses `--safe-mode`, `--setting-sources user`, and `--strict-mcp-config` to exclude project startup customization while preserving native authentication. Its `Read` proof and terminal result are checked in the bounded stream. Normal interactive use keeps native settings, hooks, plugins, MCP, and login behavior. Launch it from a real terminal so Claude Code can show its native workspace trust dialog. The launcher refuses an interactive start when stdout is not a TTY because Claude Code also skips the dialog in that case. Trusting a workspace authorizes its project configuration to run with the launcher child environment, including the gateway header. Project settings `env` entries fail preflight unless explicitly allowed; the current allowlist is empty. User-global settings retain the existing provider and credential override checks.
