# Launching the `claude` harness under Orca

Frozen by STORY-035-17′ against live validation on the devbox workstation. This file records
how the `{claude, claude/strong}` execution decision is actually launched, and the auth and
permission posture it was proven under. `ADE-BOUNDARY.md` describes who owns the seam; this
describes what the ADE does on the Claude side of it.

Everything below was executed and captured. Where something was reasoned but not run, it says
so. A tested version is not a minimum version, and this file does not invent floors it did not
measure.

---

## 1. Tested versions

- **Orca 1.4.187**
- **Claude Code 2.1.241**

These are the versions the validation ran against. **Neither is a floor.** One successful
version establishes that the path works there, not that it fails below or holds above. A floor
may only be written here by evidence that actually probes one.

---

## 2. Native Anthropic subscription authentication is required

Claude Code runs as a native CLI carrying the operator's own Anthropic Max subscription
session. The launch path must reach a live subscription-authenticated session, and nothing else.

Unlike Codex, this CLI exposes a usable non-secret auth-mode indicator. Two independent
surfaces were captured on the validated host:

| Surface | Field | Observed |
|---|---|---|
| `claude auth status` | `authMethod` | `claude.ai` |
| `claude auth status` | `apiProvider` | `firstParty` |
| `claude auth status` | `subscriptionType` | `max` |
| `--output-format stream-json` init event | `apiKeySource` | `none` |
| `--output-format json` result | `modelUsage[].provider` | `firstParty` |

`apiKeySource: none` is the closest Claude analogue to the Codex endpoint signal: it states
positively that no API key was used to authenticate the run. Combined with `authMethod:
claude.ai` and `subscriptionType: max`, this is what distinguishes the subscription path from
an API-billed one.

Corroborating, from Orca's own view of the same credential store: `orca account list --json`
reported the subscription session counter moving (5% to 6%) across the validation turns while
the weekly counter held, with `credentialSource: credentials-file` and `authProvenance:
system`. Subscription quota was consumed, not API credit.

### `claude auth status` is still not a liveness check

This is the operational finding carried over from the Codex validation, and it survives
contact with a better indicator.

`claude auth status` reads a stored credential record. It reports what kind of credential
exists and who it belongs to. It does not prove that record still works — an expired or
revoked session can still describe itself accurately. The Codex lesson was that a
status command waved a dead session into a run; nothing about Claude's richer output
changes the shape of that risk.

**A preflight that gates on `claude auth status` can wave a dead session straight into a run.**
A launcher has exactly two honest options:

1. probe with a real, trivial turn and treat its exit status as the gate; or
2. skip the preflight and accept the auth failure at launch as the failure path (§8).

Liveness in this validation was established by option 1: a real turn (`claude -p "Reply with
exactly one word: alive"`) that returned `alive` with `is_error: false`. Use `auth status` to
confirm the auth *mode* is subscription rather than API key. Do not use it to confirm the
session is alive.

---

## 3. Credential ownership

**Native Claude Code and Orca own Claude credential storage.** prox-ops never copies, edits,
centralizes, inspects or transports it. No component in this repository reads an auth file, and
no contract shape can carry what one contains.

On the validated host the credential store was `~/.claude/.credentials.json`, mode `0600`. Its
existence and mode were checked; its contents were never read.

Claude Code scopes its configuration from `CLAUDE_CONFIG_DIR`, falling back to the default
`~/.claude` when that variable is unset.

**On this host there is exactly one store.** `orca account list --json` reported zero managed
Claude accounts (`claude.accounts: []`, `activeAccountId: null`) and resolved rate limits from
the system credentials file (`authProvenance: system`). The two-store divergence that bit the
Codex validation — where Orca's built-in launcher used its own managed home and stayed
unauthenticated after the operator logged into the default home — **does not currently exist for
Claude here.**

