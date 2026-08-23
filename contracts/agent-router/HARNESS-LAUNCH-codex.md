# Launching the `codex` harness under Orca

Frozen by STORY-035-16′ against live validation on the devbox workstation. This file records
how the `{codex, openai/strong}` execution decision is actually launched, and the auth and
permission posture it was proven under. `ADE-BOUNDARY.md` describes who owns the seam; this
describes what the ADE does on the Codex side of it.

Everything below was executed and captured. Where something was reasoned but not run, it says
so. A tested version is not a minimum version, and this file does not invent floors it did not
measure.

---

## 1. Tested versions

- **Orca 1.4.187**
- **Codex CLI 0.149.0** (`codex-cli 0.149.0`)

These are the versions the validation ran against. **Neither is a floor.** One successful
version establishes that the path works there, not that it fails below or holds above. A floor
may only be written here by evidence that actually probes one.

---

## 2. Native ChatGPT authentication is required

Codex runs as a native CLI carrying the operator's own ChatGPT subscription session. The
launch path must reach a live ChatGPT-authenticated Codex session, and nothing else.

Confirmed live behaviour: an authenticated run transports over
`wss://chatgpt.com/backend-api/codex/responses` — the subscription endpoint. An unauthenticated
run falls through to `https://api.openai.com/v1/responses` and is rejected. The endpoint
difference is the direct signal that a run used the subscription and not API
billing.

### `codex login status` is not a liveness check

This is the key operational finding in this file.

`codex login status` reported `Logged in using ChatGPT` (exit 0) for two credential stores
that both failed on the first real turn with a spent refresh token. The command confirms that
a ChatGPT-mode credential record exists. It says nothing about whether that record still works.

**Any preflight that gates on `codex login status` will wave a dead session straight into a
run.** A launcher has exactly two honest options:

1. probe with a real, trivial turn and treat its exit status as the gate; or
2. skip the preflight and accept the 401-at-launch as the failure path (§8).

Choosing (2) is legitimate — the failure is honest and cheap. Choosing `login status` is not.

---

## 3. Credential ownership

**Native Codex and Orca own Codex credential storage.** prox-ops never copies, edits,
centralizes, inspects or transports it. No component in this repository reads an auth file, and
no contract shape can carry what one contains.

Codex resolves its credential store from `CODEX_HOME`, falling back to the Codex default when
that variable is unset. Two distinct stores existed on the validated host:

| Launch path | Store used |
|---|---|
| Orca terminal running custom `codex` argv (§5) | `CODEX_HOME` inherited from the terminal; unset on the tested host, so the **Codex default home, `~/.codex`** |
| Orca's built-in `--agent codex` launcher | Orca's own managed runtime home, an Orca-internal location |

**These are independent. Authenticating one does not authenticate the other**, and this was
observed directly rather than inferred: after the operator logged in, the default home ran
clean while the managed home still returned `401 unauthorized_unknown`.

The convention this contract freezes is the rule, not the paths: *the launch path uses whatever
`CODEX_HOME` its process inherits, and whoever operates that path is responsible for that
specific store being live.* Orca's internal directory layout is Orca's, may change, and is not
an interface. Do not hard-code it.

---

## 4. Model and effort translation

The catalog decision is translated to native Codex arguments. It is **always passed
explicitly** and never inherited from the operator's global Codex configuration — a launch that
relies on Codex defaults is not executing the stamped decision.

Catalog `openai/strong` → physical `openai/gpt-5.6-sol` → native Codex argument
**`gpt-5.6-sol`**.

The adapter strips the `openai/` provider qualifier because `-m/--model` names a model, not a
provider-qualified id. The provider-qualified form remains the authoritative catalog data; the
strip is a translation at the CLI boundary and nowhere else.

Effort maps one-to-one onto `model_reasoning_effort`:

| Catalog effort | Codex `model_reasoning_effort` |
|---|---|
| `low` | `low` |
| `medium` | `medium` |
| `high` | `high` — **tested** |
| `xhigh` | `xhigh` |

Only `high` was exercised live. The other three are the frozen mapping, not measured
behaviour; in particular `xhigh` acceptance by this Codex version is unverified.

`~/.codex/config.toml` is never modified. Effort is set per invocation with `-c`.

---

## 5. The launch sequence

Orca owns the worktree and the terminal; Codex runs natively inside them. Step 0 is the
setup-policy gate from below — it runs BEFORE anything is created, because a repository
wait-for-setup policy makes the whole path unsafe and stopping after worktree-create is
already too late. The terminal command records the Codex exit status in-band, because
`orca terminal wait --for exit` does not fire (the terminal outlives its command) — the
`CODEX_EXIT=` line is the launcher's only reliable completion signal.

```
# 0. setup-policy gate: stop unless the repo starts immediately
orca repo show id:<repoId> --json   # inspect hookSettings; wait-for-setup => STOP

# 1-2. lifecycle + sanitized, fail-closed launch
orca worktree create --repo id:<repoId> --name <task> --parent-worktree active --json
orca terminal create --worktree id:<repoId>::<worktreePath> --title <task> \
  --command 'env -u OPENAI_API_KEY -u CODEX_API_KEY \
    codex exec --model gpt-5.6-sol -c model_reasoning_effort="high" -s workspace-write "<task>"; \
    echo "CODEX_EXIT=$?"'
```

