# Nyctr self-service setup

This configuration prepares alpha.7, already pinned in production. It permits
NextDNS changes and the existing HTTPS model gateway, and adds registration
lookup egress. The owner decides when to merge and which console rules to enable.
It does not save a model, enable recognition, or change stored access rules.

## Credential prerequisite: keep the PR in draft

Before merge, an operator must provision a dedicated Nyctr LiteLLM virtual key
and store it in the existing Connect store's **Automation** vault, item
**nyctr-model**, field **api-key** (`op://Automation/nyctr-model/api-key`).
Restrict that key to the administrator's chosen, existing chat-completions
subscription alias, with no metered fallback. Use existing subscription capacity;
do not grant new paid inference, quota, funds, or wildcard model access. Never
reuse the gateway master key, a source credential, or another application's key.
This change creates neither the external key nor the 1Password item.

The reference is ExternalSecret `security/nyctr-model`, target Secret
`security/nyctr-model`, key `api-key`, exposed only as
`NYCTR_MODEL_SECRET_LITELLM`. It uses `onepassword-connect` with hourly refresh
and the existing reloader annotation. The service account used for the metadata
audit could read only AI-Access: no Nyctr item appeared among relevant titles,
and the existing `nyctr` item could not be read. That does not prove absence
in Automation. Provisioning there and key scope remain unverified.
The nonoptional Secret reference prevents a new pod from starting without it.
Before merge, review existing stored rules and tier states: the new provider
permission can let an already-enabled rule act on the next request or collection.
The unchanged collection interval of zero leaves alpha.7's 15-minute default in
effect until an administrator saves a schedule; zero does not disable collection.

## Administrator handoff

After the prerequisite is confirmed and the owner merges through normal GitOps,
check that the model ExternalSecret is Ready and the workload is healthy using
normal operator procedures. Do not expose secret bytes to verify delivery.
In **Setup, This installation, Model**, enter:

| Field | Value |
| --- | --- |
| Endpoint | `https://ai-api.homelab0.org/v1` |
| Model | An operator-approved existing subscription alias supporting `POST /v1/chat/completions` |
| Secret | `NYCTR_MODEL_SECRET_LITELLM` |
| Tracing | For a tracing proxy, if desired; the default server trace remains off |
| Time limit | 5 to 120 seconds; analysis itself caps the model call at 20 seconds |

The server allowlist contains only the HTTPS origin, without `/v1`. No default
model or alias is saved by these manifests. Do not choose an unqualified model
name or a Responses-only route such as `project-chatgpt-responses` or
`automation-chatgpt-*-responses`. Confirm the chosen alias and dedicated key
scope with the gateway operator. The console's Test sends inference and should
be used only when separately authorized; no live inference was run here.

Review trusted proxy hop count and current-device recognition before enabling
recognition. Review the access rule, intended advertising/tracker or threat-feed
tier, expiry, and daily limits; begin with record-only if desired. Threat feeds
require known domain age and the other required evidence. Missing registration,
missing or invalid model output, and timeouts remain unknown and require a person
when the rule needs them. **Open a site a member named** is a separate switch
that starts off. These administrator choices are not activated by this PR.

`providerWrites: [nextdns]` authorizes only that integration, independently of
stored rules. A NextDNS allowlist entry affects the **whole profile**, including
other devices sharing it. Categories, deliberate deny rules, and gateway/firewall
blocks do not open automatically. Observe the first separately authorized real
opening and its expiry removal before relying on it. This preparation performs
no provider write and does not establish backup/restore readiness.

## Registration egress and refresh

The exact registry list in `networkpolicy.yaml` comes from
<https://data.iana.org/rdap/dns.json>, publication `2026-11-19T00:00:03Z`, raw
SHA-256 `c226509a9b65202967158d2878fb28f008587a8dd0347bdc3f992b673debe1b4`.
It permits `data.iana.org` and 377 unique registry names on TCP 443, matching
alpha.7's first HTTPS URL on port 443 per service, covering 1,202 suffixes in
that snapshot. HTTP-only `kg` and `mg` are excluded; unlisted suffixes (including
`io` in this snapshot), redirects, changed registry names, or registries that
omit registration dates do not yield known age. Parent-name lookup is limited
by alpha.7's last-two/last-three-label algorithm, not a public-suffix resolver.

Review IANA changes monthly and before relying on a newly needed suffix. Fetch
the bootstrap over HTTPS, record its publication and raw SHA-256, and compare
the sorted unique hostnames selected by the algorithm above against the policy.
Review additions/removals in a PR; never follow registry redirects or replace
the list with a wildcard/world rule. Alpha.7 caches successful registration and
bootstrap metadata for 24 hours, failures for ten minutes; timeouts are uncached.
Registry calls cap at five seconds/256 KiB inside an eight-second gathering.

Cilium learns addresses through the existing cluster DNS proxy; alpha.7 resolves
registry names with a trailing dot and pins a checked public address for HTTPS.
The FQDN rules restrict destination IP and port, not TLS names or URL paths;
other names sharing an allowed IP may be reachable. Nyctr verifies public TLS,
refuses private addresses, and follows no redirects. Arbitrary internet HTTPS,
non-443 registry ports, direct LiteLLM port 4000, and the external gateway stay
closed. The existing internal gateway permits its routes at L3/L4; application
origin validation and gateway authentication constrain model credential use.

## Local verification

Run `python3 -m unittest discover -s tests/nyctr -p 'test*.py'` and
`python3 scripts/check_nyctr_release.py --verify-digest`. To also check the
actual published chart, set `NYCTR_CHART_ARCHIVE` to its pulled alpha.7 archive
and `HELM` to a Helm 3 executable for the same unittest command. The chart tests
run schema validation, lint, template, and the actual Flux kustomize patches;
they check the rendered model environment uses only Secret references.
Run `kubectl kustomize kubernetes/apps/security/nyctr/app` and the repository's
available configuration checks. CI renders the full cluster with flate and runs
the existing Nyctr release guard; neither requires a production reconciliation.
