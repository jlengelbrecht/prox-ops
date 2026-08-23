package routing

// EntitlementAvailability answers whether a named entitlement pool can fund
// an attempt right now. Production has no live signal for this - no
// subscription-exhaustion producer exists anywhere in this estate - so the
// production implementation (AlwaysAvailable) always answers true; only a
// test seam reports false, to exercise the substitution/409 branches
// amendment 1 requires without inventing a live quota source.
type EntitlementAvailability interface {
	Available(pool string) bool
}

// AlwaysAvailable is the production EntitlementAvailability: it never
// claims to observe exhaustion, because nothing in this router can.
type AlwaysAvailable struct{}

// Available always returns true.
func (AlwaysAvailable) Available(string) bool { return true }
