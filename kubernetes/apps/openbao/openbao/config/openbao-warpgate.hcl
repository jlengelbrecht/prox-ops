# Warpgate's secret backend (Kubernetes auth role warpgate): read target
# credentials under kv/warpgate, and keep its own login alive. Warpgate's own
# secrets are not here: they stay in 1Password. Written by
# scripts/openbao-configure.sh.
path "kv/data/warpgate/*" {
  capabilities = ["read"]
}

# The role drops the default policy. The backend's health check looks up the
# token, and its renewal task renews it.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