It can be created. `orca account add` exists and manages Claude accounts. The moment someone
adds one, Orca's launcher path and the native default home become independent stores, and
authenticating one will not authenticate the other. The rule this contract freezes is therefore
the same one the Codex contract freezes, stated so it holds either way: *the launch path uses
whatever config directory its process inherits, and whoever operates that path is responsible
for that specific store being live.* Orca's internal directory layout is Orca's, may change, and
is not an interface. Do not hard-code it.

### A logging hazard for launcher authors

`orca repo list --json` and `orca repo show --json` include the repo's
`gitRemoteIdentity.remoteUrl`, and on this host that URL embeds a GitHub access token. A
launcher that logs, echoes or stores raw `orca repo` JSON will write a live credential to disk.
Read the specific fields needed (`id`, `hookSettings`) and never persist the whole object.

---

## 4. Model and effort translation

The catalog decision is translated to native Claude Code arguments. It is **always passed
explicitly** and never inherited from the operator's global Claude configuration — a launch that
relies on Claude defaults is not executing the stamped decision.

Catalog `claude/strong` → physical `claude/opus` → native `--model` argument **`opus`**.

The adapter strips the `claude/` provider qualifier because `--model` names a model, not a
provider-qualified id. The provider-qualified form remains the authoritative catalog data; the
strip is a translation at the CLI boundary and nowhere else. `claude --help` documents `opus` as
an alias for the latest model of that class. The validated run resolved it to
`canonicalModel: claude-opus-5`, which is what the alias pointed at on the tested date — the
contract pins the alias, not that resolution.

**Inheritance is a live hazard here, not a theoretical one.** Two defaults were found set on the
validated host that would have silently supplied these values had they not been passed
explicitly:

- `~/.claude/settings.json` declares a `model` key.
- `CLAUDE_EFFORT` was **set in the ambient launch environment**.

Both are neutralized by §7's sanitizer plus explicit arguments. Neither file was modified.

Effort maps onto the native `--effort` flag:

| Catalog effort | Claude `--effort` |
|---|---|
| `low` | `low` — exercised as a comparison baseline |
| `medium` | `medium` |
| `high` | `high` — **tested** |
| `xhigh` | `xhigh` |
| — | `max` (accepted by the CLI; no catalog effort maps to it) |

The accepted set is `low, medium, high, xhigh, max`, enumerated by the CLI itself. Only `high`
was exercised as the stamped effort. `medium`, `xhigh` and `max` are the frozen mapping, not
measured behaviour, and are **UNVERIFIED**.

Effort was confirmed to take effect, not merely to be accepted: the identical prompt on the
identical model produced 46 thinking tokens at `--effort low` and 195 at `--effort high`.

**The effective effort of a run is not reported anywhere.** The `stream-json` init event carries
`model`, `cwd`, `permissionMode` and `apiKeySource`, but no effort field, and neither does the
`json` result. There is no post-hoc way to prove which effort a completed run used. This is a
gap in the evidence surface and it is why the next section matters.

### `--effort` fails open

An unrecognized `--effort` value does **not** stop the run. It prints

> `Warning: Unknown --effort value 'banana' — ignoring it and using the default effort. Valid values: low, medium, high, xhigh, max.`

to stderr, silently substitutes the default effort, completes the turn, and exits `0`.

A launcher that passes a typo, a stale value, or an effort this CLI version has dropped will get
a clean, successful-looking run **at the wrong effort**, with nothing in the machine-readable
output to reveal it. The launcher must therefore validate the effort string against the
enumerated set before launching, and should treat that stderr warning as a failure if it appears.

`--model` behaves the opposite way and fails closed: an unrecognized model exits `1` with
`unrecognized_model` and `api_error_status: 404`, having consumed no tokens. Do not generalize
the model behaviour to effort.

`~/.claude/settings.json` is never modified. Model and effort are set per invocation.

---

## 5. The launch sequence

