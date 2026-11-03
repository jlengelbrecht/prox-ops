# Look, don't touch: members of the authentik group openbao-readers can list
# every path and read OpenBao's own configuration (mounts, auth methods,
# policies, health), but can't read secret values or use keys. Written by
# scripts/openbao-configure.sh.
path "*" {
  capabilities = ["list"]
}

path "sys/*" {
  capabilities = ["read", "list"]
}

path "sys/raw/*" {
  capabilities = ["deny"]
}

# Raft administration, including snapshots (the whole encrypted database).
path "sys/storage/raft/*" {
  capabilities = ["deny"]
}

# Not needed to look around: live server logs, internal state, and the
# root-token and rekey workflows.
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