**Why the two-step custom-command shape rather than `--agent codex`:** Orca's own
version-matched guide states that `worktree create --agent codex` does not accept Codex-specific
`--model` or `-c model_reasoning_effort=...` arguments. Since explicit model and effort are
mandatory, the built-in launcher cannot carry the stamped decision and the custom-command path
is required. This is still Orca lifecycle ownership: Orca creates and owns both the worktree and
the terminal, and reaps the process.

**Setup ordering must be checked before using this path.** Orca cannot preserve a repository
wait-for-setup startup policy across worktree-create followed by a later custom-argv
terminal-create. Read the repo's setup policy first; if it waits for setup, this path is unsafe
and the launch must stop rather than bypass setup ordering. On the validated repo the policy was
`start-immediately` with empty setup scripts, so no conflict existed.

### Working directory

Codex must run with its working directory inside the Orca worktree. The tested path satisfies
this because an Orca terminal starts in its worktree; Codex reported
`workdir: <the Orca worktree path>` in its own session banner. A launcher that cannot show this
has not met the requirement.

---

## 6. Sandbox and approval posture

The tested and frozen posture is:

```
-s workspace-write        # sandbox: workspace-write [workdir, /tmp, $TMPDIR]
approval: never           # implied by `codex exec`; the non-interactive mode takes no -a flag
```

This is the least-privileged mode that completes a task which creates a file, and it was
established by testing the rung below it rather than by assumption. Under `-s read-only` the
same task ran, reached the model, and honestly declined:

> I couldn't create `spike-hello.md` because the workspace is read-only and write approval is
> disabled. No files were modified, and no Git commands were run.

**`--dangerously-bypass-approvals-and-sandbox` is forbidden on this path.** A git worktree is
not an external sandbox and does not justify unrestricted host execution. `danger-full-access`
is likewise out. If a task cannot complete under a native sandboxed mode, that is a blocker to
report, never a reason to weaken the posture. A future externally sandboxed VM or container
could revisit this under a different contract; it does not apply to a harness running directly
on the workstation.

---

## 7. API keys are forbidden — and the launch fails closed

The launch environment must not define `OPENAI_API_KEY` or `CODEX_API_KEY`, and no custom
`model_provider`, `model_providers`, `openai_base_url` or provider `env_key` may be configured.
The native OpenAI provider is used as shipped; Codex reported `provider: openai` on the
validated run, and neither config file declared a provider override.

Explicit `--model` and effort arguments do NOT constrain auth or provider selection: the
command inherits the terminal environment and the effective `config.toml`, and an API-key
variable or provider override there can redirect authentication away from the subscription
session. The launcher therefore fails closed rather than trusting the environment: it launches
through a sanitizer that unsets `OPENAI_API_KEY` and `CODEX_API_KEY` (the `env -u` form in
§5's canonical command), and it must refuse to launch when the effective Codex config declares
any non-default provider setting. Requirement, not tested evidence: the validated run confirmed
`OPENAI_API_KEY` unset and no config overrides; API-key-precedence behaviour itself was not
exercised and must never be.

`OPENAI_API_KEY` absence was confirmed in the launch environment. It is a necessary check, not a
sufficient one — §2's endpoint evidence is what actually distinguishes the subscription path.

Verified: with an empty credential store and no API key, Codex does not silently reach for a
billing path. It attempts `api.openai.com`, is refused
(`401 Missing bearer or basic authentication in header`), exits non-zero, and writes nothing.

---

## 8. Exit semantics

**A zero exit means the turn completed, not that the task succeeded.** The `read-only` run in §6
exited `0` while creating no file. A launcher that treats exit 0 as task success will report
false completions.

Verify the intended effect — the expected diff, the expected artifact — separately from the exit
code. The exit code is only good for distinguishing a completed turn from a failed one.

Auth failure is honest and non-zero: exit `1`, with a `401` and an explicit instruction to sign
in again. It does not fall back, does not retry into a different credential path, and does not
partially apply work.

Practical note for launcher authors: on the tested Orca version, `orca terminal wait --for exit`
did not fire when the terminal's command finished, because the terminal outlives the command.
Capture the exit status in-band (record `$?` inside the terminal command) rather than relying on
that wait.

### Auth expiry and re-login

`401`, revoked, expired, or reauthentication-required means **the attempt fails and a human is
asked to re-login.** That is the whole behaviour.

Login is never automated, never scripted, never performed with `--with-api-key`, and OAuth
material is never copied between stores or hosts. Re-login is an interactive human action taken
outside the launcher, against the specific store the launch path uses (§3).

---

## 9. Placement

`openai/strong` is vendor-hosted, so the profile carries **`placement_required: false`**. No
`/v1/place` call is made, no `x-placement` header exists, and no placement evidence is produced
or expected. Model-inference placement is about GPUs this path does not use.

---

## 10. Cleanup

A session's worktree and terminals are disposed through Orca's own lifecycle commands
(`orca worktree rm --force`, which reaps the terminals with it), leaving no worktree, branch,
directory or terminal behind. Any temporary credential home created for testing is removed, and
temporary homes are created empty — never copied from a live store.

Validation output is not committed. The scratch artifact produced during this validation existed
only inside the disposable worktree and was destroyed with it.

---

## 11. What this document does not own

Stated explicitly so it cannot be read as more than it is. This file owns *how the codex harness
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

- STORY-035-16′ — the validation this file freezes; evidence captured live on devbox.
- `ADE-BOUNDARY.md` §3 — native harnesses own their own authentication, the statement this
  validation was run against.
- Orca's version-matched CLI guide (`orca skills get orca-cli`, v1.4.187) — the documented
  custom-command shape and the `--agent` model/effort limitation.
