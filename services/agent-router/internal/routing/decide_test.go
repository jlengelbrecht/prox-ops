package routing_test

import (
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
)

func strp(s string) *string { return &s }
func intp(i int) *int       { return &i }

// minimalCatalog builds a Go-literal catalog with exactly the profiles the
// real catalog declares that routing.ApprovedPairs references, so pure
// Decide tests do not depend on loading or parsing YAML.
func minimalCatalog() *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Harnesses: []catalog.Entry[catalog.Harness]{
			{Name: "claude", Value: catalog.Harness{Supported: true, Selectable: true}},
			{Name: "codex", Value: catalog.Harness{Supported: true, Selectable: true}},
			{Name: "devin", Value: catalog.Harness{Supported: true, Selectable: true}},
			{Name: "local-agent", Value: catalog.Harness{Supported: false, Selectable: false, RouterBehaviour: "refuse-to-emit"}},
		},
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "local-code-standard", Value: catalog.Profile{
				CostClass: "free", Hosting: "local", Selectable: true,
				Capabilities: []string{"chat", "tools"}, MinContext: intp(65536),
				Entitlements: []catalog.ProfileEntitlement{{Pool: nil, CostClass: "free"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "qwen36-27b", Placement: nil}},
			}},
			{Name: "local-general", Value: catalog.Profile{
				CostClass: "free", Hosting: "local", Selectable: true,
				Capabilities: []string{"chat", "tools"}, MinContext: intp(65536),
				Entitlements: []catalog.ProfileEntitlement{{Pool: nil, CostClass: "free"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "qwen36-27b", Placement: nil}},
			}},
			{Name: "claude/strong", Value: catalog.Profile{
				CostClass: "subscription", Hosting: "vendor", Selectable: true,
				Capabilities: []string{"chat", "tools"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: strp("anthropic-max"), CostClass: "subscription"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "claude/opus", Placement: nil}},
			}},
			{Name: "openai/strong", Value: catalog.Profile{
				CostClass: "subscription", Hosting: "vendor", Selectable: true,
				Capabilities: []string{"chat", "tools"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: strp("openai-plus"), CostClass: "subscription"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "openai/gpt-5.6-sol", Placement: nil}},
			}},
			{Name: "devin/free", Value: catalog.Profile{
				CostClass: "free", Hosting: "vendor", Selectable: true,
				Capabilities: []string{"chat", "tools"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: strp("devin-free"), CostClass: "free"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "swe-1-7", Placement: nil}},
			}},
			{Name: "minimax/strong", Value: catalog.Profile{
				CostClass: "subscription", Hosting: "vendor", Selectable: false,
				Capabilities: []string{"chat"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: strp("minimax-max"), CostClass: "subscription"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "minimax/m3", Placement: nil}},
			}},
		},
		EntitlementPools: []catalog.Entry[catalog.EntitlementPool]{
			{Name: "anthropic-max", Value: catalog.EntitlementPool{Provider: "anthropic", CostClass: "subscription", CredentialClass: "subscription-session", Spillover: "none"}},
			{Name: "openai-plus", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "subscription", CredentialClass: "subscription-session", Spillover: "none"}},
			{Name: "devin-free", Value: catalog.EntitlementPool{Provider: "cognition", CostClass: "free", CredentialClass: "account-session", Spillover: "none"}},
			{Name: "minimax-max", Value: catalog.EntitlementPool{Provider: "minimax", CostClass: "subscription", CredentialClass: "provider-api-key", Spillover: "none"}},
		},
	}
}

func baseInput() routing.Input {
	return routing.Input{Ambiguity: "low", BlastRadius: "low", PlacementPolicy: "prefer-warm-local"}
}

func TestDecide_StandardBand(t *testing.T) {
	cat := minimalCatalog()
	res := routing.Decide(cat, baseInput(), false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK", res.Outcome)
	}
	d := res.Decision
	if d.Harness != "claude" || d.ModelProfile != "local-code-standard" {
		t.Fatalf("primary = %s/%s, want claude/local-code-standard", d.Harness, d.ModelProfile)
	}
	if len(d.Fallbacks) != 2 || d.Fallbacks[0].Pair.ModelProfile != "openai/strong" || d.Fallbacks[1].Pair.ModelProfile != "devin/free" {
		t.Fatalf("fallbacks = %+v, want [openai/strong, devin/free]", d.Fallbacks)
	}
}

