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
	// validCandidateReasonCode is place-result.schema.json's
	// candidate_reason_code enum - the subset legal ONLY inside
	// alternatives[].reason.code. A placed_* code or a result-level
	// unavailable code (no_eligible_placement etc.) is illegal here even
	// though both are members of the overall reason.code enum: they
	// describe the WHOLE CALL, not one candidate.
	validCandidateReasonCode = map[string]bool{
		"not_selected_lower_rank": true, "not_selected_colder": true,
		"withdrawn_interactive": true, "withdrawn_draining": true,
		"offline": true, "not_yet_observed": true, "not_selectable": true,
		"constraint_unsatisfiable": true,
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

// isRawNull reports whether a raw JSON value is the literal null token.
func isRawNull(v json.RawMessage) bool {
	return string(bytes.TrimSpace(v)) == "null"
}

// rawObject decodes raw into a map of its top-level members' own raw JSON
// values, rejecting a null or non-object value outright. It is the minimal
// presence-checking mechanism this parser adds alongside typed decoding
// (owner directive, 2026-08-23 FINAL round): requireKeys below then asks
// "was this required member actually present in the document" - a question
// typed decoding into a Go struct cannot answer on its own, because an
// OMITTED bool/int/string/array/map field and one explicitly set to its
// legal Go zero value (false, 0, "", nil) decode identically, and because
// unmarshaling a JSON `null` into a non-pointer Go field is a silent no-op,
// not an error. This is intentionally NOT a general JSON Schema validator -
// it answers exactly that one presence question, for the specific required
// members the committed schemas name.
func rawObject(raw json.RawMessage) (map[string]json.RawMessage, error) {
	if isRawNull(raw) {
		return nil, newInputError("expected a JSON object, got null")
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(raw, &m); err != nil {
		return nil, newInputError("expected a JSON object: %v", err)
	}
	return m, nil
}

// requireKeys fails unless every name in required is a KEY of obj - present
// in the document, whatever its value. It never judges the value itself: a
// present key whose value is JSON null passes here even where the schema
// forbids null for that member, because some required members ARE
// legitimately nullable (entitlement_pool, override) and some are not
// (task, tags, headers, alternatives, reason...) - that is always a
// separate, per-field check made after this one.
func requireKeys(obj map[string]json.RawMessage, context string, required ...string) error {
	for _, key := range required {
		if _, ok := obj[key]; !ok {
			return newInputError("%s.%s is required and must be present", context, key)
		}
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

	top, err := rawObject(raw)
	if err != nil {
		return stampvalidate.Stamp{}, err
	}
	if err := requireKeys(top, "stamp",
		"harness", "model_profile", "effort", "cost_class", "entitlement_pool",
		"metered", "placement_policy", "placement_required", "catalog_version",
		"expires_at", "task", "override",
	); err != nil {
		return stampvalidate.Stamp{}, err
	}
	// Required BOOLEAN presence is not enough: encoding/json accepts JSON
	// null into a non-pointer bool without error, leaving the zero value
	// false - so an explicit null would silently read as a legal false.
	// The frozen schemas type these members as boolean, not boolean|null.
	if isRawNull(top["metered"]) {
		return stampvalidate.Stamp{}, newInputError("stamp.metered must be a boolean, not null")
	}
	if isRawNull(top["placement_required"]) {
		return stampvalidate.Stamp{}, newInputError("stamp.placement_required must be a boolean, not null")
	}
	taskObj, err := rawObject(top["task"])
	if err != nil {
		return stampvalidate.Stamp{}, newInputError("task must be an object: %v", err)
	}
	if err := requireKeys(taskObj, "task", "story_id", "tags"); err != nil {
		return stampvalidate.Stamp{}, err
	}
	if isRawNull(taskObj["tags"]) {
		// tags is policy input to forbidden_for (rule 2): a decoder that let
		// null silently become an empty/nil slice would let a forged or
		// truncated document quietly drop security tags. [] is the only
		// legal way to state "no tags"; null is not.
		return stampvalidate.Stamp{}, newInputError("task.tags must not be null - an empty array ([]) is the legal way to state no tags")
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
		overrideObj, err := rawObject(top["override"])
		if err != nil {
			return stampvalidate.Stamp{}, newInputError("override must be an object: %v", err)
		}
		if err := requireKeys(overrideObj, "override", "actor", "at", "original", "reason"); err != nil {
			return stampvalidate.Stamp{}, err
		}
		originalObj, err := rawObject(overrideObj["original"])
		if err != nil {
			return stampvalidate.Stamp{}, newInputError("override.original must be an object: %v", err)
		}
		if err := requireKeys(originalObj, "override.original", "harness", "model_profile"); err != nil {
			return stampvalidate.Stamp{}, err
		}

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
// its own: required-member PRESENCE (not merely typed-zero-value validity),
// the closed header-key set, the ttl_seconds floor, the readiness/
// reason-code vocabularies, and the status-DEPENDENT {placed, unavailable}
// shape (owner directive, 2026-08-23 FINAL round: "make the CLI parser
// honor that nested contract... do NOT change the frozen
// place-result.schema.json"). A result that violates any of these is
// malformed input (exit 2) exactly like a decode failure - these are
// schema-shape facts, not policy verdicts, and stampvalidate.checkPlacement
// must never be asked to reason about placement evidence that could not
// have come from a real /v1/place response in the first place. obj is the
// raw-object presence view of the SAME result document w was decoded from.
func validatePlaceResult(w placeResultWire, obj map[string]json.RawMessage) error {
	if err := requireKeys(obj, "result",
		"status", "model", "placement", "readiness", "estimated_cold_start_s",
		"headers", "ttl_seconds", "reason", "alternatives", "catalog_version",
	); err != nil {
		return err
	}
	// headers, reason and alternatives are typed object/object/array
	// throughout place-result.schema.json - never legally null, unlike
	// model/placement/readiness/estimated_cold_start_s, which the status
	// switch below allows to be null exactly when status is "unavailable".
	if isRawNull(obj["headers"]) {
		return newInputError("result.headers must not be null")
	}
	if isRawNull(obj["reason"]) {
		return newInputError("result.reason must not be null")
	}
	if isRawNull(obj["alternatives"]) {
		return newInputError("result.alternatives must not be null - an empty array ([]) is the legal way to state no alternatives")
	}
	reasonObj, err := rawObject(obj["reason"])
	if err != nil {
		return newInputError("result.reason must be an object: %v", err)
	}
	if err := requireKeys(reasonObj, "result.reason", "code", "message"); err != nil {
		return err
	}

	// catalog_version is REQUIRED and must match the frozen digest pattern
	// UNCONDITIONALLY - a missing or empty value is malformed input (exit
	// 2), never silently accepted here and left to fail closed later as a
	// policy-invalid placement_catalog_version_mismatch verdict (exit 1).
	if !catalogVersionPattern.MatchString(w.CatalogVersion) {
		return newInputError("result.catalog_version must match ^sha256:[0-9a-f]{64}$")
	}

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

// parseAlternatives validates every entry of result.alternatives against
// the frozen place-result.schema.json #/$defs/alternative shape before
// constructing stampvalidate.PlaceAlternative - previously these were
// copied straight through with no validation at all (owner directive,
// 2026-08-23 FINAL round). raw is the already-confirmed-non-null
// alternatives array's own raw JSON (obj["alternatives"] from the caller),
// used only for the per-element presence check (eligible in particular:
// its Go zero value, false, is a legal value, so only a raw presence check
// can tell "omitted" from "explicitly false").
func parseAlternatives(typed []placeAlternativeWire, raw json.RawMessage) ([]stampvalidate.PlaceAlternative, error) {
	var rawElems []json.RawMessage
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &rawElems); err != nil {
			return nil, newInputError("result.alternatives must be an array: %v", err)
		}
	}
	if len(rawElems) != len(typed) {
		// decodeStrict already proved raw parses into exactly `typed` with
		// this many elements; this is a defensive invariant, not a
		// reachable caller input.
		return nil, newInputError("result.alternatives: internal decode mismatch")
	}

	alts := make([]stampvalidate.PlaceAlternative, 0, len(typed))
	for i, a := range typed {
		context := fmt.Sprintf("result.alternatives[%d]", i)
		obj, err := rawObject(rawElems[i])
		if err != nil {
			return nil, newInputError("%s must be an object: %v", context, err)
		}
		if err := requireKeys(obj, context, "placement", "model", "readiness", "eligible", "reason"); err != nil {
			return nil, err
		}
		// Same null-masking hole as the stamp's required booleans: null
		// decodes into bool as false, and eligible: false is a LEGAL value
		// here, so only the raw bytes can tell the two apart.
		if isRawNull(obj["eligible"]) {
			return nil, newInputError("%s.eligible must be a boolean, not null", context)
		}
		if !validPlacementName[a.Placement] {
			return nil, newInputError("%s.placement must be one of the closed placement vocabulary", context)
		}
		if strings.TrimSpace(a.Model) == "" {
			return nil, newInputError("%s.model is required and must be non-empty", context)
		}
		if !validReadiness[a.Readiness] {
			return nil, newInputError("%s.readiness must be one of warm, cached, absent, unknown", context)
		}
		if a.EstimatedColdStartS != nil && *a.EstimatedColdStartS < 0 {
			return nil, newInputError("%s.estimated_cold_start_s must be >= 0 when present", context)
		}
		reasonObj, err := rawObject(obj["reason"])
		if err != nil {
			return nil, newInputError("%s.reason must be an object: %v", context, err)
		}
		if err := requireKeys(reasonObj, context+".reason", "code", "message"); err != nil {
			return nil, err
		}
		if strings.TrimSpace(a.Reason.Message) == "" {
			return nil, newInputError("%s.reason.message is required and must not be empty", context)
		}
		// Restricted to the CANDIDATE-LEVEL subset (place-result.schema.json
		// $defs/candidate_reason_code): a placed_* or result-level
		// unavailable code here is a schema violation, not a policy
		// question - never invented wire vocabulary, just the frozen subset
		// this position is actually allowed to carry.
		if !validCandidateReasonCode[a.Reason.Code] {
			return nil, newInputError("%s.reason.code %q is not a legal candidate-level reason code", context, a.Reason.Code)
		}

		alts = append(alts, stampvalidate.PlaceAlternative{
			Placement: a.Placement, Model: a.Model, Readiness: a.Readiness,
			EstimatedColdStartS: a.EstimatedColdStartS, Eligible: a.Eligible,
			Reason: stampvalidate.PlaceReason{Code: a.Reason.Code, Message: a.Reason.Message},
		})
	}
	return alts, nil
}

// parseEvidence strictly decodes and validates a placement-evidence JSON
// document against contracts/agent-router/schemas/placement-evidence.schema.json.
func parseEvidence(raw []byte) (*stampvalidate.PlacementEvidence, error) {
	var w placementEvidenceWire
	if err := decodeStrict(raw, &w); err != nil {
		return nil, err
	}

	top, err := rawObject(raw)
	if err != nil {
		return nil, err
	}
	if err := requireKeys(top, "evidence", "resolved_at", "result"); err != nil {
		return nil, err
	}
	resultObj, err := rawObject(top["result"])
	if err != nil {
		return nil, newInputError("result must be an object: %v", err)
	}

	resolvedAt, err := time.Parse(time.RFC3339, w.ResolvedAt)
	if err != nil {
		return nil, newInputError("resolved_at is not a valid RFC 3339 timestamp: %v", err)
	}
	if !validStatus[w.Result.Status] {
		return nil, newInputError("result.status %q must be \"placed\" or \"unavailable\"", w.Result.Status)
	}
	if err := validatePlaceResult(w.Result, resultObj); err != nil {
		return nil, err
	}

	alts, err := parseAlternatives(w.Result.Alternatives, resultObj["alternatives"])
	if err != nil {
		return nil, err
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
