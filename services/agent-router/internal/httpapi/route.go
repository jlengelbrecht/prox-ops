package httpapi

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
)

// maxRouteBodyBytes bounds how much of a /v1/route request body this
// service will read. Task metadata (a title, a summary, a handful of tags
// and paths) is a few KB in practice; this is generous headroom against a
// misbehaving or hostile caller, not a working limit.
const maxRouteBodyBytes = 64 * 1024

var catalogVersionPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

var (
	validAmbiguity       = map[string]bool{"low": true, "medium": true, "high": true}
	validBlastRadius     = map[string]bool{"low": true, "medium": true, "high": true, "critical": true}
	validVolumeHint      = map[string]bool{"low": true, "medium": true, "high": true}
	validPlacementPolicy = map[string]bool{"prefer-warm-local": true, "cluster-only": true, "edge-only": true, "any-24gb": true}
)

// invalidRequestError carries the field-scoped detail invalid_request
// responses require (error.schema.json details.field).
type invalidRequestError struct {
	Field   string
	Message string
}

func (e *invalidRequestError) Error() string { return e.Message }

// handleRoute implements POST /v1/route (contracts/agent-router/openapi.yaml).
// Side-effect free and idempotent: it writes nothing, and identical input
// against an identical catalog and authorization state yields an identical
// decision (amendment 3).
func (s *Server) handleRoute(w http.ResponseWriter, r *http.Request) {
	token := bearerToken(r)
	if !s.callerAuth.Valid(token) {
		writeError(w, http.StatusUnauthorized, "unauthenticated",
			"Missing or invalid bearer credential.", "abort", "/v1/route", nil, nil, nil)
		return
	}

	if s.catalog.Err != nil {
		s.writeCatalogError(w, "/v1/route", true)
		return
	}
	cat := s.catalog.Catalog
	digest := s.catalog.Digest

	body, err := io.ReadAll(io.LimitReader(r.Body, maxRouteBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "could not read request body", "abort", "/v1/route", nil, nil, nil)
		return
	}
	if len(body) > maxRouteBodyBytes {
		writeError(w, http.StatusBadRequest, "invalid_request", "request body too large", "abort", "/v1/route", nil, nil, nil)
		return
	}

	req, verr := parseRouteRequest(body)
	if verr != nil {
		var details map[string]any
		if verr.Field != "" {
			details = map[string]any{"field": verr.Field}
		}
		writeError(w, http.StatusBadRequest, "invalid_request", verr.Message, "abort", "/v1/route", details, nil, nil)
		return
	}

	if req.CatalogVersion != nil && *req.CatalogVersion != digest {
		writeError(w, http.StatusConflict, "catalog_version_stale",
			"The catalog moved between planning and this call. The stamped decision would be computed against a catalog the router is no longer serving. Call /v1/route again: re-planning is a new attempt.",
			"re-plan", "/v1/route",
			map[string]any{
				"received_catalog_version": *req.CatalogVersion,
				"expected_catalog_version": digest,
			}, nil, &digest)
		return
	}

	allowMetered := req.AllowMetered != nil && *req.AllowMetered
	authorized := allowMetered && s.meteredAuthority.Authorized(token)

	placementPolicy := cat.DefaultPlacementPolicy
	if req.PlacementPolicy != nil {
		placementPolicy = *req.PlacementPolicy
	}

	in := routing.Input{
		Ambiguity:       req.Ambiguity,
		BlastRadius:     req.BlastRadius,
		ContextSize:     req.ContextSize,
		VolumeHint:      req.VolumeHint,
		Tags:            req.Tags,
		TouchedPaths:    req.TouchedPaths,
		PlacementPolicy: placementPolicy,
	}
	if allowMetered && !authorized {
		// The refusal is unconditional - authorization is decided before
		// routing ever selects anything. details.withheld, when present, is
		// a PURE COUNTERFACTUAL: this same request and catalog routed AS IF
		// authorized, reported only when that run selects a genuinely
		// catalog-declared metered candidate. Catalog 1.3.0 declares none,
		// so live 403s legitimately omit it; nothing is fabricated and the
		// shape carries harness/profile/cost class only - never credential
		// material. Decide is pure, so this has no side effects and cannot
		// weaken the refusal it decorates.
		var details map[string]any
		if cf := routing.Decide(cat, in, true, s.entitlementAvailability); cf.Outcome == routing.OutcomeOK && cf.Decision != nil && cf.Decision.CostClass == "metered" {
			details = map[string]any{"withheld": map[string]any{
				"harness":       cf.Decision.Harness,
				"model_profile": cf.Decision.ModelProfile,
				"cost_class":    cf.Decision.CostClass,
			}}
		}
		writeError(w, http.StatusForbidden, "metered_authorization_required",
			"The request set allow_metered: true, but the authenticated caller does not hold authority to approve billable spend. The flag is intent, not authority. Escalate to a principal that holds metered-spend authority; do not retry, and do not retry under a different credential.",
			"abort", "/v1/route", details, nil, &digest)
		return
	}

	result := routing.Decide(cat, in, authorized, s.entitlementAvailability)

	switch result.Outcome {
	case routing.OutcomeNoEligibleProfile:
		writeError(w, http.StatusConflict, "no_eligible_profile",
			"No approved harness/model_profile pairing satisfies this request against the current catalog. Relax a constraint (context_size, tags) or wait for the catalog to change, but do not retry unchanged.",
			"re-plan", "/v1/route", nil, nil, &digest)
		return
	case routing.OutcomeMeteredDenied:
		writeError(w, http.StatusConflict, "metered_denied",
			"Every option that could serve this request is billable and the request did not set allow_metered, and no non-metered substitute exists. Re-plan with explicit metered intent from a principal that holds metered-spend authority.",
			"re-plan", "/v1/route",
			map[string]any{"withheld": withheldDetails(result.Withheld)}, nil, &digest)
		return
	}

	now := s.store.Now()
	profile := buildExecutionProfileWire(result.Decision, digest, cat.DocumentVersion, now)
	writeJSON(w, http.StatusOK, profile)
}

