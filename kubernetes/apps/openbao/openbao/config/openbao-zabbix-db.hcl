# The Zabbix database login (External Secrets, Kubernetes auth role zabbix-db):
# read this static role, nothing else. Written by scripts/openbao-configure.sh.
path "database/static-creds/zabbix" {
  capabilities = ["read"]
}

# The role drops the default policy; External Secrets' Vault client checks and
# revokes its own token.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
