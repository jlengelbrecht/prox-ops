# The hourly job that rotates Warpgate's Kubernetes target tokens (Kubernetes
# auth role warpgate-tokens): get a token for each account, store it. Only
# kubernetes_namespace may be set, so it can't choose the lifetime or audience.
# Written by scripts/openbao-configure.sh.
path "kubernetes/creds/warpgate-k8s-read" {
  capabilities       = ["update"]
  allowed_parameters = {
    "kubernetes_namespace" = []
  }
}

path "kv/data/warpgate/k8s-read" {
  capabilities = ["create", "update"]
}

path "kubernetes/creds/warpgate-k8s-admin" {
  capabilities       = ["update"]
  allowed_parameters = {
    "kubernetes_namespace" = []
  }
}

path "kv/data/warpgate/k8s-admin" {
  capabilities = ["create", "update"]
}

# The role drops the default policy, so revoking its own token needs this.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
