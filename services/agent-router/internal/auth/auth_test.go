package auth

import "testing"

func TestCallerAuth(t *testing.T) {
	a := NewCallerAuth([]string{"good-token", "", "second-token"})

	for _, tc := range []struct {
		name  string
		token string
		want  bool
	}{
		{"accepted", "good-token", true},
		{"second accepted", "second-token", true},
		{"wrong", "bad-token", false},
		{"empty rejected", "", false},
		// A blank entry in the configured set must never become an
		// accepted credential - an unset env var would otherwise
		// authenticate everyone presenting no token.
		{"prefix of a valid token", "good", false},
		{"valid token plus suffix", "good-token-extra", false},
	} {
		if got := a.Valid(tc.token); got != tc.want {
			t.Errorf("%s: Valid(%q) = %v, want %v", tc.name, tc.token, got, tc.want)
		}
	}
}

func TestCallerAuth_NoTokensConfigured(t *testing.T) {
	// With nothing configured, nothing authenticates - including "".
	a := NewCallerAuth(nil)
	if a.Valid("") || a.Valid("anything") {
		t.Error("a CallerAuth with no configured tokens accepted a credential")
	}
}

func TestNodeAuth_BindsTokenToOneNode(t *testing.T) {
	a := NewNodeAuth(map[string]string{
		"token-a": "node-a",
		"token-b": "node-b",
		"":        "ignored-blank-token",
		"token-c": "",
	})

	node, ok := a.NodeFor("token-a")
	if !ok || node != "node-a" {
		t.Errorf("NodeFor(token-a) = %q,%v; want node-a,true", node, ok)
	}
	// The binding is what stops one node's credential from reporting as
	// another: token-b resolves to node-b and nothing else, so the handler
	// can reject a payload naming any other node.
	if node, _ := a.NodeFor("token-b"); node != "node-b" {
		t.Errorf("NodeFor(token-b) = %q, want node-b", node)
	}
	for _, bad := range []string{"", "token-c", "unknown", "token-", "token-aa"} {
		if node, ok := a.NodeFor(bad); ok {
			t.Errorf("NodeFor(%q) = %q,true; want not recognized", bad, node)
		}
	}
}

func TestNodeAuth_NoCredentialsConfigured(t *testing.T) {
	a := NewNodeAuth(nil)
	if _, ok := a.NodeFor("anything"); ok {
		t.Error("a NodeAuth with no configured credentials recognized a token")
	}
}
