# The job that copies kv/pam one way into the 1Password vault OpenBao-Records.
# It records what it copied in KV custom metadata and never touches kv/warpgate.
path "kv/data/pam/*" {
  capabilities = ["read"]
}

# Only custom_metadata can change, so the job can't alter version retention.
path "kv/metadata/pam/*" {
  capabilities       = ["read", "list", "patch"]
  allowed_parameters = {
    "custom_metadata" = []
  }
}

# The role drops the default policy, so revoking its own token needs this.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
