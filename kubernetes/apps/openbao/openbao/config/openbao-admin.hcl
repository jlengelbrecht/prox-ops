# Full administration: members of the authentik group openbao-admins (OIDC
# login) get this policy through the external identity group of the same
# name. Written by scripts/openbao-configure.sh.
path "*" {
  capabilities = ["create", "read", "update", "patch", "delete", "list", "sudo"]
}
