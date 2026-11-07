# The approval page: see held requests, approve them, deny them. Nothing else.
# This reaches the page through the external group pam-approvers, on the pam-oidc
# mount. That has to be its own auth mount: identity policies follow the entity,
# so any login on the oidc mount also carries openbao-admin.
path "sys/control-group/authorize" {
  capabilities = ["update"]
}

path "sys/control-group/request" {
  capabilities = ["update"]
}

# OpenBao refuses this listing without sudo.
path "auth/token/accessors/" {
  capabilities = ["list", "sudo"]
}

path "auth/token/revoke-accessor" {
  capabilities = ["update"]
}
