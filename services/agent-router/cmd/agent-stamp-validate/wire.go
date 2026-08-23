// Command agent-stamp-validate is the local, non-Go-caller shape of
// STORY-035-12's final pre-execution policy validator: one stamp JSON (plus
// optional placement-evidence JSON) in, one Verdict JSON out. It is a leaf
// tool - it never launches anything, never talks to a network, and never
// shells out (see the boundary greps this story's gate 5 runs). Orca's
// launcher execs it immediately before harness launch; this binary itself
// has no opinion about what "launch" means.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/stampvalidate"
)

// The vocabulary maps below restate contracts/agent-router/schemas/
// execution-profile.schema.json's closed enums (harness, model_profile,
// placement_policy, entitlement_pool) for this CLI's own input-shape
// validation. Restated rather than imported from httpapi deliberately: this
// binary must not depend on the HTTP service package at all (it is not
// functionality the running router owns or executes - see this package's
// doc comment and ADE-BOUNDARY.md §7).
var (
	validHarness = map[string]bool{"claude": true, "codex": true, "devin": true}
	validProfile = map[string]bool{
		"local-code-fast": true, "local-code-standard": true, "local-general": true, "local-unrestricted": true,
		"claude/strong": true, "openai/strong": true, "devin/free": true, "minimax/strong": true,
	}
	validPolicy = map[string]bool{"prefer-warm-local": true, "cluster-only": true, "edge-only": true, "any-24gb": true}
	validPool   = map[string]bool{"anthropic-max": true, "openai-plus": true, "devin-free": true, "minimax-max": true}
	validEffort = map[string]bool{"low": true, "medium": true, "high": true, "xhigh": true}
	validCost   = map[string]bool{"free": true, "subscription": true, "metered": true}
	validStatus = map[string]bool{"placed": true, "unavailable": true}

	// The four vocabularies below restate contracts/agent-router/schemas/
	// place-result.schema.json's closed enums for the SAME reason: this
	// binary must not import httpapi, so PlacementEvidence.result - the
	// frozen PlaceResult shape, unmodified (owner clarification 1) - gets
	// its own structural validation here rather than a loose subset decode
	// (owner directive, 2026-08-23 correctness round).
	validPlacementName = map[string]bool{
		"kserve-a5000": true, "cachyos-7900xtx": true, "bazzite-5090": true, "laptop-rtx5000": true,
	}
	validReadiness = map[string]bool{"warm": true, "cached": true, "absent": true, "unknown": true}
	// validPlacedReasonCode / validUnavailableReasonCode are the two
	// disjoint call-level subsets of place-result.schema.json's reason.code
	// enum that are legal on result.reason depending on status - a placed_*
	// code on an unavailable result (or vice versa) is a schema violation,
	// not a policy question.
	validPlacedReasonCode = map[string]bool{
		"placed_warm": true, "placed_cached": true, "placed_cold": true, "placed_only_candidate": true,
	}
	validUnavailableReasonCode = map[string]bool{
		"no_eligible_placement": true, "policy_resolves_to_nothing": true,
		"all_candidates_withdrawn": true, "constraint_unsatisfiable": true,
	}
)

// stampWire mirrors contracts/agent-router/schemas/execution-stamp.schema.json
// field-for-field. Decoded with DisallowUnknownFields so an unrecognized
// property is rejected at the door, matching the schema's closure.
type stampWire struct {
	Harness                string        `json:"harness"`
	ModelProfile           string        `json:"model_profile"`
	Effort                 string        `json:"effort"`
	CostClass              string        `json:"cost_class"`
	EntitlementPool        *string       `json:"entitlement_pool"`
	Metered                bool          `json:"metered"`
	PlacementPolicy        string        `json:"placement_policy"`
	PlacementRequired      bool          `json:"placement_required"`
	CatalogVersion         string        `json:"catalog_version"`
	CatalogDocumentVersion string        `json:"catalog_document_version,omitempty"`
	ExpiresAt              string        `json:"expires_at"`
	Task                   taskWire      `json:"task"`
	Override               *overrideWire `json:"override"`
	CreatedAt              *string       `json:"created_at,omitempty"`
}

type taskWire struct {
	StoryID     string   `json:"story_id"`
	Title       string   `json:"title,omitempty"`
	Tags        []string `json:"tags"`
	ContextSize *int     `json:"context_size,omitempty"`
}

type overrideOriginalWire struct {
	Harness      string `json:"harness"`
	ModelProfile string `json:"model_profile"`
}

type overrideWire struct {
	Actor    string               `json:"actor"`
	At       string               `json:"at"`
	Original overrideOriginalWire `json:"original"`
	Reason   string               `json:"reason"`
}

