package placement

import (
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
)

func fptr(f float64) *float64 { return &f }
func iptr(i int) *int         { return &i }
func bptr(b bool) *bool       { return &b }
func sptr(s string) *string   { return &s }

func cands(readiness map[string]string, order []string) []candidate {
	out := make([]candidate, 0, len(order))
	for i, name := range order {
		out = append(out, candidate{
			placement: name, model: "m", rank: i, eligible: true,
			facts: Facts{HardEligible: true, Readiness: readiness[name]},
		})
	}
	return out
}

func names(list []candidate) []string {
	out := make([]string, len(list))
	for i, c := range list {
		out[i] = c.placement
	}
	return out
}

func assertOrder(t *testing.T, got []candidate, want ...string) {
	t.Helper()
	g := names(got)
	if len(g) != len(want) {
		t.Fatalf("order = %v, want %v", g, want)
	}
	for i := range want {
		if g[i] != want[i] {
			t.Fatalf("order = %v, want %v", g, want)
		}
	}
}

// Weighted promotion moves a known-warm candidate left by exactly
// warm_bonus_rank_shift eligible positions, bounded at index 0. The
// regression this pins: a shift of 1 produces a key TIE with the cold
// incumbent, and tie-breaking by original index left the warm candidate
// unmoved ([A,B,C] with B warm and shift 1 stayed [A,B,C]).
func TestReorderWeighted_exact_promotion(t *testing.T) {
	pol := func(shift int) catalog.Policy {
		return catalog.Policy{WarmPreference: "weighted", WarmBonusRankShift: iptr(shift)}
	}
	order := []string{"A", "B", "C"}

	t.Run("B_warm_shift_1", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessWarm, "C": ReadinessAbsent}, order), pol(1))
		assertOrder(t, got, "B", "A", "C")
	})
	t.Run("C_warm_shift_1", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessAbsent, "C": ReadinessWarm}, order), pol(1))
		assertOrder(t, got, "A", "C", "B")
	})
	t.Run("shift_0_unchanged", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessWarm, "C": ReadinessAbsent}, order), pol(0))
		assertOrder(t, got, "A", "B", "C")
	})
	t.Run("C_warm_shift_2_exact", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessAbsent, "C": ReadinessWarm}, order), pol(2))
		assertOrder(t, got, "C", "A", "B")
	})
	t.Run("bounded_at_index_0", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessWarm, "C": ReadinessAbsent}, order), pol(5))
		assertOrder(t, got, "B", "A", "C")
	})
	t.Run("cached_gets_no_bonus", func(t *testing.T) {
		got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessAbsent, "B": ReadinessCached, "C": ReadinessAbsent}, order), pol(1))
		assertOrder(t, got, "A", "B", "C")
	})
	t.Run("deterministic", func(t *testing.T) {
		in := map[string]string{"A": ReadinessAbsent, "B": ReadinessWarm, "C": ReadinessWarm}
		first := names(reorderByWarmPreference(cands(in, order), pol(1)))
		for i := 0; i < 20; i++ {
			again := names(reorderByWarmPreference(cands(in, order), pol(1)))
			for j := range first {
				if again[j] != first[j] {
					t.Fatalf("run %d: order %v != first run %v", i, again, first)
				}
			}
		}
	})
}

// strict and ignored keep their existing semantics, untouched by the
// weighted fix.
func TestReorderStrictAndIgnored_unchanged(t *testing.T) {
	order := []string{"A", "B", "C"}
	readiness := map[string]string{"A": ReadinessAbsent, "B": ReadinessAbsent, "C": ReadinessWarm}

	got := reorderByWarmPreference(cands(readiness, order), catalog.Policy{WarmPreference: "strict"})
	assertOrder(t, got, "C", "A", "B")

	got = reorderByWarmPreference(cands(readiness, order), catalog.Policy{WarmPreference: "ignored"})
	assertOrder(t, got, "A", "B", "C")
}

// coldStartCatalog is a synthetic catalog with one KServe-like candidate
// declared scale_to_zero: true - the shape allow_cold_start: false exists
// to govern. No Kubernetes client, no probe: the declaration is catalog
// data and readiness comes from the caller-resolved Facts.
func coldStartCatalog(allowColdStart bool) *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion: "9.9.9",
		Placements: []catalog.Entry[catalog.Placement]{
			{Name: "kserve-like", Value: catalog.Placement{
				Status: "available", Selectable: true, Kind: "kserve",
				Capacity:    catalog.Capacity{VramGB: fptr(24)},
				ScaleToZero: bptr(true),
			}},
		},
		Models: map[string]catalog.Model{
			"m": {Hosting: "local", Placements: []string{"kserve-like"}},
		},
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "p", Value: catalog.Profile{
				Selectable: true,
				Physical:   []catalog.ProfilePhysical{{ModelID: "m", Placement: sptr("kserve-like")}},
			}},
		},
		Policies: []catalog.Entry[catalog.Policy]{
			{Name: "pol", Value: catalog.Policy{
				PreferOrder: []string{"kserve-like"}, WarmPreference: "strict",
				AllowColdStart: allowColdStart, ResolvesToday: true,
			}},
		},
	}
}