Orca owns the worktree and the terminal; Claude Code runs natively inside them. Step 0 is the
setup-policy gate — it runs BEFORE anything is created, because a repository wait-for-setup
policy makes the whole path unsafe and stopping after worktree-create is already too late. The
terminal command records the Claude exit status in-band, because `orca terminal wait --for exit`
does not fire (the terminal outlives its command) — the `CLAUDE_EXIT=` line is the launcher's
only reliable completion signal.

```shell
# 0. setup-policy gate: stop unless the repo starts immediately
orca repo list --json    # read hookSettings only; wait-for-setup => STOP
                         # do not log the raw object (§3 logging hazard)

# 1-2. lifecycle + sanitized, fail-closed launch
orca worktree create --repo id:<repoId> --name <task> \
  --parent-worktree worktree:<callerWorktreeId> --json
# task text is written to a file by the LAUNCHER, never interpolated into
# the shell command: a double quote or shell metacharacter inside <task>
# would otherwise execute in the terminal shell, outside claude's
# restricted tool set, and could forge the CLAUDE_EXIT line.
printf '%s' <task> > <worktreePath>/.task-prompt   # launcher-side write, argv-safe

orca terminal create --worktree id:<repoId>::<worktreePath> --title <taskTitle> \
  --command 'env -i HOME="<intendedHome>" PATH="$PATH" USER="$USER" LANG=C.UTF-8 TERM=dumb \
    claude -p "$(cat .task-prompt)" --model opus --effort high \
      --permission-mode acceptEdits --tools "Read,Glob,Grep,Write" \
      --strict-mcp-config --output-format json < /dev/null; \
    echo "CLAUDE_EXIT=$?"'
```

`<intendedHome>` is the explicit, launcher-configured home whose `.claude` tree carries the
credential store this launch is meant to use — never the ambient `$HOME` passed through
blind. `env -i` strips `CLAUDE_CONFIG_DIR`, so whatever `HOME` survives silently selects the
store; pinning it makes the credential-store choice an explicit launcher decision instead of
an inherited accident, and keeps the §3 store-per-path rule enforceable. The command-substituted
`.task-prompt` read happens inside single quotes, so the task text itself is never parsed by
the outer shell.

**Why the two-step custom-command shape rather than `--agent claude`:** `orca worktree create`
accepts only `--agent <id>` and `--prompt <text>`. Its full option list, read from the
version-matched CLI's own help output, contains no `--model`, no `--effort`, and no passthrough
for agent-specific arguments. Since explicit model and effort are mandatory, the built-in
launcher cannot carry the stamped decision and the custom-command path is required. This was
checked directly for Claude rather than assumed from the Codex result — the conclusion happens to
match, but the evidence is Claude's own. This is still Orca lifecycle ownership: Orca creates and
owns both the worktree and the terminal, and reaps the process.

**Setup ordering must be checked before using this path.** Orca cannot preserve a repository
wait-for-setup startup policy across worktree-create followed by a later custom-argv
terminal-create. Read the repo's setup policy first; if it waits for setup, this path is unsafe
and the launch must stop rather than bypass setup ordering. On the validated repo the policy was
re-verified on the day of validation as `setupAgentStartupPolicy: start-immediately` with empty
setup scripts, so no conflict existed.

Three details that cost time if you skip them:

- `--parent-worktree active` resolves against the **caller's** current directory and fails with
  `selector_not_found` unless that directory is itself inside an Orca-managed worktree. A plain
  `git worktree` is not. Pass an explicit `worktree:<id>` selector or `--no-parent`.
- Bare `worktree create` (no `--agent`) opens a fallback shell when the repo configures no
  default terminal. The validated worktree ended with four terminals: three launched here plus
  that shell. Expect it; it is reaped with the worktree.
- Redirect stdin. Without `< /dev/null`, `claude -p` waits and then warns
  `no stdin data received in 3s, proceeding without it`.

### Working directory

Claude must run with its working directory inside the Orca worktree. The tested path satisfies
this because an Orca terminal starts in its worktree; Claude reported
`cwd: <the Orca worktree path>` in its own `stream-json` init event, and the terminal's own
`pwd` agreed. A launcher that cannot show this has not met the requirement.

