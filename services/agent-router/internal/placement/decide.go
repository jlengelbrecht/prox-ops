// Package placement implements the dispatch-time /v1/place decision:
// resolving a stamped model_profile + placement_policy onto physical
// compute, against live capacity facts the caller (httpapi) has already
// resolved. It is pure and deterministic (STORY-035-11 "Deterministic"): the
// same catalog, facts and Input always yield the same Result, with no
// wall-clock, no randomness and no I/O.
package placement

import (
	"fmt"
	"sort"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
)

// Readiness mirrors place-result.schema.json $defs/readiness.
const (
	ReadinessWarm    = "warm"
	ReadinessCached  = "cached"
	ReadinessAbsent  = "absent"
	ReadinessUnknown = "unknown"
)

// Facts is one placement's live capacity facts, already resolved by httpapi
// from the capacity store and the catalog - this package never touches
// capacity.Store or heartbeat parsing directly, so it stays a pure function
// of its inputs.
type Facts struct {
	// HardEligible is the STORY-035-9a/9c hard-constraint verdict: not
	// INTERACTIVE/DRAINING/OFFLINE/unreachable, and catalog-selectable.
	HardEligible bool
	// HardReasonCode explains HardEligible=false, restricted to
	// place-result.schema.json $defs/candidate_ineligible_reason_code minus
	// constraint_unsatisfiable (this package derives that one itself from
	// VRAM facts, never from the caller).
	HardReasonCode string
	// Readiness is warm | cached | absent | unknown (owner ruling R11, never
	// collapsed). unknown is never treated as warm.
	Readiness string
	// EstimatedColdStartS is the catalog-derived estimate for this
	// placement's readiness, already resolved by the caller per owner
	// clarification 2 (warm->0, cached->load estimate, absent->nil,
	// KServe unknown->its provisional estimate). nil when no estimate
	// applies.
	EstimatedColdStartS *float64
	// FreeVramGB is the most recently heartbeated free VRAM, nil when no
	// live measurement exists (including every static KServe candidate,
	// which this package never invents a reading for).
	FreeVramGB *float64
	// MeasuredVramGB is the catalog's own capacity.vram_gb for this
	// placement - nil means unmeasured, and a hard VRAM floor can never be
	// satisfied from nil, nor from a nameplate figure (the caller never
	// passes one here).
	MeasuredVramGB *float64
}

// Input is the resolved subset of PlaceRequest this package decides against.
type Input struct {
	ModelProfile    string
	PlacementPolicy string
	// MinFreeVramGB is the caller's additional hard floor, on top of
	// whatever the policy sets (PlaceRequest.min_free_vram_gb).
	MinFreeVramGB *float64
	// Exclude is placements the caller does not want considered at all -
	// dropped before candidate construction, same as if the policy's
	// prefer_order never named them.
	Exclude []string
}

// Outcome enumerates what Decide can produce.
type Outcome int

const (
	OutcomeOK Outcome = iota
	OutcomeUnknownProfile
	OutcomeUnknownPolicy
)

// Candidate is one alternative in the result, eligible or not.
type Candidate struct {
	Placement           string
	Model               string
	Readiness           string
	EstimatedColdStartS *float64
	Eligible            bool
	ReasonCode          string
	ReasonMessage       string
}

// Result is Decide's return value. On OutcomeUnknownProfile/UnknownPolicy
// every other field is zero and httpapi renders the 404 taxonomy instead.
type Result struct {
	Outcome Outcome

	// Status is "placed" or "unavailable" - only meaningful when
	// Outcome == OutcomeOK.
	Status              string
	Model               *string
	Placement           *string
	Readiness           *string
	EstimatedColdStartS *float64
	ReasonCode          string
	ReasonMessage       string
	Alternatives        []Candidate
}