func TestDecide_StrongBandExcludesStandardOnlyPairs(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	in.Ambiguity = "high"
	in.BlastRadius = "critical"
	res := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK", res.Outcome)
	}
	d := res.Decision
	if d.Harness != "claude" || d.ModelProfile != "claude/strong" {
		t.Fatalf("primary = %s/%s, want claude/claude/strong", d.Harness, d.ModelProfile)
	}
	for _, f := range d.Fallbacks {
		if f.Pair.ModelProfile == "devin/free" || f.Pair.ModelProfile == "local-code-standard" {
			t.Errorf("standard-only pair %q leaked into a strong-band fallback list", f.Pair.ModelProfile)
		}
	}
}

func TestDecide_ForbiddenForExcludesEvenAnApprovedPair(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	// local-code-standard has no forbidden_for in minimalCatalog; add it here
	// to prove the filter fires regardless of the profile it is attached to.
	for i, e := range cat.Profiles {
		if e.Name == "local-code-standard" {
			e.Value.ForbiddenFor = []string{"security"}
			cat.Profiles[i] = e
		}
	}
	in.Tags = []string{"security"}
	res := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK (devin/free should remain)", res.Outcome)
	}
	if res.Decision.ModelProfile == "local-code-standard" {
		t.Fatal("local-code-standard was emitted despite forbidden_for matching a request tag")
	}
}

func TestDecide_ContextRequirementExcludesUnguaranteedProfiles(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	in.ContextSize = intp(65537) // one over local-code-standard's 65536 guarantee
	res := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeNoEligibleProfile {
		t.Fatalf("outcome = %v, want OutcomeNoEligibleProfile: no candidate guarantees this floor", res.Outcome)
	}
}

func TestDecide_NoContextRequirementAdmitsNullGuaranteeProfiles(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	in.ContextSize = nil
	res := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK", res.Outcome)
	}
}

func TestDecide_MeteredDefaultDenySubstitutesNonMetered(t *testing.T) {
	cat := minimalCatalog()
	for i, e := range cat.Profiles {
		if e.Name == "openai/strong" {
			e.Value.Entitlements = append(e.Value.Entitlements, catalog.ProfileEntitlement{Pool: strp("openai-payg"), CostClass: "metered"})
			cat.Profiles[i] = e
		}
	}
	cat.EntitlementPools = append(cat.EntitlementPools, catalog.Entry[catalog.EntitlementPool]{
		Name: "openai-payg", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "metered", CredentialClass: "provider-api-key", Spillover: "none"},
	})
	// Exclude local-code-standard from this scenario by tagging it forbidden,
	// so openai/strong (now resolved metered, since openai-plus is
	// exhausted) ranks ahead of devin/free and becomes the "best" candidate
	// the default-deny ladder must intercept.
	for i, e := range cat.Profiles {
		if e.Name == "local-code-standard" {
			e.Value.ForbiddenFor = []string{"whatever"}
			cat.Profiles[i] = e
		}
	}
	in := baseInput()
	in.Tags = []string{"whatever"}
	avail := excludeSet{"openai-plus": true}

	res := routing.Decide(cat, in, false, avail)
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK (devin/free substitute)", res.Outcome)
	}
	if res.Decision.ModelProfile != "devin/free" {
		t.Fatalf("primary = %s, want devin/free", res.Decision.ModelProfile)
	}
	if res.Decision.MeteredDenied == nil || res.Decision.MeteredDenied.ModelProfile != "openai/strong" {
		t.Fatalf("MeteredDenied = %+v, want withheld openai/strong", res.Decision.MeteredDenied)
	}
}