func withheldDetails(w *routing.WithheldCandidate) map[string]any {
	return map[string]any{
		"harness":       w.Harness,
		"model_profile": w.ModelProfile,
		"cost_class":    "metered",
	}
}

// parseRouteRequest strictly decodes and validates a RouteRequest body
// against contracts/agent-router/openapi.yaml components.schemas.RouteRequest.
// DisallowUnknownFields enforces the schema's additionalProperties: false
// closure at the wire (STORY-035-10 gate case unknown_field).
func parseRouteRequest(body []byte) (*routeRequestWire, *invalidRequestError) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var req routeRequestWire
	if err := dec.Decode(&req); err != nil {
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &typeErr) {
			return nil, &invalidRequestError{Field: typeErr.Field, Message: fmt.Sprintf("field %q has the wrong type: %v", typeErr.Field, err)}
		}
		if strings.Contains(err.Error(), "unknown field") {
			field := strings.TrimSuffix(strings.TrimPrefix(err.Error(), `json: unknown field "`), `"`)
			return nil, &invalidRequestError{Field: field, Message: fmt.Sprintf("unknown field %q: RouteRequest is closed and does not accept it.", field)}
		}
		return nil, &invalidRequestError{Message: fmt.Sprintf("could not parse request body: %v", err)}
	}
	// Decoder.More() does not reliably report trailing non-whitespace after a
	// top-level object; the correct proof of "nothing follows" is a second
	// decode that must fail with io.EOF. `{...} true` and `{...}{...}` both
	// land here.
	var trailing json.RawMessage
	if err := dec.Decode(&trailing); err != io.EOF {
		return nil, &invalidRequestError{Message: "request body contains trailing data after the JSON object"}
	}

	if strings.TrimSpace(req.StoryID) == "" {
		return nil, &invalidRequestError{Field: "story_id", Message: "story_id is required and must not be empty"}
	}
	if strings.TrimSpace(req.Title) == "" {
		return nil, &invalidRequestError{Field: "title", Message: "title is required and must not be empty"}
	}
	if !validAmbiguity[req.Ambiguity] {
		return nil, &invalidRequestError{Field: "ambiguity", Message: fmt.Sprintf("ambiguity must be one of low, medium, high. Received %q.", req.Ambiguity)}
	}
	if !validBlastRadius[req.BlastRadius] {
		return nil, &invalidRequestError{Field: "blast_radius", Message: fmt.Sprintf("blast_radius must be one of low, medium, high, critical. Received %q.", req.BlastRadius)}
	}
	if req.ContextSize != nil && *req.ContextSize < 0 {
		return nil, &invalidRequestError{Field: "context_size", Message: "context_size must be >= 0"}
	}
	if req.VolumeHint != "" && !validVolumeHint[req.VolumeHint] {
		return nil, &invalidRequestError{Field: "volume_hint", Message: fmt.Sprintf("volume_hint must be one of low, medium, high. Received %q.", req.VolumeHint)}
	}
	if req.PlacementPolicy != nil && !validPlacementPolicy[*req.PlacementPolicy] {
		return nil, &invalidRequestError{Field: "placement_policy", Message: fmt.Sprintf("placement_policy must be one of prefer-warm-local, cluster-only, edge-only, any-24gb. Received %q.", *req.PlacementPolicy)}
	}
	if req.CatalogVersion != nil && !catalogVersionPattern.MatchString(*req.CatalogVersion) {
		return nil, &invalidRequestError{Field: "catalog_version", Message: "catalog_version must match ^sha256:[0-9a-f]{64}$"}
	}
	return &req, nil
}