// allow_cold_start: false forbids selecting a scale_to_zero: true candidate
// that is not KNOWN warm - unknown cannot prove no wake is required, and
// cached/absent are never treated as warm. allow_cold_start: true keeps
// today's behavior. The rejection reuses the frozen reason vocabulary
// (constraint_unsatisfiable), never a new wire code.
func TestDecide_allow_cold_start_enforced(t *testing.T) {
	in := Input{ModelProfile: "p", PlacementPolicy: "pol"}

	for _, readiness := range []string{ReadinessUnknown, ReadinessCached, ReadinessAbsent} {
		t.Run("forbidden_"+readiness, func(t *testing.T) {
			facts := map[string]Facts{"kserve-like": {HardEligible: true, Readiness: readiness}}
			res := Decide(coldStartCatalog(false), facts, in)
			if res.Status != "unavailable" {
				t.Fatalf("status = %q, want unavailable (readiness %s cannot prove no wake)", res.Status, readiness)
			}
			if len(res.Alternatives) != 1 || res.Alternatives[0].Eligible {
				t.Fatalf("alternatives = %+v, want the single candidate ineligible", res.Alternatives)
			}
			if res.Alternatives[0].ReasonCode != "constraint_unsatisfiable" {
				t.Fatalf("reason = %q, want constraint_unsatisfiable (frozen vocabulary)", res.Alternatives[0].ReasonCode)
			}
		})
	}

	t.Run("warm_satisfies", func(t *testing.T) {
		zero := 0.0
		facts := map[string]Facts{"kserve-like": {HardEligible: true, Readiness: ReadinessWarm, EstimatedColdStartS: &zero}}
		res := Decide(coldStartCatalog(false), facts, in)
		if res.Status != "placed" {
			t.Fatalf("status = %q, want placed: known-warm needs no wake", res.Status)
		}
	})

	t.Run("allowed_keeps_current_behavior", func(t *testing.T) {
		facts := map[string]Facts{"kserve-like": {HardEligible: true, Readiness: ReadinessUnknown}}
		res := Decide(coldStartCatalog(true), facts, in)
		if res.Status != "placed" {
			t.Fatalf("status = %q, want placed under allow_cold_start: true", res.Status)
		}
	})

	t.Run("undeclared_placement_unaffected", func(t *testing.T) {
		cat := coldStartCatalog(false)
		cat.Placements[0].Value.ScaleToZero = nil
		facts := map[string]Facts{"kserve-like": {HardEligible: true, Readiness: ReadinessUnknown}}
		res := Decide(cat, facts, in)
		if res.Status != "placed" {
			t.Fatalf("status = %q, want placed: no scale_to_zero declaration means the rule does not apply", res.Status)
		}
	})
}

// largeRequestCatalog: two placements, both eligible, prefer_order puts the
// cold cluster-like candidate FIRST so the ordinary policy ranking and the
// large-request conservative preference provably disagree.
func largeRequestCatalog() *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion: "9.9.9",
		Placements: []catalog.Entry[catalog.Placement]{
			{Name: "cold-first", Value: catalog.Placement{Status: "available", Selectable: true, Kind: "edge"}},
			{Name: "warm-second", Value: catalog.Placement{Status: "available", Selectable: true, Kind: "edge"}},
		},
		Models: map[string]catalog.Model{
			"m": {Hosting: "local", Placements: []string{"cold-first", "warm-second"}},
		},
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "p", Value: catalog.Profile{
				Selectable: true,
				Physical: []catalog.ProfilePhysical{
					{ModelID: "m", Placement: sptr("cold-first")},
					{ModelID: "m", Placement: sptr("warm-second")},
				},
			}},
		},
		Policies: []catalog.Entry[catalog.Policy]{
			{Name: "pol", Value: catalog.Policy{
				PreferOrder: []string{"cold-first", "warm-second"}, WarmPreference: "ignored",
				AllowColdStart: true, ResolvesToday: true,
			}},
		},
	}
}

