package httpapi

// MeteredAuthority answers whether the authenticated caller independently
// holds authority to approve billable spend - a question separate from
// whether the bearer credential is valid at all. This is the injectable
// authorization seam amendment 1 requires: CallerAuth answers only "is this
// bearer valid," and the frozen contract deliberately does not pick the
// eventual identity mechanism, so this interface lets the handler consult
// an answer without route policy depending on how that answer is produced.
//
// The token is the ONLY input: amendment 1 requires that spend authority
// never be derived from any request-controlled value (requester, tags,
// repo, allow_metered itself, credential shape, harness, provider). A
// MeteredAuthority implementation must not be handed the request body.
type MeteredAuthority interface {
	Authorized(token string) bool
}

// DenyAllMeteredAuthority is the production MeteredAuthority. Catalog 1.3.0
// declares no metered funding source and the deployed router's CallerAuth
// cannot distinguish ordinary planning automation from a principal
// authorized to approve metered spend, so every current caller carries
// metered_spend_authorized=false. Later identity work supplies a real
// implementation without rewriting route policy (amendment 1).
type DenyAllMeteredAuthority struct{}

// Authorized always returns false.
func (DenyAllMeteredAuthority) Authorized(string) bool { return false }