func TestDecide_MeteredAuthorizedSelectsDeclaredMeteredCandidate(t *testing.T) {
	cat := minimalCatalog()
	for i, e := range cat.Profiles {
		if e.Name == "openai/strong" {
			e.Value.Entitlements = append(e.Value.Entitlements, catalog.ProfileEntitlement{Pool: strp("openai-payg"), CostClass: "metered"})
			cat.Profiles[i] = e
		}
	}
	cat.EntitlementPools = append(cat.EntitlementPools, catalog.Entry[catalog.EntitlementPool]{
		Name: "openai-payg", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "metered", CredentialClass: "provider-api-key", Spillover: "none"},
	})
	for i, e := range cat.Profiles {
		if e.Name == "local-code-standard" {
			e.Value.ForbiddenFor = []string{"whatever"}
			cat.Profiles[i] = e
		}
	}
	in := baseInput()
	in.Tags = []string{"whatever"}
	avail := excludeSet{"openai-plus": true}

	res := routing.Decide(cat, in, true, avail) // authorized
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK", res.Outcome)
	}
	if res.Decision.ModelProfile != "openai/strong" || res.Decision.CostClass != "metered" || !res.Decision.Metered {
		t.Fatalf("decision = %+v, want openai/strong metered=true", res.Decision)
	}
	if res.Decision.MeteredDenied != nil {
		t.Error("MeteredDenied set on an authorized, successfully metered decision")
	}
}

func TestDecide_MeteredNoAlternative409(t *testing.T) {
	cat := minimalCatalog()
	for i, e := range cat.Profiles {
		if e.Name == "openai/strong" {
			e.Value.Entitlements = append(e.Value.Entitlements, catalog.ProfileEntitlement{Pool: strp("openai-payg"), CostClass: "metered"})
			cat.Profiles[i] = e
		}
	}
	cat.EntitlementPools = append(cat.EntitlementPools, catalog.Entry[catalog.EntitlementPool]{
		Name: "openai-payg", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "metered", CredentialClass: "provider-api-key", Spillover: "none"},
	})
	// Strong band with claude/strong removed (not selectable here) leaves
	// openai/strong (now metered) as the sole candidate.
	for i, e := range cat.Profiles {
		if e.Name == "claude/strong" {
			e.Value.Selectable = false
			cat.Profiles[i] = e
		}
	}
	in := baseInput()
	in.Ambiguity = "high"
	avail := excludeSet{"openai-plus": true}

	res := routing.Decide(cat, in, false, avail)
	if res.Outcome != routing.OutcomeMeteredDenied {
		t.Fatalf("outcome = %v, want OutcomeMeteredDenied", res.Outcome)
	}
	if res.Withheld == nil || res.Withheld.ModelProfile != "openai/strong" {
		t.Fatalf("withheld = %+v, want openai/strong", res.Withheld)
	}
}

func TestDecide_NonSelectableNeverEmitted(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	in.Ambiguity = "high"
	in.BlastRadius = "critical"
	res := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if res.Outcome != routing.OutcomeOK {
		t.Fatalf("outcome = %v, want OK", res.Outcome)
	}
	if res.Decision.ModelProfile == "minimax/strong" {
		t.Fatal("minimax/strong (selectable: false) was emitted")
	}
	for _, f := range res.Decision.Fallbacks {
		if f.Pair.ModelProfile == "minimax/strong" {
			t.Fatal("minimax/strong (selectable: false) leaked into fallbacks")
		}
	}
}

func TestDecide_LocalAgentNeverEmitted(t *testing.T) {
	for _, p := range routing.ApprovedPairs {
		if p.Harness == "local-agent" {
			t.Fatalf("routing.ApprovedPairs contains a local-agent pair: %+v", p)
		}
	}
}

func TestDecide_Deterministic(t *testing.T) {
	cat := minimalCatalog()
	in := baseInput()
	r1 := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	r2 := routing.Decide(cat, in, false, routing.AlwaysAvailable{})
	if r1.Decision.Harness != r2.Decision.Harness || r1.Decision.ModelProfile != r2.Decision.ModelProfile {
		t.Fatalf("Decide is not deterministic: %+v vs %+v", r1.Decision, r2.Decision)
	}
	if len(r1.Decision.Fallbacks) != len(r2.Decision.Fallbacks) {
		t.Fatalf("fallback count differs across identical calls: %d vs %d", len(r1.Decision.Fallbacks), len(r2.Decision.Fallbacks))
	}
	for i := range r1.Decision.Fallbacks {
		if r1.Decision.Fallbacks[i].Pair.ModelProfile != r2.Decision.Fallbacks[i].Pair.ModelProfile {
			t.Fatalf("fallback order differs across identical calls at index %d", i)
		}
	}
}

// excludeSet is a routing.EntitlementAvailability test seam: every pool is
// available unless explicitly listed.
type excludeSet map[string]bool

func (e excludeSet) Available(pool string) bool { return !e[pool] }
