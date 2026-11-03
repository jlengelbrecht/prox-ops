# Look, don't touch: members of the authentik group openbao-readers can list
# every path and read what the web UI needs to show OpenBao's mounts and
# policies, but can't read secret values or use keys. The built-in default
# policy (also attached) covers the token's own lookup and capabilities.
# Written by scripts/openbao-configure.sh.
path "*" {
  capabilities = ["list"]
}

# Allowlist, not sys/*: host info, profiling and the like stay closed.
# The UI's mount and permission discovery.
path "sys/internal/ui/*" {
  capabilities = ["read", "list"]
}

# Secrets engines.
path "sys/mounts" {
  capabilities = ["read"]
}

path "sys/mounts/*" {
  capabilities = ["read"]
}

# Policies.
path "sys/policies/acl" {
  capabilities = ["list"]
}

path "sys/policies/acl/*" {
  capabilities = ["read"]
}

# Defence in depth against the list grant above: raw storage, Raft
# administration (snapshots are the whole encrypted database), live server
# logs, internal state, and the root-token and rekey workflows.
path "sys/raw/*" {
  capabilities = ["deny"]
}

path "sys/storage/raft/*" {
  capabilities = ["deny"]
}

path "sys/monitor" {
  capabilities = ["deny"]
}

path "sys/internal/inspect/*" {
  capabilities = ["deny"]
}

path "sys/generate-root*" {
  capabilities = ["deny"]
}

path "sys/rekey*" {
  capabilities = ["deny"]
}

path "sys/rekey-recovery-key*" {
  capabilities = ["deny"]
}
