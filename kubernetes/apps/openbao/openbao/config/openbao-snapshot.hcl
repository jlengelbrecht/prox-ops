# The daily snapshot job (openbao/openbao-snapshots, Kubernetes auth role
# openbao-snapshot): take a Raft snapshot, nothing else. Written by
# scripts/openbao-configure.sh.
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}

# The role drops the default policy, so revoking its own token at the end of
# the job needs this.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