// buildExecutionProfileWire assembles the wire ExecutionProfile from a pure
// routing.Decision plus the catalog provenance and clock. now is threaded
// through explicitly (capacity.Store's injected clock) so a fixed clock in
// tests produces byte-identical, reproducible output (amendment 3).
func buildExecutionProfileWire(d *routing.Decision, digest, docVersion string, now time.Time) executionProfileWire {
	fallbacks := make([]fallbackWire, 0, len(d.Fallbacks))
	for _, f := range d.Fallbacks {
		fallbacks = append(fallbacks, fallbackWire{
			Harness:           f.Pair.Harness,
			ModelProfile:      f.Pair.ModelProfile,
			CostClass:         f.CostClass,
			EntitlementPool:   f.EntitlementPool,
			PlacementRequired: f.PlacementRequired,
		})
	}

	profile := executionProfileWire{
		Harness:                d.Harness,
		ModelProfile:           d.ModelProfile,
		Effort:                 d.Effort,
		CostClass:              d.CostClass,
		EntitlementPool:        d.EntitlementPool,
		PlacementPolicy:        d.PlacementPolicy,
		PlacementRequired:      d.PlacementRequired,
		Fallbacks:              fallbacks,
		Metered:                d.Metered,
		Rationale:              buildRationale(d),
		CatalogVersion:         digest,
		CatalogDocumentVersion: docVersion,
		ExpiresAt:              now.Add(24 * time.Hour).UTC().Format(time.RFC3339),
	}
	if d.MeteredDenied != nil {
		profile.MeteredDenied = &meteredDeniedDTO{
			Code:       "metered_denied",
			Obligation: "re-plan",
			Message: fmt.Sprintf(
				"%s/%s scored highest but is billable, and the request did not set allow_metered. A non-metered option was returned instead. Authorizing the spend is a human decision, per attempt.",
				d.MeteredDenied.Harness, d.MeteredDenied.ModelProfile),
			Withheld: withheldCandidateDTO{
				Harness:      d.MeteredDenied.Harness,
				ModelProfile: d.MeteredDenied.ModelProfile,
				CostClass:    "metered",
			},
		}
	}
	return profile
}

func buildRationale(d *routing.Decision) string {
	var b strings.Builder
	fmt.Fprintf(&b, "Routed to band %q. Primary is %s/%s (%s", d.Band, d.Harness, d.ModelProfile, d.CostClass)
	if d.EntitlementPool != nil {
		fmt.Fprintf(&b, ", funded by %s", *d.EntitlementPool)
	}
	b.WriteString("). ")
	if d.PlacementRequired {
		fmt.Fprintf(&b, "The profile is local, so placement_policy %s binds and the client must resolve it via /v1/place before dispatch. ", d.PlacementPolicy)
	} else {
		b.WriteString("The profile is vendor-hosted and resolved by the harness itself, so placement_policy is carried but inert and no /v1/place call is needed. ")
	}
	if len(d.Fallbacks) == 0 {
		b.WriteString("No fallbacks: if the primary cannot start, this attempt should fail and be re-planned rather than silently substitute a weaker or costlier option.")
	} else {
		b.WriteString("Fallbacks, most preferred first: ")
		for i, f := range d.Fallbacks {
			if i > 0 {
				b.WriteString(", ")
			}
			fmt.Fprintf(&b, "%s/%s (%s)", f.Pair.Harness, f.Pair.ModelProfile, f.CostClass)
		}
		b.WriteString(".")
	}
	if d.MeteredDenied != nil {
		fmt.Fprintf(&b, " %s/%s scored highest but was withheld: it is billable and the request did not set allow_metered (invariant 4).", d.MeteredDenied.Harness, d.MeteredDenied.ModelProfile)
	}
	return b.String()
}