// placementEvidenceWire mirrors contracts/agent-router/schemas/
// placement-evidence.schema.json field-for-field: {resolved_at, result}. The
// nested result is the FROZEN place-result.schema.json shape, unmodified
// (owner clarification 1) - placeResultWire below restates that same shape
// httpapi.placeResultWire already decodes, kept as its own type here so this
// binary has no import dependency on the httpapi package at all.
type placementEvidenceWire struct {
	ResolvedAt string          `json:"resolved_at"`
	Result     placeResultWire `json:"result"`
}

type placeResultWire struct {
	Status              string                 `json:"status"`
	Model               *string                `json:"model"`
	Placement           *string                `json:"placement"`
	Readiness           *string                `json:"readiness"`
	EstimatedColdStartS *float64               `json:"estimated_cold_start_s"`
	Headers             map[string]string      `json:"headers"`
	TTLSeconds          int                    `json:"ttl_seconds"`
	Reason              placeReasonWire        `json:"reason"`
	Alternatives        []placeAlternativeWire `json:"alternatives"`
	CatalogVersion      string                 `json:"catalog_version"`
}

type placeReasonWire struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type placeAlternativeWire struct {
	Placement           string          `json:"placement"`
	Model               string          `json:"model"`
	Readiness           string          `json:"readiness"`
	EstimatedColdStartS *float64        `json:"estimated_cold_start_s"`
	Eligible            bool            `json:"eligible"`
	Reason              placeReasonWire `json:"reason"`
}

// inputError is a malformed-input failure: exit code 2, no execution, no
// Verdict printed (frozen CLI exit semantics, amendment 7).
type inputError struct{ msg string }

func (e *inputError) Error() string { return e.msg }

func newInputError(format string, a ...any) *inputError {
	return &inputError{msg: fmt.Sprintf(format, a...)}
}

// decodeStrict is the same closed-schema decode idiom httpapi.parseRouteRequest
// and httpapi.parsePlaceRequest use: DisallowUnknownFields, then a second
// decode that must fail with io.EOF to prove nothing trails the JSON value.
func decodeStrict(raw []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &typeErr) {
			return newInputError("field %q has the wrong type: %v", typeErr.Field, err)
		}
		if strings.Contains(err.Error(), "unknown field") {
			field := strings.TrimSuffix(strings.TrimPrefix(err.Error(), `json: unknown field "`), `"`)
			return newInputError("unknown field %q: this shape is closed and does not accept it", field)
		}
		return newInputError("could not parse JSON: %v", err)
	}
	var trailing json.RawMessage
	if err := dec.Decode(&trailing); err != io.EOF {
		return newInputError("input contains trailing data after the JSON value")
	}
	return nil
}