---

## 6. Permission posture

The tested and frozen posture is:

```text
--permission-mode acceptEdits
--tools "Read,Glob,Grep,Write"
--strict-mcp-config
```

This was established by testing the rung below it rather than by assumption, and the result
changed what this section had to say.

### Permission mode alone is not a write barrier

The first rung tested was `--permission-mode default` — the most restrictive non-interactive
mode — with no tool restriction. The permission system did its job at the level it operates:
the `Write` tool call was denied, and a Bash `>` redirection to the same path was denied.

**The file was created anyway.** The agent reached the same effect through `tee`, which no rule
prohibited. `permission_denials` listed the two blocked calls, `is_error` was `false`, and the
process exited `0`. The filesystem was checked independently: `spike-hello.md` existed with the
expected contents.

The lesson is that permission modes deny individual tool calls; they do not constrain the set of
effects reachable from the tools that remain. A general-purpose shell is a general-purpose write
primitive, and leaving `Bash` enabled while denying `Write` restricts nothing in practice. **Do
not treat a restrictive `--permission-mode` as a containment boundary.**

Containment came from restricting the tool set. Under `--tools "Read,Glob,Grep"` the same task
reached the model and honestly declined:

> I can't create the file — the `Write` tool is disabled for this session, and there's no
> Bash/Edit tool available either. [...] I'm not going to route around a deliberately disabled
> tool. Nothing was created in `<worktree>`.

No file was created, verified independently. `permission_denials` was empty — the tools were
never available to be denied.

The frozen posture is the minimum step up from there that lets the task do its work
deliberately: `Write` granted explicitly, `Bash` withheld, `acceptEdits` so the grant does not
need an interactive approver. Under it the task succeeded with zero permission denials.

### MCP servers are part of the capability surface

The declining run named an MCP tool (a browser automation tool capable of arbitrary code
execution) as an escape route it was choosing not to take. The init event showed 15 MCP servers
loaded from the operator's configuration.

A restricted `--tools` list does not remove MCP tools. Any launch claiming a least-privilege
posture must also pass `--strict-mcp-config` (with no `--mcp-config`) so the session starts with
no MCP servers at all. The frozen posture includes it.

### Bypass modes are forbidden

**`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, and
`--permission-mode bypassPermissions` are forbidden on this path.** A git worktree is not an
external sandbox and does not justify unrestricted host execution. If a task cannot complete
under a restricted tool set, that is a blocker to report, never a reason to weaken the posture.
A future externally sandboxed VM or container could revisit this under a different contract; it
does not apply to a harness running directly on the workstation.

**`--bare` is also forbidden, for a different reason.** Its own help states that under it
"Anthropic auth is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` via `--settings` (OAuth and
keychain are never read)." It disables the subscription path by construction. Never use it here.

---

## 7. API keys are forbidden — and the launch fails closed

The launch environment must not define an API key or any credential, endpoint, provider, model
or effort override, and no `apiKeyHelper` or settings `env` block may be configured.

The Codex validation's lesson was that the obvious variable is not the whole surface. On Claude
that lesson is larger by an order of magnitude. Reading the installed binary's own environment
strings rather than trusting memory, the surface that can redirect authentication, endpoint,
provider, model or effort away from the stamped subscription decision includes at least:

**Credential substitution** — `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`,
`CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`,
`ANTHROPIC_IDENTITY_TOKEN`, `ANTHROPIC_IDENTITY_TOKEN_FILE`, `CLAUDE_CODE_SESSION_ACCESS_TOKEN`,
`CLAUDE_CODE_HOST_AUTH_ENV_VAR`, `CLAUDE_CODE_HOST_CREDS_FILE`.

**Endpoint redirect** — `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_HOST`, `CLAUDE_CODE_API_BASE_URL`,
`CLAUDE_CODE_PROXY_URL`, `CLAUDE_CODE_HTTP_PROXY`, `CLAUDE_CODE_HTTPS_PROXY`,
`ANTHROPIC_UNIX_SOCKET`, `CLAUDE_CODE_USE_GATEWAY`.

