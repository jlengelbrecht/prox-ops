// Package stampvalidate implements STORY-035-12's final pre-execution policy
// validator: ValidateFinal(cat, stamp, vctx, now) Verdict. It is the last
// check before an accepted or overridden route stamp is allowed to launch a
// harness session.
//
// OWNERSHIP (STORY-035-12 "The named owner of final stamp validation"). This
// package is a SEPARATE pre-execution policy-validation component. It SHARES
// policy/catalog code with agent-router by living in the same
// services/agent-router Go module - source-code colocation is an
// implementation convenience, NOT runtime ownership. No request to the
// router happens at validation time, and the running agent-router process is
// never involved: cmd/agent-router does not import this package, and this
// package never dials a network socket (see the boundary greps this story's
// gate 5 runs). For non-Go callers it ships as the agent-stamp-validate CLI
// (cmd/agent-stamp-validate), built from this same source commit.
//
// PURITY (frozen). ValidateFinal is a pure function of (catalog, stamp,
// validationContext, now): no I/O, no wall-clock, no randomness, no
// environment reads. The same inputs always yield a byte-identical Verdict.
// Every wall-clock read and every file read happens in the CLI
// (cmd/agent-stamp-validate) or in a caller's own code, never in here.
//
// NO DUPLICATED AUTHORITY (amendment 5). Harness/model-profile approval
// reuses routing.ApprovedPairs directly (see approvedPair in validate.go);
// forbidden_for reuses routing.TagsForbidden; placement authorization reuses
// catalog.Catalog.ModelAuthorizedOnPlacement plus the same profile/policy
// tables routing and placement already read. There is no second pair matrix
// or authority table anywhere in this package.
package stampvalidate

import "time"

// TaskIdentity is the task/story-identity subset of the stamp (amendment 4):
// enough to say WHICH attempt this is and to carry the policy-bearing tags a
// profile's catalog forbidden_for list is checked against. It deliberately
// carries nothing else - no touched_paths, no requester, no repo - because
// none of that is read by any frozen validation rule.
type TaskIdentity struct {
	StoryID string
	Title   string
	Tags    []string
}

// OverrideOriginal is the pre-override values an audit record remembers -
// what the router itself would have recommended, before a human overrode it.
type OverrideOriginal struct {
	Harness      string
	ModelProfile string
}

// Override is the nullable pre-stamp override audit record amendment 4
// freezes: {actor, at, original: {harness, model_profile}, reason}. Its
// presence is provenance only - amendment 4 is explicit that the override
// record grants nothing and resets nothing; the FINAL stamped fields are
// validated on their own merits by every other rule, unconditionally.
type Override struct {
	Actor    string
	At       time.Time
	Original OverrideOriginal
	Reason   string
}

// Stamp mirrors contracts/agent-router/schemas/execution-stamp.schema.json
// field-for-field (amendment 4). It carries the final accepted ROUTE
// decision and task-policy inputs ONLY - never a resolved physical placement
// (amendment 2: that lives in the separate, ephemeral PlacementEvidence).
type Stamp struct {
	Harness      string
	ModelProfile string
	Effort       string
	CostClass    string
	// EntitlementPool is nil for local execution, exactly as on
	// ExecutionProfile - local execution draws on no entitlement pool.
	EntitlementPool   *string
	Metered           bool
	PlacementPolicy   string
	PlacementRequired bool
	CatalogVersion    string
	// CatalogDocumentVersion is additive/optional (amendment 4: "when
	// present"). Empty string means absent; it is audit provenance only and
	// no frozen rule reads it.
	CatalogDocumentVersion string
	// ExpiresAt MUST be copied unchanged from the accepted /v1/route
	// decision (amendment 4). Stamping or overriding never grants a fresh
	// window; ValidateFinal only ever compares it against now (check 7), it
	// never recomputes or extends it.
	ExpiresAt time.Time
	Task      TaskIdentity
	// Override is nil when the stamp is the router's unmodified
	// recommendation, non-nil when a human deliberately overrode it before
	// stamping (amendment 4, ADE-BOUNDARY.md §7).
	Override *Override
	// CreatedAt is optional audit-only provenance (amendment 4): it does NOT
	// reset route validity and no frozen rule reads it.
	CreatedAt *time.Time
}

// PlaceReason mirrors place-result.schema.json #/$defs/reason.
type PlaceReason struct {
	Code    string
	Message string
}

// PlaceAlternative mirrors place-result.schema.json #/$defs/alternative.
// ValidateFinal never reads this - it is decoded only because the frozen
// PlaceResult schema requires it (owner clarification 1: "the frozen
// PlaceResult schema itself untouched").
type PlaceAlternative struct {
	Placement           string
	Model               string
	Readiness           string
	EstimatedColdStartS *float64
	Eligible            bool
	Reason              PlaceReason
}

// PlaceResult mirrors contracts/agent-router/schemas/place-result.schema.json
// field-for-field, UNCHANGED (owner clarification 1 and STORY-035-12
// "Implementation scope": "no PlaceResult schema change"). This is the exact
// same wire shape POST /v1/place already returns; stampvalidate does not
// invent a second one.
type PlaceResult struct {
	Status              string
	Model               *string
	Placement           *string
	Readiness           *string
	EstimatedColdStartS *float64
	Headers             map[string]string
	TTLSeconds          int
	Reason              PlaceReason
	Alternatives        []PlaceAlternative
	CatalogVersion      string
}

// PlacementEvidence is the caller-owned wrapper amendment 2 and the owner's
// clarification 1 freeze: {resolved_at, result: PlaceResult}. There is ONE
// authoritative copy of placement data - status, placement, headers,
// ttl_seconds, catalog_version, readiness all live in the nested Result, and
// this wrapper adds nothing but the timestamp that PlaceResult itself
// deliberately does not carry (owner clarification 1: "do not duplicate
// those fields in the wrapper; two values that can disagree is a failing
// design").
type PlacementEvidence struct {
	ResolvedAt time.Time
	Result     PlaceResult
}

// ValidationContext carries independently-established execution-time facts
// supplied by the invoking environment - never derivable from the stamp
// itself (amendment 3). It is the only source of placement evidence and of
// metered-spend authority; a stamp cannot mint either for itself.
type ValidationContext struct {
	// PlacementEvidence is nil when no /v1/place call has happened for this
	// attempt (or none was required). It is never read from or inferred by
	// the stamp.
	PlacementEvidence *PlacementEvidence
	// MeteredSpendAuthorized is whether the invoking environment
	// independently holds authority to approve billable spend for this
	// attempt - a fact about the CALLER, never about the stamp. The MVP
	// agent-stamp-validate CLI has no flag that can set this true (amendment
	// 3): only a Go caller constructing ValidationContext directly can, and
	// STORY-035-12 tests do so purely to prove the dual-key semantics.
	MeteredSpendAuthorized bool
}

// CheckResult is one named rule's verdict - every one of the eight frozen
// rules is always present, in a fixed order, whether it passed or failed
// (the story's "every check evaluated; Verdict lists all").
type CheckResult struct {
	Name       string
	Passed     bool
	ReasonCode string
	Message    string
}

// Verdict is ValidateFinal's return value, mirroring
// contracts/agent-router/schemas/validation-verdict.schema.json
// field-for-field. Valid is the AND of every CheckResult.Passed - there is no
// separate override or bypass path (amendment 6: "no bypass surface").
type Verdict struct {
	Valid          bool
	Checks         []CheckResult
	CatalogVersion string
}