// parseStamp strictly decodes and validates a stamp JSON document against
// contracts/agent-router/schemas/execution-stamp.schema.json.
func parseStamp(raw []byte) (stampvalidate.Stamp, error) {
	var w stampWire
	if err := decodeStrict(raw, &w); err != nil {
		return stampvalidate.Stamp{}, err
	}

	if !validHarness[w.Harness] {
		return stampvalidate.Stamp{}, newInputError("harness %q is not in the closed harness vocabulary", w.Harness)
	}
	if !validProfile[w.ModelProfile] {
		return stampvalidate.Stamp{}, newInputError("model_profile %q is not in the closed model_profile vocabulary", w.ModelProfile)
	}
	if !validEffort[w.Effort] {
		return stampvalidate.Stamp{}, newInputError("effort %q must be one of low, medium, high, xhigh", w.Effort)
	}
	if !validCost[w.CostClass] {
		return stampvalidate.Stamp{}, newInputError("cost_class %q must be one of free, subscription, metered", w.CostClass)
	}
	if w.EntitlementPool != nil && !validPool[*w.EntitlementPool] {
		return stampvalidate.Stamp{}, newInputError("entitlement_pool %q is not in the closed entitlement_pool vocabulary", *w.EntitlementPool)
	}
	if !validPolicy[w.PlacementPolicy] {
		return stampvalidate.Stamp{}, newInputError("placement_policy %q is not in the closed placement_policy vocabulary", w.PlacementPolicy)
	}
	if !catalogVersionPattern.MatchString(w.CatalogVersion) {
		return stampvalidate.Stamp{}, newInputError("catalog_version must match ^sha256:[0-9a-f]{64}$")
	}
	expiresAt, err := time.Parse(time.RFC3339, w.ExpiresAt)
	if err != nil {
		return stampvalidate.Stamp{}, newInputError("expires_at is not a valid RFC 3339 timestamp: %v", err)
	}
	if strings.TrimSpace(w.Task.StoryID) == "" {
		return stampvalidate.Stamp{}, newInputError("task.story_id is required and must not be empty")
	}
	if w.Task.ContextSize != nil && *w.Task.ContextSize < 0 {
		return stampvalidate.Stamp{}, newInputError("task.context_size must be >= 0")
	}

	stamp := stampvalidate.Stamp{
		Harness:                w.Harness,
		ModelProfile:           w.ModelProfile,
		Effort:                 w.Effort,
		CostClass:              w.CostClass,
		EntitlementPool:        w.EntitlementPool,
		Metered:                w.Metered,
		PlacementPolicy:        w.PlacementPolicy,
		PlacementRequired:      w.PlacementRequired,
		CatalogVersion:         w.CatalogVersion,
		CatalogDocumentVersion: w.CatalogDocumentVersion,
		ExpiresAt:              expiresAt,
		Task:                   stampvalidate.TaskIdentity{StoryID: w.Task.StoryID, Title: w.Task.Title, Tags: w.Task.Tags, ContextSize: w.Task.ContextSize},
	}

	if w.CreatedAt != nil {
		createdAt, err := time.Parse(time.RFC3339, *w.CreatedAt)
		if err != nil {
			return stampvalidate.Stamp{}, newInputError("created_at is not a valid RFC 3339 timestamp: %v", err)
		}
		stamp.CreatedAt = &createdAt
	}

	if w.Override != nil {
		at, err := time.Parse(time.RFC3339, w.Override.At)
		if err != nil {
			return stampvalidate.Stamp{}, newInputError("override.at is not a valid RFC 3339 timestamp: %v", err)
		}
		if strings.TrimSpace(w.Override.Actor) == "" {
			return stampvalidate.Stamp{}, newInputError("override.actor is required and must not be empty")
		}
		if strings.TrimSpace(w.Override.Reason) == "" {
			return stampvalidate.Stamp{}, newInputError("override.reason is required and must not be empty")
		}
		// override.original.{harness,model_profile} decode to the zero value
		// ("") when absent from the input, which would otherwise sail past
		// validation silently. They carry the same closed vocabularies as the
		// top-level harness/model_profile fields (execution-stamp.schema.json
		// #/$defs/override), so a malformed or missing original record must be
		// rejected here exactly like the top-level fields are, not decoded into
		// an unvalidated audit record.
		if !validHarness[w.Override.Original.Harness] {
			return stampvalidate.Stamp{}, newInputError("override.original.harness %q is not in the closed harness vocabulary", w.Override.Original.Harness)
		}
		if !validProfile[w.Override.Original.ModelProfile] {
			return stampvalidate.Stamp{}, newInputError("override.original.model_profile %q is not in the closed model_profile vocabulary", w.Override.Original.ModelProfile)
		}
		stamp.Override = &stampvalidate.Override{
			Actor: w.Override.Actor,
			At:    at,
			Original: stampvalidate.OverrideOriginal{
				Harness:      w.Override.Original.Harness,
				ModelProfile: w.Override.Original.ModelProfile,
			},
			Reason: w.Override.Reason,
		}
	}

	return stamp, nil
}

// validatePlaceResult enforces the structural invariants of the FROZEN
// place-result.schema.json that a generic strict-decode cannot express on
// its own: the closed header-key set, the ttl_seconds floor, the readiness/
// reason-code vocabularies, and the status-DEPENDENT {placed, unavailable}
// shape (owner directive, 2026-08-23 correctness round: "make the CLI
// parser honor that nested contract... do NOT change the frozen
// place-result.schema.json"). A result that violates any of these is
// malformed input (exit 2) exactly like a decode failure - these are
// schema-shape facts, not policy verdicts, and stampvalidate.checkPlacement
// must never be asked to reason about placement evidence that could not
// have come from a real /v1/place response in the first place.
func validatePlaceResult(w placeResultWire) error {
	// The allowed header key set is closed to "x-placement" regardless of
	// status (place-result.schema.json headers.propertyNames) - this is the
	// one channel a forged evidence document could otherwise use to smuggle
	// an arbitrary header past the caller into an agentgateway request.
	for key := range w.Headers {
		if key != "x-placement" {
			return newInputError("result.headers contains key %q: only \"x-placement\" is ever a legal header name", key)
		}
	}
	if w.TTLSeconds < 1 {
		return newInputError("result.ttl_seconds must be >= 1, got %d", w.TTLSeconds)
	}
	if w.EstimatedColdStartS != nil && *w.EstimatedColdStartS < 0 {
		return newInputError("result.estimated_cold_start_s must be >= 0 when present")
	}
	if strings.TrimSpace(w.Reason.Message) == "" {
		return newInputError("result.reason.message is required and must not be empty")
	}

	switch w.Status {
	case "placed":
		if w.Model == nil || strings.TrimSpace(*w.Model) == "" {
			return newInputError("result.model is required and must be non-empty when status is \"placed\"")
		}
		if w.Placement == nil || !validPlacementName[*w.Placement] {
			return newInputError("result.placement must be one of the closed placement vocabulary when status is \"placed\"")
		}
		if w.Readiness == nil || !validReadiness[*w.Readiness] {
			return newInputError("result.readiness must be one of warm, cached, absent, unknown when status is \"placed\"")
		}
		if _, ok := w.Headers["x-placement"]; !ok {
			return newInputError("result.headers must contain \"x-placement\" when status is \"placed\"")
		}
		if !validPlacedReasonCode[w.Reason.Code] {
			return newInputError("result.reason.code %q is not a legal reason on a \"placed\" result", w.Reason.Code)
		}
	case "unavailable":
		// An unavailable result is an EXPLICIT EMPTY PLACEMENT
		// (place-result.schema.json): every one of these fields is REQUIRED
		// to be absent/empty, never a silent fall-through a caller could
		// mistake for a real placement.
		if w.Model != nil {
			return newInputError("result.model must be null when status is \"unavailable\"")
		}
		if w.Placement != nil {
			return newInputError("result.placement must be null when status is \"unavailable\"")
		}
		if w.Readiness != nil {
			return newInputError("result.readiness must be null when status is \"unavailable\"")
		}
		if w.EstimatedColdStartS != nil {
			return newInputError("result.estimated_cold_start_s must be null when status is \"unavailable\"")
		}
		if len(w.Headers) != 0 {
			return newInputError("result.headers must be empty when status is \"unavailable\"")
		}
		if !validUnavailableReasonCode[w.Reason.Code] {
			return newInputError("result.reason.code %q is not a legal reason on an \"unavailable\" result", w.Reason.Code)
		}
	}
	return nil
}