**Third-party provider redirect** — `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
`CLAUDE_CODE_USE_FOUNDRY`, `CLAUDE_CODE_USE_MANTLE`, `CLAUDE_CODE_USE_ANTHROPIC_AWS`,
`CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD`, and their `*_API_KEY` / `*_BASE_URL` siblings.

**Model redirect** — `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL` and its sonnet/haiku/fable
counterparts, `ANTHROPIC_CUSTOM_MODEL_OPTION`, `ANTHROPIC_SMALL_FAST_MODEL`,
`CLAUDE_CODE_SUBAGENT_MODEL`.

**Effort redirect** — `CLAUDE_CODE_EFFORT_LEVEL`, `CLAUDE_EFFORT`.

**Config-scope redirect** — `CLAUDE_CONFIG_DIR`, `ANTHROPIC_CONFIG_DIR`,
`CLAUDE_CODE_MANAGED_SETTINGS_PATH`, `CLAUDE_CODE_REMOTE_SETTINGS_PATH`, `CLAUDE_ENV_FILE`,
`CLAUDE_SECURESTORAGE_CONFIG_DIR`.

**A denylist over this surface is the wrong instrument.** It is long, it spans several naming
conventions, and it grows with each release — a launcher pinned to today's `env -u` list will
silently stop being fail-closed the first time a new variable ships. The launcher therefore
sanitizes by **allowlist**, starting from an empty environment and admitting only what the
process needs:

```shell
env -i HOME="<intendedHome>" PATH="$PATH" USER="$USER" LANG=C.UTF-8 TERM=dumb claude ...
```

Verified: with this sanitizer, **zero** `ANTHROPIC_*` or `CLAUDE_*` variables survive into the
child, checked by name against a deliberately polluted parent environment. The same check
without the sanitizer showed all of them visible, including the `CLAUDE_EFFORT` that was
genuinely set on this host. `HOME` is deliberately present — it is what
carries the native credential store (§3) — but it is set EXPLICITLY to the launcher's intended
home, never passed through from the ambient environment: `env -i` removes `CLAUDE_CONFIG_DIR`,
so the surviving `HOME` alone selects the credential/settings tree, and an inherited `HOME`
would make that selection an accident.

Explicit `--model` and `--effort` arguments do NOT constrain auth or provider selection. The
command otherwise inherits the terminal environment and the effective settings files, and an
override there can redirect authentication away from the subscription session. That
`ANTHROPIC_BASE_URL` is honoured rather than ignored was observed directly: an unsanitized run
against an unreachable base URL did not fall through to the real endpoint — it hung retrying the
redirect until killed. The sanitized run of the same command reached the subscription normally.

The launcher must also refuse to launch when the effective Claude settings declare
`apiKeyHelper`, an `env` block, `awsAuthRefresh`, `gcpAuthRefresh`, or `forceLoginMethod`. On the
validated host none were present at any layer, and no managed or enterprise settings file existed
at `/etc/claude-code/managed-settings.json`. Requirement, not tested evidence: API-key-precedence
behaviour itself was not exercised and must never be.

Absence of `ANTHROPIC_API_KEY` was confirmed in the launch environment. It is a necessary check,
not a sufficient one — §2's `apiKeySource: none` is what actually evidences the subscription path.

Verified: with an isolated empty configuration directory and no API key, Claude Code does not
silently reach for a billing path. It reports `Not logged in · Please run /login`, exits `1`,
records `modelUsage: {}` and `total_cost_usd: 0`, and writes nothing. The isolated context was
created empty — never copied from the live store — and removed afterwards.

---

## 8. Exit semantics

**A zero exit means the turn completed, not that the task succeeded.** Both non-succeeding runs
in §6 exited `0`: the `--tools "Read,Glob,Grep"` run created no file, and the `default`-mode run
created one only by routing around two denied calls. A launcher that treats exit 0 as task
success will report false completions in both directions — missed work and unintended work.

Verify the intended effect — the expected diff, the expected artifact — separately from the exit
code. Every filesystem claim in this document was checked against the filesystem, not against
what the agent said it did. The exit code is only good for distinguishing a completed turn from
a failed one.

Two further traps in the machine-readable output:

- **`subtype` is not a status field.** The auth-failure run reported `subtype: "success"` while
  `is_error` was `true`. Gate on `is_error`, never on `subtype`.
- **Measure the exit code without a pipeline.** During this validation a
  `timeout ... | tail` construction reported the auth failure as exit 0; the same command with
  its status read directly reported exit 1. The pipeline was reading the wrong element. Capture
  `$?` from the `claude` process itself, which is exactly what the in-band `CLAUDE_EXIT=` line
  in §5 does.

Auth failure is honest and non-zero: exit `1`, with `Not logged in · Please run /login` in both
`text` and `json` output modes. It does not fall back, does not retry into a different credential
path, consumes no tokens, and does not partially apply work.

Practical note for launcher authors: on the tested Orca version, `orca terminal wait --for exit`
did not fire when the terminal's command finished, because the terminal outlives the command.
This was tested for Claude directly rather than inherited from the Codex result — the wait ran to
a full 150-second timeout against a command that had already completed. Capture the exit status
in-band rather than relying on that wait.

### Auth expiry and re-login

Not-logged-in, revoked, expired, or reauthentication-required means **the attempt fails and a
human is asked to re-login.** That is the whole behaviour.

Login is never automated, never scripted, and OAuth material is never copied between stores or
hosts. Re-login is an interactive human action taken outside the launcher, against the specific
store the launch path uses (§3).

---

## 9. Placement

`claude/strong` is vendor-hosted, so the profile carries **`placement_required: false`**. No
`/v1/place` call is made, no `x-placement` header exists, and no placement evidence is produced
or expected. Model-inference placement is about GPUs this path does not use.

---

## 10. Cleanup

A session's worktree and terminals are disposed through Orca's own lifecycle commands
(`orca worktree rm --worktree <selector> --force`, which reaps the terminals with it). After the
validation the removal was verified on every surface: the directory was gone, the worktree was
absent from `orca worktree list`, the terminal count for it was zero, no git branch remained, and
the git worktree registry had no entry. Any temporary configuration or home directory created for
testing is removed, and temporary contexts are created empty — never copied from a live store.

Validation output is not committed. The scratch artifact produced during this validation existed
only inside the disposable worktree and was destroyed with it.

---

## 11. What this document does not own

Stated explicitly so it cannot be read as more than it is. This file owns *how the claude harness
is launched and under what auth and permission posture*. It does **not** own:

- **routing** — which harness, model or effort is chosen. That is `agent-router`'s
  recommendation and the human's acceptance.
- **stamping** — recording the immutable decision onto a story.
- **final validation** — the pre-execution stamp validator. STORY-035-22 owns the real
  ordering: immutable stamp, optional placement, final validation, and only then launch. This
  document is downstream of all of it and never invokes it.
- **session scheduling** — when, whether or in what order sessions run.
- **login automation** — there is none, by design, and none may be added here.

---

## Sources

- STORY-035-17′ — the validation this file freezes; evidence captured live on devbox.
- `HARNESS-LAUNCH-codex.md` — the sibling contract whose shape and lessons this mirrors.
- `ADE-BOUNDARY.md` §3 — native harnesses own their own authentication, the statement this
  validation was run against.
- Orca's version-matched CLI guide (`orca skills get orca-cli`, v1.4.187) and the
  `orca worktree create --help` option list — the documented custom-command shape and the
  absence of any model/effort passthrough on `--agent`.
- `claude --help` and the installed 2.1.241 binary's own environment-variable strings — the
  override surface in §7, read rather than recalled.
