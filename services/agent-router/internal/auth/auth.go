// Package auth holds the two independent bearer-credential checks the
// contract requires: a caller credential for GET /v1/status
// (callerBearer), and a per-node edge credential bound to exactly one node
// identifier for POST /v1/capacity/heartbeat (edgeNodeBearer). A client
// that can ask for status must not be able to report capacity, and a
// credential that could heartbeat as any node could withdraw a healthy
// placement or keep a dead one advertised - so the two are never the same
// set (contracts/agent-router/openapi.yaml).
package auth

import "crypto/subtle"

// CallerAuth checks the caller bearer credential.
type CallerAuth struct {
	tokens map[string]bool
}

// NewCallerAuth builds a CallerAuth from a set of valid tokens. Empty
// strings are ignored so an unset/blank env var never becomes an accepted
// credential.
func NewCallerAuth(tokens []string) *CallerAuth {
	m := make(map[string]bool, len(tokens))
	for _, t := range tokens {
		if t != "" {
			m[t] = true
		}
	}
	return &CallerAuth{tokens: m}
}

// Valid reports whether token is an accepted caller credential.
//
// The comparison is constant-time and does not stop at the first match.
// A plain map lookup compares bytes and short-circuits on the first
// mismatch, which leaks how much of a guessed credential was correct; the
// loop below always visits every configured token so the time taken does
// not depend on the value supplied. Iteration cost varies with how many
// credentials are configured, which is not secret.
func (a *CallerAuth) Valid(token string) bool {
	if token == "" {
		return false
	}
	match := 0
	for t := range a.tokens {
		match |= subtle.ConstantTimeCompare([]byte(token), []byte(t))
	}
	return match == 1
}

// NodeAuth checks per-node edge bearer credentials, each bound to exactly
// one node identifier.
type NodeAuth struct {
	nodeForToken map[string]string
}

// NewNodeAuth builds a NodeAuth from a token -> node id map. Credentials
// come from the environment or a mounted secret (cmd/agent-router); nothing
// credential-shaped is ever constructed from a literal in source.
func NewNodeAuth(credentials map[string]string) *NodeAuth {
	m := make(map[string]string, len(credentials))
	for token, node := range credentials {
		if token != "" && node != "" {
			m[token] = node
		}
	}
	return &NodeAuth{nodeForToken: m}
}

// NodeFor returns the node identifier token is bound to, and whether token
// is a recognized credential at all.
// The comparison is constant-time and visits every credential, for the
// same reason as CallerAuth.Valid. This one guards a write path bound to a
// node identity, so a leak here would help forge a heartbeat for a node the
// caller does not hold a credential for.
func (a *NodeAuth) NodeFor(token string) (string, bool) {
	if token == "" {
		return "", false
	}
	var node string
	found := 0
	for t, n := range a.nodeForToken {
		eq := subtle.ConstantTimeCompare([]byte(token), []byte(t))
		if eq == 1 {
			node = n
		}
		found |= eq
	}
	if found != 1 {
		return "", false
	}
	return node, true
}