// parseEvidence strictly decodes and validates a placement-evidence JSON
// document against contracts/agent-router/schemas/placement-evidence.schema.json.
func parseEvidence(raw []byte) (*stampvalidate.PlacementEvidence, error) {
	var w placementEvidenceWire
	if err := decodeStrict(raw, &w); err != nil {
		return nil, err
	}

	resolvedAt, err := time.Parse(time.RFC3339, w.ResolvedAt)
	if err != nil {
		return nil, newInputError("resolved_at is not a valid RFC 3339 timestamp: %v", err)
	}
	if !validStatus[w.Result.Status] {
		return nil, newInputError("result.status %q must be \"placed\" or \"unavailable\"", w.Result.Status)
	}
	if w.Result.CatalogVersion != "" && !catalogVersionPattern.MatchString(w.Result.CatalogVersion) {
		return nil, newInputError("result.catalog_version must match ^sha256:[0-9a-f]{64}$")
	}
	if err := validatePlaceResult(w.Result); err != nil {
		return nil, err
	}

	alts := make([]stampvalidate.PlaceAlternative, 0, len(w.Result.Alternatives))
	for _, a := range w.Result.Alternatives {
		alts = append(alts, stampvalidate.PlaceAlternative{
			Placement: a.Placement, Model: a.Model, Readiness: a.Readiness,
			EstimatedColdStartS: a.EstimatedColdStartS, Eligible: a.Eligible,
			Reason: stampvalidate.PlaceReason{Code: a.Reason.Code, Message: a.Reason.Message},
		})
	}

	return &stampvalidate.PlacementEvidence{
		ResolvedAt: resolvedAt,
		Result: stampvalidate.PlaceResult{
			Status:              w.Result.Status,
			Model:               w.Result.Model,
			Placement:           w.Result.Placement,
			Readiness:           w.Result.Readiness,
			EstimatedColdStartS: w.Result.EstimatedColdStartS,
			Headers:             w.Result.Headers,
			TTLSeconds:          w.Result.TTLSeconds,
			Reason:              stampvalidate.PlaceReason{Code: w.Result.Reason.Code, Message: w.Result.Reason.Message},
			Alternatives:        alts,
			CatalogVersion:      w.Result.CatalogVersion,
		},
	}, nil
}

// verdictWire mirrors contracts/agent-router/schemas/
// validation-verdict.schema.json field-for-field, the frozen stdout shape.
type verdictWire struct {
	Valid          bool        `json:"valid"`
	Checks         []checkWire `json:"checks"`
	CatalogVersion string      `json:"catalog_version"`
}

type checkWire struct {
	Name       string `json:"name"`
	Passed     bool   `json:"passed"`
	ReasonCode string `json:"reason_code"`
	Message    string `json:"message"`
}

func toVerdictWire(v stampvalidate.Verdict) verdictWire {
	checks := make([]checkWire, 0, len(v.Checks))
	for _, c := range v.Checks {
		checks = append(checks, checkWire{Name: c.Name, Passed: c.Passed, ReasonCode: c.ReasonCode, Message: c.Message})
	}
	return verdictWire{Valid: v.Valid, Checks: checks, CatalogVersion: v.CatalogVersion}
}