// candidate is Decide's internal working representation: a raw
// {placement, model} pair from the profile, in prefer_order position, plus
// its resolved facts and eligibility verdict.
type candidate struct {
	placement string
	model     string
	rank      int
	facts     Facts
	eligible  bool
	reason    string
	message   string
}

// Decide runs the normative placement policy (STORY-035-11 "Frozen placement
// semantics" and the owner's four normative clarifications) against the
// loaded catalog. cat must be non-nil; facts must carry an entry for every
// placement the catalog declares (httpapi.resolvePlaceFacts guarantees
// this).
func Decide(cat *catalog.Catalog, facts map[string]Facts, in Input) Result {
	prof, ok := findProfile(cat, in.ModelProfile)
	if !ok {
		return Result{Outcome: OutcomeUnknownProfile}
	}
	pol, ok := findPolicy(cat, in.PlacementPolicy)
	if !ok {
		return Result{Outcome: OutcomeUnknownPolicy}
	}

	excluded := toSet(in.Exclude)
	physical := map[string]string{} // placement -> model_id
	for _, ph := range prof.Physical {
		if ph.Placement == nil {
			continue
		}
		physical[*ph.Placement] = ph.ModelID
	}

	// Candidate construction is the policy's prefer_order INTERSECTED with
	// this profile's physical[] - never model.placements (owner
	// clarification 1: "never reconstruct candidates from
	// models[].placements"). A placement the policy does not name is not a
	// candidate at all, which is how cluster-only never chooses the edge
	// and edge-only never silently chooses the cluster.
	var cands []candidate
	for i, name := range pol.PreferOrder {
		modelID, ok := physical[name]
		if !ok || excluded[name] {
			continue
		}
		f := facts[name]
		elig, reason, msg := evaluate(cat, pol, in, modelID, name, f)
		cands = append(cands, candidate{
			placement: name, model: modelID, rank: i,
			facts: f, eligible: elig, reason: reason, message: msg,
		})
	}

	if len(cands) == 0 {
		return unavailable(pol, "policy_resolves_to_nothing",
			fmt.Sprintf("placement_policy %q names no placement that %q has a candidate for in the loaded catalog.", in.PlacementPolicy, in.ModelProfile),
			nil)
	}

	var eligible []candidate
	for _, c := range cands {
		if c.eligible {
			eligible = append(eligible, c)
		}
	}

	if len(eligible) == 0 {
		code, msg := emptyResultReason(pol, cands)
		return unavailable(pol, code, msg, toAlternatives(cands, nil))
	}

	ranked := reorderByWarmPreference(eligible, pol)
	winner := ranked[0]

	code := placedReasonCode(winner, len(eligible))
	msg := placedMessage(code, winner)

	// Alternatives: the rest of the ranked eligible list, then every
	// ineligible candidate, both in the order this run considered them.
	var altSource []candidate
	altSource = append(altSource, ranked[1:]...)
	for _, c := range cands {
		if !c.eligible {
			altSource = append(altSource, c)
		}
	}

	model := winner.model
	placementName := winner.placement
	readiness := winner.facts.Readiness

	return Result{
		Outcome:             OutcomeOK,
		Status:              "placed",
		Model:               &model,
		Placement:           &placementName,
		Readiness:           &readiness,
		EstimatedColdStartS: winner.facts.EstimatedColdStartS,
		ReasonCode:          code,
		ReasonMessage:       msg,
		Alternatives:        toAlternatives(altSource, &winner),
	}
}

func unavailable(pol catalog.Policy, code, message string, alternatives []Candidate) Result {
	return Result{
		Outcome:       OutcomeOK,
		Status:        "unavailable",
		ReasonCode:    code,
		ReasonMessage: message,
		Alternatives:  alternatives,
	}
}

