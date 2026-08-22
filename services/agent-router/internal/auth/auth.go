// Package auth holds the two independent bearer-credential checks the
// contract requires: a caller credential for GET /v1/status
// (callerBearer), and a per-node edge credential bound to exactly one node
// identifier for POST /v1/capacity/heartbeat (edgeNodeBearer). A client
// that can ask for status must not be able to report capacity, and a
// credential that could heartbeat as any node could withdraw a healthy
// placement or keep a dead one advertised - so the two are never the same
// set (contracts/agent-router/openapi.yaml).
package auth

import (
	"crypto/sha256"
	"crypto/subtle"
)

// CallerAuth checks the caller bearer credential.
type CallerAuth struct {
	digests map[[sha256.Size]byte]bool
}

// NewCallerAuth builds a CallerAuth from a set of valid tokens. Empty
// strings are ignored so an unset/blank env var never becomes an accepted
// credential. Each token is hashed once here, at construction, rather than
// on every request.
func NewCallerAuth(tokens []string) *CallerAuth {
	m := make(map[[sha256.Size]byte]bool, len(tokens))
	for _, t := range tokens {
		if t != "" {
			m[sha256.Sum256([]byte(t))] = true
		}
	}
	return &CallerAuth{digests: m}
}

// Valid reports whether token is an accepted caller credential.
//
// The comparison is constant-time, visits every configured credential, and
// compares fixed-width digests rather than the raw tokens.
// subtle.ConstantTimeCompare returns 0 immediately when its two inputs have
// different lengths, so comparing raw tokens of varying length would leak
// how long the presented token is relative to each configured one. Hashing
// both sides to a fixed 32-byte digest first removes that channel: every
// comparison is the same length regardless of what was presented, and the
// loop still visits every credential rather than stopping at the first
// match. Iteration cost varies with how many credentials are configured,
// which is not secret.
func (a *CallerAuth) Valid(token string) bool {
	if token == "" {
		return false
	}
	presented := sha256.Sum256([]byte(token))
	match := 0
	for d := range a.digests {
		match |= subtle.ConstantTimeCompare(presented[:], d[:])
	}
	return match == 1
}

// NodeAuth checks per-node edge bearer credentials, each bound to exactly
// one node identifier.
type NodeAuth struct {
	nodeForDigest map[[sha256.Size]byte]string
}

// NewNodeAuth builds a NodeAuth from a token -> node id map. Credentials
// come from the environment or a mounted secret (cmd/agent-router); nothing
// credential-shaped is ever constructed from a literal in source. Each
// token is hashed once here, at construction.
func NewNodeAuth(credentials map[string]string) *NodeAuth {
	m := make(map[[sha256.Size]byte]string, len(credentials))
	for token, node := range credentials {
		if token != "" && node != "" {
			m[sha256.Sum256([]byte(token))] = node
		}
	}
	return &NodeAuth{nodeForDigest: m}
}

// NodeFor returns the node identifier token is bound to, and whether token
// is a recognized credential at all.
// The comparison is constant-time over fixed-width digests and visits every
// credential, for the same reason as CallerAuth.Valid. This one guards a
// write path bound to a node identity, so a leak here would help forge a
// heartbeat for a node the caller does not hold a credential for.
func (a *NodeAuth) NodeFor(token string) (string, bool) {
	if token == "" {
		return "", false
	}
	presented := sha256.Sum256([]byte(token))
	var node string
	found := 0
	for d, n := range a.nodeForDigest {
		eq := subtle.ConstantTimeCompare(presented[:], d[:])
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