// estimated_request_bytes > 64 KiB means Agentgateway cannot replay the
// request to a fallback, so /v1/place prefers any known-warm eligible
// candidate over every non-warm one - within the policy's unchanged
// candidate universe, and only when a known-warm candidate exists.
func TestDecide_large_request_prefers_warm(t *testing.T) {
	zero := 0.0
	facts := map[string]Facts{
		"cold-first":  {HardEligible: true, Readiness: ReadinessAbsent},
		"warm-second": {HardEligible: true, Readiness: ReadinessWarm, EstimatedColdStartS: &zero},
	}
	base := Input{ModelProfile: "p", PlacementPolicy: "pol"}

	t.Run("at_65536_ordinary_ranking", func(t *testing.T) {
		in := base
		limit := 65536
		in.EstimatedRequestBytes = &limit
		res := Decide(largeRequestCatalog(), facts, in)
		if res.Placement == nil || *res.Placement != "cold-first" {
			t.Fatalf("placement = %v, want cold-first: 65536 is AT the replay buffer, not above it", res.Placement)
		}
	})
	t.Run("nil_ordinary_ranking", func(t *testing.T) {
		res := Decide(largeRequestCatalog(), facts, base)
		if res.Placement == nil || *res.Placement != "cold-first" {
			t.Fatalf("placement = %v, want cold-first with no estimate given", res.Placement)
		}
	})
	t.Run("at_65537_warm_wins", func(t *testing.T) {
		in := base
		over := 65537
		in.EstimatedRequestBytes = &over
		res := Decide(largeRequestCatalog(), facts, in)
		if res.Placement == nil || *res.Placement != "warm-second" {
			t.Fatalf("placement = %v, want warm-second: above the replay buffer the warm candidate wins", res.Placement)
		}
		if res.ReasonCode != "placed_warm" {
			t.Fatalf("reason = %q, want placed_warm (frozen vocabulary, no new code)", res.ReasonCode)
		}
	})
	t.Run("no_warm_candidate_ordinary_ranking", func(t *testing.T) {
		in := base
		over := 65537
		in.EstimatedRequestBytes = &over
		coldFacts := map[string]Facts{
			"cold-first":  {HardEligible: true, Readiness: ReadinessAbsent},
			"warm-second": {HardEligible: true, Readiness: ReadinessCached},
		}
		res := Decide(largeRequestCatalog(), coldFacts, in)
		if res.Placement == nil || *res.Placement != "cold-first" {
			t.Fatalf("placement = %v, want cold-first: cached is not warm, ordinary ranking stands", res.Placement)
		}
	})
	t.Run("policy_boundary_absolute", func(t *testing.T) {
		// The policy only ranks cold-first; a warm placement OUTSIDE its
		// prefer_order must not be considered no matter the request size.
		cat := largeRequestCatalog()
		cat.Policies[0].Value.PreferOrder = []string{"cold-first"}
		in := base
		over := 1 << 20
		in.EstimatedRequestBytes = &over
		res := Decide(cat, facts, in)
		if res.Placement == nil || *res.Placement != "cold-first" {
			t.Fatalf("placement = %v, want cold-first: the conservative preference never widens the policy's candidate universe", res.Placement)
		}
	})
	t.Run("deterministic", func(t *testing.T) {
		in := base
		over := 65537
		in.EstimatedRequestBytes = &over
		first := Decide(largeRequestCatalog(), facts, in)
		for i := 0; i < 20; i++ {
			again := Decide(largeRequestCatalog(), facts, in)
			if *again.Placement != *first.Placement || again.ReasonCode != first.ReasonCode {
				t.Fatalf("run %d: %v/%s != first %v/%s", i, again.Placement, again.ReasonCode, first.Placement, first.ReasonCode)
			}
		}
	})
}

// A negative decoded warm_bonus_rank_shift must never move a warm candidate
// backward - it is normalized to no promotion.
func TestReorderWeighted_negative_shift_is_no_promotion(t *testing.T) {
	pol := catalog.Policy{WarmPreference: "weighted", WarmBonusRankShift: iptr(-3)}
	got := reorderByWarmPreference(cands(map[string]string{"A": ReadinessWarm, "B": ReadinessAbsent, "C": ReadinessAbsent}, []string{"A", "B", "C"}), pol)
	assertOrder(t, got, "A", "B", "C")
}

// A prefer_order entry the loaded catalog does not declare is skipped
// entirely during candidate construction: no zero-value facts, no
// alternative with an empty reason code, no invalid PlaceResult.
func TestDecide_undeclared_placement_skipped(t *testing.T) {
	cat := largeRequestCatalog()
	cat.Policies[0].Value.PreferOrder = []string{"ghost", "warm-second"}
	cat.Profiles[0].Value.Physical = append(cat.Profiles[0].Value.Physical,
		catalog.ProfilePhysical{ModelID: "m", Placement: sptr("ghost")})
	zero := 0.0
	facts := map[string]Facts{
		"warm-second": {HardEligible: true, Readiness: ReadinessWarm, EstimatedColdStartS: &zero},
	}
	res := Decide(cat, facts, Input{ModelProfile: "p", PlacementPolicy: "pol"})
	if res.Placement == nil || *res.Placement != "warm-second" {
		t.Fatalf("placement = %v, want warm-second: the undeclared placement is not a candidate", res.Placement)
	}
	for _, a := range res.Alternatives {
		if a.Placement == "ghost" {
			t.Fatalf("alternatives contain the undeclared placement: %+v", res.Alternatives)
		}
		if !a.Eligible && a.ReasonCode == "" {
			t.Fatalf("alternative with empty reason code: %+v", a)
		}
	}
}