// evaluate applies the hard-constraint verdict already computed by the
// caller (f.HardEligible) plus the VRAM feasibility rules owner
// clarification 3 states, never inventing a margin and never substituting a
// nameplate figure for a missing measurement.
func evaluate(cat *catalog.Catalog, pol catalog.Policy, in Input, modelID, placementName string, f Facts) (eligible bool, reasonCode, message string) {
	if !f.HardEligible {
		return false, f.HardReasonCode, hardReasonMessage(f.HardReasonCode, placementName)
	}

	// allow_cold_start: false forbids waking a scaled-to-zero placement
	// (catalog policy vocabulary). Only a placement that DECLARES
	// scale_to_zero: true is governed by this rule, and only known-warm
	// proves no wake is required - cached, absent and unknown all fail
	// closed (unknown cannot prove the placement is not scaled to zero
	// right now, and cached/absent are never treated as warm).
	if !pol.AllowColdStart {
		if pl, ok := findPlacement(cat, placementName); ok && pl.ScaleToZero != nil && *pl.ScaleToZero {
			if f.Readiness != ReadinessWarm {
				return false, "constraint_unsatisfiable", fmt.Sprintf(
					"the policy forbids cold starts and %s is declared scale-to-zero; only a known-warm placement satisfies that, and %s is %s.",
					placementName, placementName, f.Readiness)
			}
		}
	}

	// Policy's own min_vram_gb: a hard floor on MEASURED catalog capacity,
	// satisfiable only by a real measurement (catalog README "Capacity
	// numbers are sourced, not assumed").
	if pol.MinVramGB != nil {
		if f.MeasuredVramGB == nil || *f.MeasuredVramGB < *pol.MinVramGB {
			return false, "constraint_unsatisfiable", fmt.Sprintf(
				"%s requires a measured GPU capacity of at least %g GB; %s reports %s.",
				placementNounPhrase(placementName), *pol.MinVramGB, placementName, measuredOrUnmeasured(f.MeasuredVramGB))
		}
	}

	// Additional free-VRAM floor: the policy's own min_free_vram_gb and the
	// request's min_free_vram_gb are ADDITIVE hard floors (PlaceRequest
	// description: "on top of whatever the policy sets"), so the effective
	// floor is whichever is higher. Unknown free VRAM can never satisfy
	// either.
	if floor := effectiveMinFree(pol, in); floor != nil {
		if f.FreeVramGB == nil || *f.FreeVramGB < *floor {
			return false, "constraint_unsatisfiable", fmt.Sprintf(
				"%s requires at least %g GB of free VRAM; %s reports %s.",
				placementNounPhrase(placementName), *floor, placementName, freeOrUnknown(f.FreeVramGB))
		}
	}

	// Model load feasibility: already-warm never re-checked (the model
	// already holds its VRAM). Not warm, with a live free-VRAM reading and a
	// catalog VRAM budget for the model, requires headroom to load it.
	if f.Readiness != ReadinessWarm {
		model, ok := cat.Models[modelID]
		if ok && model.VramGbEstimate != nil && f.FreeVramGB != nil {
			if *f.FreeVramGB < *model.VramGbEstimate {
				return false, "constraint_unsatisfiable", fmt.Sprintf(
					"loading %s needs an estimated %g GB of VRAM; %s reports only %g GB free.",
					modelID, *model.VramGbEstimate, placementName, *f.FreeVramGB)
			}
		}
	}

	return true, "", ""
}

// effectiveMinFree combines the policy's min_free_vram_gb with the request's
// own floor: both are hard floors and the higher one governs.
func effectiveMinFree(pol catalog.Policy, in Input) *float64 {
	switch {
	case pol.MinFreeVramGB == nil:
		return in.MinFreeVramGB
	case in.MinFreeVramGB == nil:
		return pol.MinFreeVramGB
	case *in.MinFreeVramGB > *pol.MinFreeVramGB:
		return in.MinFreeVramGB
	default:
		return pol.MinFreeVramGB
	}
}

