# Crossplane admission guard fixtures

Server-side dry-run tests for the ValidatingAdmissionPolicies in
`kubernetes/apps/crossplane-system/guards/app/`. This directory is outside the
Flux path, so nothing here is ever applied to the cluster by Flux. Every value
is a placeholder.

| File | Expected result |
|---|---|
| `admitted.yaml` | Every object admitted (positive controls) |
| `denied.yaml` | Every object denied, by the policy in its `Expect:` comment |
| `after-setup.yaml` | Every object denied, after step 4 below |

## Policies

| ID | Policy | Rule |
|---|---|---|
| V1 | `crossplane-azuread-v1-no-cluster-providerconfig` | No `ClusterProviderConfig`, no legacy `azuread.upbound.io` `ProviderConfig` |
| V2 | `crossplane-azuread-v2-managed-resources` | Namespaced `ProviderConfig` ref; no `*`, `Delete` or `LateInitialize` (absent field counts as `*`); no connection secret; Observe-only needs an external name |
| V3 | `crossplane-azuread-v3-namespaces` | Invitations and groups in `identity-people`, applications and principals in `identity-apps`, access policies in `identity-ca` |
| V4 | `crossplane-azuread-v4-invitations` | Guest only; no `message.additionalRecipients`; `forProvider`/`initProvider` immutable |
| V5 | `crossplane-azuread-v5-providerconfig` | Identity namespaces only; `source: Secret`; no `tenantID`, `clientID`, `msiEndpoint`, `oidcTokenFilePath` |
| V6 | `crossplane-azuread-v6-applications` | No application `password` |
| V7 | `crossplane-azuread-v7-conditional-access` | Observe-only unless `identity-ca` is labelled `identity.homelab0.org/ca-write=enabled`; then break-glass exclusion, report-only on create, review annotation to enforce |
| V8 | `crossplane-azuread-v8-external-names` | External-name allowlist per kind; new objects only under allowlisted names; Observe-only list |

V7 and V8 read `identity-system/identity-guard-params` and deny everything while
it is missing.

## Fixture to policy map

| Fixture (`metadata.name`) | File | Must be denied by |
|---|---|---|
| `v1-cluster-providerconfig` | denied | V1 |
| `v1-legacy-providerconfig` | denied | V1 |
| `v5-wrong-namespace` | denied | V5 |
| `v5-source-environment` | denied | V5 |
| `v5-tenant-id` | denied | V5 |
| `v5-oidc-token-file` | denied | V5 |
| `v5-cross-namespace-secret` | denied | V5 |
| `v2-delete-policy` | denied | V2 |
| `v2-late-initialize` | denied | V2 |
| `v2-defaulted-policies` | denied | V2 |
| `v2-cluster-providerconfig-ref` | denied | V2 |
| `v2-defaulted-providerconfig-ref` | denied | V2 |
| `v2-connection-secret` | denied | V2 |
| `grp-access-02` | denied | V2 |
| `v3-wrong-namespace` | denied | V3 |
| `v8-unlisted-external-name` | denied | V8 |
| `v8-unlisted-create-name` | denied | V8 |
| `grp-access-03` | denied | V8 |
| `v8-admin-group-writable` | denied | V8 |
| `guest-02` | denied | V4 |
| `guest-03` | denied | V4 |
| `v6-password` | denied | V6 |
| `v7-write-without-label` | denied | V7 |
| `guest-01` (update) | after-setup | V4 |
| `v7-missing-breakglass` | after-setup | V7 |
| `v7-created-enforced` | after-setup | V7 |
| `policy-01` (update) | after-setup | V7 |

Each fixture breaks one rule. If a denial message names a different policy too,
the fixture is wrong, not the policy.

## Procedure (throwaway kind cluster)

Run as cluster-admin, never as the provider ServiceAccount: the provider's own
requests are skipped by design.

1. Install Crossplane and the provider from `kubernetes/apps/crossplane-system`
   (rendered with `kustomize build` and the HelmRelease values), and wait until
   the five managed resource CRDs are served. Then create the namespaces and the
   params ConfigMap. The namespaces matter: without them the API server rejects
   a fixture before admission policy runs, which would look like a pass.

   ```bash
   for ns in identity-system identity-people identity-apps identity-ca; do
     kubectl create namespace "$ns"
   done
   kubectl apply -f - <<'EOF'
   apiVersion: v1
   kind: ConfigMap
   metadata:
     name: identity-guard-params
     namespace: identity-system
   data:
     external-names.groups: placeholder-group-01,placeholder-admin-group-01
     external-names.applications: placeholder-app-01
     external-names.principals: placeholder-principal-01
     external-names.accesspolicies: placeholder-policy-01,placeholder-policy-02,placeholder-policy-03
     create-names.groups: grp-access-01,grp-access-02,grp-access-03
     create-names.invitations: guest-01,guest-02,guest-03
     observe-only-external-names: placeholder-admin-group-01
     breakglass-group-id: placeholder-breakglass-group
   EOF
   ```

2. Positive controls: `kubectl apply --dry-run=server -f admitted.yaml`.
   Every object must be accepted.

3. Negatives: `kubectl apply --dry-run=server -f denied.yaml`. Every object
   must fail with `ValidatingAdmissionPolicy '<expected policy>' ... denied
   request`. Repeat with `kubectl apply --server-side --dry-run=server -f
   denied.yaml` to show the denials hold under server-side apply, which is how
   Flux applies.

4. Stateful negatives:

   ```bash
   kubectl apply -f admitted.yaml
   kubectl label namespace identity-ca identity.homelab0.org/ca-write=enabled
   kubectl apply --dry-run=server -f after-setup.yaml   # every object denied
   kubectl label namespace identity-ca identity.homelab0.org/ca-write-
   ```

5. Finalizers are never stranded: `kubectl delete -f admitted.yaml` must
   complete. The provider removes its own finalizers, and its requests are not
   matched.

6. Fail-closed: `kubectl -n identity-system delete configmap
   identity-guard-params`, then `kubectl apply --dry-run=server -f
   admitted.yaml`. Every managed resource must now be denied by V8, and the
   AccessPolicy by V7 as well. The ProviderConfig is still admitted.
