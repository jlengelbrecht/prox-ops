# The merge gate's evidence worker (Kubernetes auth role merge-evidence):
# request a short token for the gate-reader account in one namespace. Nothing
# else. Only kubernetes_namespace may be set, so a caller can't choose the
# token's audience or lifetime. Written by scripts/openbao-configure.sh.
path "kubernetes/creds/gate-reader" {
  capabilities       = ["update"]
  allowed_parameters = {
    "kubernetes_namespace" = []
  }
}

# The role drops the default policy, so revoking its own token needs this.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