func hardReasonMessage(code, placementName string) string {
	switch code {
	case "withdrawn_interactive":
		return fmt.Sprintf("%s last reported state INTERACTIVE: a human has the GPU.", placementName)
	case "withdrawn_draining":
		return fmt.Sprintf("%s last reported state DRAINING: finishing in-flight work, refusing new.", placementName)
	case "offline":
		return fmt.Sprintf("%s is OFFLINE: either it reported so explicitly, or it has been silent past the offline window.", placementName)
	case "not_yet_observed":
		return fmt.Sprintf("%s has not sent its first heartbeat in this router process's lifetime yet.", placementName)
	case "not_selectable":
		return fmt.Sprintf("%s is declared in the catalog but not selectable (status: planned or selectable: false).", placementName)
	default:
		return fmt.Sprintf("%s is not eligible.", placementName)
	}
}

func placementNounPhrase(name string) string { return fmt.Sprintf("this policy on %s", name) }

func measuredOrUnmeasured(v *float64) string {
	if v == nil {
		return "no measurement"
	}
	return fmt.Sprintf("%g GB", *v)
}

func freeOrUnknown(v *float64) string {
	if v == nil {
		return "no live measurement"
	}
	return fmt.Sprintf("%g GB free", *v)
}

// emptyResultReason picks the result-level unavailable code when no
// candidate is eligible, in the priority order the frozen taxonomy implies:
// a constraint that excludes every candidate is reported as such; failing
// that, a policy the catalog says cannot resolve today is reported as that
// static fact rather than a live symptom of it; failing that, a set that is
// ineligible purely because every member withdrew itself (interactive,
// draining, or offline) gets the more specific code; anything else is the
// generic dynamic case.
func emptyResultReason(pol catalog.Policy, cands []candidate) (code, message string) {
	allConstraint, allWithdrawn := true, true
	for _, c := range cands {
		if c.reason != "constraint_unsatisfiable" {
			allConstraint = false
		}
		if c.reason != "withdrawn_interactive" && c.reason != "withdrawn_draining" && c.reason != "offline" {
			allWithdrawn = false
		}
	}
	switch {
	case allConstraint:
		return "constraint_unsatisfiable", "Every candidate this policy names fails a hard VRAM or capacity constraint."
	case !pol.ResolvesToday:
		return "policy_resolves_to_nothing", "This policy names no placement that can resolve in the loaded catalog today."
	case allWithdrawn:
		return "all_candidates_withdrawn", "Every candidate this policy names has withdrawn itself (interactive, draining, or offline)."
	default:
		return "no_eligible_placement", "Candidates exist but none is eligible right now."
	}
}

// reorderByWarmPreference implements owner clarification 1's ranking rule
// exactly: strict puts every known-warm candidate ahead of every non-warm
// one, preserving prefer_order within each group; weighted moves a warm
// candidate up exactly warm_bonus_rank_shift positions, deterministically;
// ignored leaves prefer_order (the list's existing order) untouched. list
// must already be in prefer_order order.
func reorderByWarmPreference(list []candidate, pol catalog.Policy) []candidate {
	switch pol.WarmPreference {
	case "strict":
		var warm, cold []candidate
		for _, c := range list {
			if c.facts.Readiness == ReadinessWarm {
				warm = append(warm, c)
			} else {
				cold = append(cold, c)
			}
		}
		return append(warm, cold...)
	case "weighted":
		shift := 0
		if pol.WarmBonusRankShift != nil {
			shift = *pol.WarmBonusRankShift
		}
		type keyed struct {
			c        candidate
			key, idx int
		}
		arr := make([]keyed, len(list))
		for i, c := range list {
			key := i
			if c.facts.Readiness == ReadinessWarm {
				key -= shift
			}
			arr[i] = keyed{c, key, i}
		}
		// A promoted warm candidate must land exactly shift positions to
		// the left (bounded at index 0), so on a key TIE the warm candidate
		// wins - tie-breaking by original index instead would leave the
		// cold incumbent ahead and the promotion would move zero positions
		// ([A,B,C] with B warm and shift 1 must yield [B,A,C], not [A,B,C]).
		// Warm-vs-warm and cold-vs-cold ties keep original order, so the
		// result stays deterministic and relative order is preserved.
		sort.SliceStable(arr, func(i, j int) bool {
			if arr[i].key != arr[j].key {
				return arr[i].key < arr[j].key
			}
			iw := arr[i].c.facts.Readiness == ReadinessWarm
			jw := arr[j].c.facts.Readiness == ReadinessWarm
			if iw != jw {
				return iw
			}
			return arr[i].idx < arr[j].idx
		})
		out := make([]candidate, len(arr))
		for i, k := range arr {
			out[i] = k.c
		}
		return out
	default: // ignored
		return list
	}
}

