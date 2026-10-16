# homelab-identity stubs for flux-local

`identity-system/homelab-identity` is a private repository, so CI can't read it. The
flux-local workflow maps that source to this directory, so the `identity-people`,
`identity-apps` and `identity-ca` Kustomizations still build in CI. Each directory holds
an empty `kustomization.yaml`; the real content lives in the private repository.
