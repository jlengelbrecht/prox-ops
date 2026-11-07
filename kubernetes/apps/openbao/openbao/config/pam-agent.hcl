# Agent PAM: what a harness login (auth/jwt, entities agent-<harness>) may do.
# Reading anything under kv/data/pam waits for an approval from pam-approvers.
# Writing a new value to the agent's own inbox doesn't, because the agent can't
# read it back without approval or overwrite it. Agents can't change a live
# secret or read another agent's inbox: one approval is a replayable window,
# and the approver can't see a write's body.
#
# Every control_group needs its ttl (a read vanishes without one), and no other
# policy on an agent identity may touch these paths: a plain grant on the same
# path silently removes the gate. scripts/pam/policy_guard.py checks both.
path "kv/data/pam/live/*" {
  capabilities = ["read"]
  control_group = {
    ttl = "15m"
    factor "owner" {
      controlled_capabilities = ["read"]
      identity {
        group_names = ["pam-approvers"]
        approvals   = 1
      }
    }
  }
}

# Own inbox: create without approval, no overwrite, read with approval.
path "kv/data/pam/inbox/{{identity.entity.name}}/*" {
  capabilities = ["create", "read"]
  control_group = {
    ttl = "15m"
    factor "owner" {
      controlled_capabilities = ["read"]
      identity {
        group_names = ["pam-approvers"]
        approvals   = 1
      }
    }
  }
}

path "kv/metadata/pam/live/*" {
  capabilities = ["list"]
}

# Unwrap gives the same error for a pending request and a denied one. Asking
# whether the wrapping token still exists is how the client tells them apart.
# This works on any accessor, not only the agent's own: it shows that token's
# policies, entity, display name, login path and expiry, never the token.
path "auth/token/lookup-accessor" {
  capabilities = ["update"]
}

# The role drops the default policy, so revoking its own token needs this.
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