func placedReasonCode(winner candidate, eligibleCount int) string {
	if eligibleCount == 1 {
		return "placed_only_candidate"
	}
	switch winner.facts.Readiness {
	case ReadinessWarm:
		return "placed_warm"
	case ReadinessCached:
		return "placed_cached"
	default:
		return "placed_cold"
	}
}

func placedMessage(code string, winner candidate) string {
	switch code {
	case "placed_only_candidate":
		return fmt.Sprintf("%s is the only eligible candidate for this profile under this policy.", winner.placement)
	case "placed_warm":
		return fmt.Sprintf("%s is already warm with %s loaded, so it can answer with no load step.", winner.placement, winner.model)
	case "placed_cached":
		return fmt.Sprintf("%s has %s cached locally: a load, not a download.", winner.placement, winner.model)
	default:
		return fmt.Sprintf("%s was chosen on policy order; a cold start is expected.", winner.placement)
	}
}

func toAlternatives(cands []candidate, winner *candidate) []Candidate {
	out := make([]Candidate, 0, len(cands))
	for _, c := range cands {
		if winner != nil && c.placement == winner.placement && c.model == winner.model {
			continue
		}
		reason, msg := c.reason, c.message
		if c.eligible {
			reason = altReasonForEligible(winner, c)
			msg = altMessageForEligible(reason, c)
		}
		out = append(out, Candidate{
			Placement:           c.placement,
			Model:               c.model,
			Readiness:           c.facts.Readiness,
			EstimatedColdStartS: c.facts.EstimatedColdStartS,
			Eligible:            c.eligible,
			ReasonCode:          reason,
			ReasonMessage:       msg,
		})
	}
	return out
}

func altReasonForEligible(winner *candidate, alt candidate) string {
	if winner != nil && winner.facts.Readiness == ReadinessWarm && alt.facts.Readiness != ReadinessWarm {
		return "not_selected_colder"
	}
	return "not_selected_lower_rank"
}

func altMessageForEligible(reason string, alt candidate) string {
	if reason == "not_selected_colder" {
		return fmt.Sprintf("%s is eligible but not known to be warm, and the chosen candidate is.", alt.placement)
	}
	return fmt.Sprintf("%s is eligible but ranked below the chosen candidate.", alt.placement)
}

func toSet(items []string) map[string]bool {
	m := make(map[string]bool, len(items))
	for _, i := range items {
		m[i] = true
	}
	return m
}

func findProfile(cat *catalog.Catalog, name string) (catalog.Profile, bool) {
	for _, e := range cat.Profiles {
		if e.Name == name {
			return e.Value, true
		}
	}
	return catalog.Profile{}, false
}

func findPolicy(cat *catalog.Catalog, name string) (catalog.Policy, bool) {
	for _, e := range cat.Policies {
		if e.Name == name {
			return e.Value, true
		}
	}
	return catalog.Policy{}, false
}

func findPlacement(cat *catalog.Catalog, name string) (catalog.Placement, bool) {
	for _, e := range cat.Placements {
		if e.Name == name {
			return e.Value, true
		}
	}
	return catalog.Placement{}, false
}
