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
	StoryID string   `json:"story_id"`
	Title   string   `json:"title,omitempty"`
	Tags    []string `json:"tags"`
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
		Task:                   stampvalidate.TaskIdentity{StoryID: w.Task.StoryID, Title: w.Task.Title, Tags: w.Task.Tags},
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
