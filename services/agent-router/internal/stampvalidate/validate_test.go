package stampvalidate_test

import (
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/stampvalidate"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// realDigest is the committed catalog 1.3.0's real content digest
// (contracts/agent-router/README.md's digest table; also
// catalog_test.go's wantDigest).
const realDigest = "sha256:2fd681e0e988bf6be94b8923b1485a0aa174a4e66aec580a2d383c764b60e229"

func strp(s string) *string { return &s }

// realCatalog loads the committed production catalog - the same document
// the deployed router serves - so every case below that does not need a
// synthetic shape is proven against reality, not a hand-copied restatement
// of it.
func realCatalog(t *testing.T) *catalog.Catalog {
	t.Helper()
	cat, digest, err := catalog.Load(testutil.ExtractCatalogYAML(t))
	if err != nil {
		t.Fatalf("loading real catalog: %v", err)
	}
	if digest != realDigest {
		t.Fatalf("digest = %q, want %q (contracts/agent-router/README.md digest table drifted)", digest, realDigest)
	}
	return cat
}

func fixedNow(t *testing.T) time.Time {
	t.Helper()
	now, err := time.Parse(time.RFC3339, "2026-08-20T12:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	return now
}

// vendorStamp is an unmodified claude/strong router recommendation, stamped
// exactly as /v1/route would emit it (security-tagged-excludes-unrestricted.json
// style): vendor-hosted, no placement, no override.
func vendorStamp(now time.Time) stampvalidate.Stamp {
	return stampvalidate.Stamp{
		Harness:           "claude",
		ModelProfile:      "claude/strong",
		Effort:            "xhigh",
		CostClass:         "subscription",
		EntitlementPool:   strp("anthropic-max"),
		Metered:           false,
		PlacementPolicy:   "prefer-warm-local",
		PlacementRequired: false,
		CatalogVersion:    realDigest,
		ExpiresAt:         now.Add(24 * time.Hour),
		Task:              stampvalidate.TaskIdentity{StoryID: "STORY-035-12", Title: "freeze the validator", Tags: []string{"security", "iam"}},
	}
}

// localStamp is an unmodified local-code-standard router recommendation
// (local-code-standard.json style): local-hosted, placement required.
func localStamp(now time.Time) stampvalidate.Stamp {
	return stampvalidate.Stamp{
		Harness:           "claude",
		ModelProfile:      "local-code-standard",
		Effort:            "medium",
		CostClass:         "free",
		EntitlementPool:   nil,
		Metered:           false,
		PlacementPolicy:   "prefer-warm-local",
		PlacementRequired: true,
		CatalogVersion:    realDigest,
		ExpiresAt:         now.Add(24 * time.Hour),
		Task:              stampvalidate.TaskIdentity{StoryID: "STORY-035-12", Title: "a contained refactor", Tags: nil},
	}
}

// freshEvidence is a matching, unexpired /v1/place result for localStamp:
// placed on cachyos-7900xtx (first in prefer-warm-local's prefer_order, and
// one of local-code-standard's declared physical candidates), resolved
// "just now" relative to resolvedAt.
func freshEvidence(resolvedAt time.Time) *stampvalidate.PlacementEvidence {
	return &stampvalidate.PlacementEvidence{
		ResolvedAt: resolvedAt,
		Result: stampvalidate.PlaceResult{
			Status:         "placed",
			Model:          strp("qwen36-27b"),
			Placement:      strp("cachyos-7900xtx"),
			Readiness:      strp("warm"),
			Headers:        map[string]string{"x-placement": "cachyos-7900xtx"},
			TTLSeconds:     30,
			Reason:         stampvalidate.PlaceReason{Code: "placed_warm", Message: "warm edge candidate"},
			CatalogVersion: realDigest,
		},
	}
}

func mustCheck(t *testing.T, v stampvalidate.Verdict, name string) stampvalidate.CheckResult {
	t.Helper()
	for _, c := range v.Checks {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no check named %q in verdict %+v", name, v)
	return stampvalidate.CheckResult{}
}

// Test_valid_router_stamp_passes proves an unmodified router recommendation,
// stamped, validates clean end to end.
func Test_valid_router_stamp_passes(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	v := stampvalidate.ValidateFinal(cat, vendorStamp(now), stampvalidate.ValidationContext{}, now)
	if !v.Valid {
		t.Fatalf("verdict = %+v, want Valid=true", v)
	}
	if len(v.Checks) != 8 {
		t.Fatalf("len(Checks) = %d, want 8 (every rule evaluated and listed)", len(v.Checks))
	}
	if v.CatalogVersion != realDigest {
		t.Errorf("CatalogVersion = %q, want %q", v.CatalogVersion, realDigest)
	}
}

// Test_valid_override_passes proves a human override that still satisfies
// every hard rule validates - overriding from local-code-standard to
// claude/strong for a task that is not tagged in any way claude/strong's
// (empty) forbidden_for would exclude.
func Test_valid_override_passes(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	stamp := vendorStamp(now)
	stamp.Task.Tags = nil // no forbidden tags in play
	stamp.Override = &stampvalidate.Override{
		Actor: "josh", At: now.Add(-time.Minute),
		Original: stampvalidate.OverrideOriginal{Harness: "claude", ModelProfile: "local-code-standard"},
		Reason:   "needs judgement the local profile cannot provide",
	}
	v := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if !v.Valid {
		t.Fatalf("verdict = %+v, want Valid=true", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckOverrideWithin); c.ReasonCode != stampvalidate.ReasonOverrideRecorded {
		t.Errorf("override_within_policy reason = %q, want %q", c.ReasonCode, stampvalidate.ReasonOverrideRecorded)
	}
}

// Test_forbidden_override_fails proves an override to a pairing forbidden
// for the stamped task tags fails, even though the pair itself is approved.
// SYNTHETIC: the real catalog 1.3.0 has no approved-pair profile with a
// non-empty forbidden_for, so this mutates a loaded copy to give
// local-code-standard one, the same technique routing/decide_test.go uses
// (TestDecide_ForbiddenForExcludesEvenAnApprovedPair).
func Test_forbidden_override_fails(t *testing.T) {
	cat := realCatalog(t)
	for i, e := range cat.Profiles {
		if e.Name == "local-code-standard" {
			e.Value.ForbiddenFor = []string{"security"}
			cat.Profiles[i] = e
		}
	}
	now := fixedNow(t)
	stamp := localStamp(now)
	stamp.Task.Tags = []string{"security"}
	stamp.Override = &stampvalidate.Override{
		Actor: "josh", At: now.Add(-time.Minute),
		Original: stampvalidate.OverrideOriginal{Harness: "claude", ModelProfile: "local-general"},
		Reason:   "operator chose deliberately",
	}
	v := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{PlacementEvidence: freshEvidence(now.Add(-time.Second))}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false: forbidden_for must not be bypassed by an override", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckForbiddenFor); c.Passed || c.ReasonCode != stampvalidate.ReasonForbiddenForTag {
		t.Errorf("forbidden_for = %+v, want failed with %q", c, stampvalidate.ReasonForbiddenForTag)
	}
}

// Test_invalid_pairing_fails proves a pair outside routing.ApprovedPairs
// fails, regardless of whether either half individually names a real
// catalog entry.
func Test_invalid_pairing_fails(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	stamp := localStamp(now)
	stamp.Harness = "codex" // codex+local-code-standard is not an approved pair
	v := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckApprovedPair); c.Passed || c.ReasonCode != stampvalidate.ReasonPairNotApproved {
		t.Errorf("approved_pair = %+v, want failed with %q", c, stampvalidate.ReasonPairNotApproved)
	}
}

// Test_pair_authority_not_duplicated proves the approved_pair check IS
// routing.ApprovedPairs itself, not a copy: mutating the shared package
// variable changes the verdict for an identical stamp, with no code change
// in stampvalidate.
func Test_pair_authority_not_duplicated(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	stamp := localStamp(now)
	stamp.Harness = "devin"
	stamp.ModelProfile = "local-code-standard" // devin+local-code-standard: not approved today

	before := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if c := mustCheck(t, before, stampvalidate.CheckApprovedPair); c.Passed {
		t.Fatalf("approved_pair passed before mutation; test premise is wrong: %+v", c)
	}

	original := routing.ApprovedPairs
	t.Cleanup(func() { routing.ApprovedPairs = original })
	mutated := append([]routing.Pair(nil), original...)
	mutated = append(mutated, routing.Pair{Harness: "devin", ModelProfile: "local-code-standard", Bands: []routing.Band{routing.BandStandard}, Priority: 99})
	routing.ApprovedPairs = mutated

	after := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if c := mustCheck(t, after, stampvalidate.CheckApprovedPair); !c.Passed {
		t.Fatalf("approved_pair still failed after adding the pair to routing.ApprovedPairs directly - stampvalidate must be reading the live table: %+v", c)
	}
}

// Test_expires_at_not_reset proves a stamp minted later (a later created_at)
// gains no fresh validity window: expires_at alone governs, and now past
// expires_at fails regardless of how recently the stamp claims to have been
// created.
func Test_expires_at_not_reset(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	stamp := vendorStamp(now)
	stamp.ExpiresAt = now.Add(-time.Minute) // already expired relative to now
	justMinted := now.Add(-time.Second)     // "created" one second ago
	stamp.CreatedAt = &justMinted

	v := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false: a fresh created_at must not extend expires_at", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckNotExpired); c.Passed || c.ReasonCode != stampvalidate.ReasonStampExpired {
		t.Errorf("not_expired = %+v, want failed with %q", c, stampvalidate.ReasonStampExpired)
	}
}

// Test_unauthorized_metered_fails proves the dual-key rule in BOTH
// directions: intent without authority fails, and authority without intent
// also fails.
func Test_unauthorized_metered_fails(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)

	intentOnly := vendorStamp(now)
	intentOnly.Metered = true
	v1 := stampvalidate.ValidateFinal(cat, intentOnly, stampvalidate.ValidationContext{MeteredSpendAuthorized: false}, now)
	if v1.Valid {
		t.Fatalf("intent without authority: verdict = %+v, want Valid=false", v1)
	}
	if c := mustCheck(t, v1, stampvalidate.CheckMeteredDualKey); c.Passed || c.ReasonCode != stampvalidate.ReasonMeteredAuthorityMissing {
		t.Errorf("metered_dual_key = %+v, want failed with %q", c, stampvalidate.ReasonMeteredAuthorityMissing)
	}

	authorityOnly := vendorStamp(now)
	authorityOnly.Metered = false
	v2 := stampvalidate.ValidateFinal(cat, authorityOnly, stampvalidate.ValidationContext{MeteredSpendAuthorized: true}, now)
	if v2.Valid {
		t.Fatalf("authority without intent: verdict = %+v, want Valid=false", v2)
	}
	if c := mustCheck(t, v2, stampvalidate.CheckMeteredDualKey); c.Passed || c.ReasonCode != stampvalidate.ReasonMeteredIntentMissing {
		t.Errorf("metered_dual_key = %+v, want failed with %q", c, stampvalidate.ReasonMeteredIntentMissing)
	}
}

// Test_stale_catalog_fails_closed proves a digest mismatch fails closed even
// though the stamped pair exists in the (real, loaded) catalog too - drift
// is judged on the digest alone, never re-argued from whether the pair still
// happens to resolve.
func Test_stale_catalog_fails_closed(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	stamp := vendorStamp(now)
	stamp.CatalogVersion = "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

	v := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckCatalogDrift); c.Passed || c.ReasonCode != stampvalidate.ReasonCatalogVersionStale {
		t.Errorf("catalog_drift_fails_closed = %+v, want failed with %q", c, stampvalidate.ReasonCatalogVersionStale)
	}
	// The pair itself is still approved and present in the loaded catalog -
	// proving the failure is judged on the digest, not smuggled in via a
	// pairing that would also have failed on its own.
	if c := mustCheck(t, v, stampvalidate.CheckApprovedPair); !c.Passed {
		t.Errorf("approved_pair = %+v, want passed (drift must be the sole reason this stamp is invalid)", c)
	}
}

// Test_local_placement_missing_fails proves placement_required: true with no
// PlacementEvidence fails.
func Test_local_placement_missing_fails(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	v := stampvalidate.ValidateFinal(cat, localStamp(now), stampvalidate.ValidationContext{}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckPlacement); c.Passed || c.ReasonCode != stampvalidate.ReasonPlacementEvidenceMissing {
		t.Errorf("placement = %+v, want failed with %q", c, stampvalidate.ReasonPlacementEvidenceMissing)
	}
}

// Test_local_placement_fresh_passes proves a valid local stamp plus fresh,
// matching, authorized, same-catalog evidence within its TTL passes.
func Test_local_placement_fresh_passes(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	evidence := freshEvidence(now.Add(-5 * time.Second)) // resolved 5s ago, ttl 30s
	v := stampvalidate.ValidateFinal(cat, localStamp(now), stampvalidate.ValidationContext{PlacementEvidence: evidence}, now)
	if !v.Valid {
		t.Fatalf("verdict = %+v, want Valid=true", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckPlacement); c.ReasonCode != stampvalidate.ReasonPlacedAndFresh {
		t.Errorf("placement reason = %q, want %q", c.ReasonCode, stampvalidate.ReasonPlacedAndFresh)
	}
}

// Test_placement_evidence_expired_fails proves IDENTICAL evidence fails once
// now reaches resolved_at + ttl_seconds EXACTLY - the frozen boundary is
// strict (<), so equality is invalid, not a grace instant. Both the exact
// boundary and one instant past it are checked; one instant before must
// still pass, isolating the boundary precisely.
func Test_placement_evidence_expired_fails(t *testing.T) {
	cat := realCatalog(t)
	resolvedAt := fixedNow(t)
	evidence := freshEvidence(resolvedAt)
	expiry := resolvedAt.Add(30 * time.Second)

	stillValid := stampvalidate.ValidateFinal(cat, localStamp(resolvedAt), stampvalidate.ValidationContext{PlacementEvidence: evidence}, expiry.Add(-time.Nanosecond))
	if !stillValid.Valid {
		t.Fatalf("one nanosecond before expiry: verdict = %+v, want Valid=true", stillValid)
	}

	atExactly := stampvalidate.ValidateFinal(cat, localStamp(resolvedAt), stampvalidate.ValidationContext{PlacementEvidence: evidence}, expiry)
	if atExactly.Valid {
		t.Fatalf("at exact expiry: verdict = %+v, want Valid=false (equality at expiry is invalid by design)", atExactly)
	}
	if c := mustCheck(t, atExactly, stampvalidate.CheckPlacement); c.Passed || c.ReasonCode != stampvalidate.ReasonPlacementEvidenceExpired {
		t.Errorf("placement at exact expiry = %+v, want failed with %q", c, stampvalidate.ReasonPlacementEvidenceExpired)
	}

	afterExpiry := stampvalidate.ValidateFinal(cat, localStamp(resolvedAt), stampvalidate.ValidationContext{PlacementEvidence: evidence}, expiry.Add(time.Nanosecond))
	if afterExpiry.Valid {
		t.Fatalf("one nanosecond after expiry: verdict = %+v, want Valid=false", afterExpiry)
	}
}

// Test_reuse_stamp_fresh_evidence_passes proves that replacing ONLY expired
// evidence with a fresh valid /place result lets the SAME unexpired stamp
// pass - the recovery path amendment 6 names for a placement-only failure,
// with no new stamp minted.
func Test_reuse_stamp_fresh_evidence_passes(t *testing.T) {
	cat := realCatalog(t)
	stampCreatedAt := fixedNow(t)
	stamp := localStamp(stampCreatedAt) // expires_at = stampCreatedAt + 24h, unchanged below

	firstAttemptNow := stampCreatedAt.Add(time.Minute)
	expiredEvidence := freshEvidence(firstAttemptNow.Add(-time.Hour)) // resolved long ago, ttl 30s: expired
	first := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{PlacementEvidence: expiredEvidence}, firstAttemptNow)
	if first.Valid {
		t.Fatalf("setup: expired evidence unexpectedly passed: %+v", first)
	}
	if c := mustCheck(t, first, stampvalidate.CheckPlacement); c.ReasonCode != stampvalidate.ReasonPlacementEvidenceExpired {
		t.Fatalf("setup: expected placement_evidence_expired, got %+v", c)
	}

	secondAttemptNow := firstAttemptNow.Add(time.Second)
	freshOnly := freshEvidence(secondAttemptNow.Add(-time.Second)) // freshly re-placed
	second := stampvalidate.ValidateFinal(cat, stamp, stampvalidate.ValidationContext{PlacementEvidence: freshOnly}, secondAttemptNow)
	if !second.Valid {
		t.Fatalf("verdict = %+v, want Valid=true: the same stamp with fresh evidence must pass", second)
	}
	if stamp.ExpiresAt != localStamp(stampCreatedAt).ExpiresAt {
		t.Fatal("test bug: the stamp must be literally unchanged between attempts")
	}
}

// Test_vendor_fabricated_placement_fails proves a vendor stamp
// (placement_required: false) fails if ANY placement evidence accompanies
// it, regardless of how well-formed that evidence is.
func Test_vendor_fabricated_placement_fails(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)
	evidence := freshEvidence(now.Add(-time.Second))
	v := stampvalidate.ValidateFinal(cat, vendorStamp(now), stampvalidate.ValidationContext{PlacementEvidence: evidence}, now)
	if v.Valid {
		t.Fatalf("verdict = %+v, want Valid=false", v)
	}
	if c := mustCheck(t, v, stampvalidate.CheckPlacement); c.Passed || c.ReasonCode != stampvalidate.ReasonVendorProfileWithPlacement {
		t.Errorf("placement = %+v, want failed with %q", c, stampvalidate.ReasonVendorProfileWithPlacement)
	}
}

// Test_determinism proves ValidateFinal is a pure function: repeated,
// concurrent evaluation of the same fixtures under -race yields
// byte-identical Verdicts every time, with no shared mutable state.
func Test_determinism(t *testing.T) {
	cat := realCatalog(t)
	now := fixedNow(t)

	type fixture struct {
		name  string
		stamp stampvalidate.Stamp
		vctx  stampvalidate.ValidationContext
	}
	fixtures := []fixture{
		{"vendor_valid", vendorStamp(now), stampvalidate.ValidationContext{}},
		{"local_valid", localStamp(now), stampvalidate.ValidationContext{PlacementEvidence: freshEvidence(now.Add(-5 * time.Second))}},
		{"local_missing_evidence", localStamp(now), stampvalidate.ValidationContext{}},
		{"metered_unauthorized", func() stampvalidate.Stamp { s := vendorStamp(now); s.Metered = true; return s }(), stampvalidate.ValidationContext{}},
	}

	want := make([]stampvalidate.Verdict, len(fixtures))
	for i, f := range fixtures {
		want[i] = stampvalidate.ValidateFinal(cat, f.stamp, f.vctx, now)
	}

	const iterations = 50
	const workers = 20
	var wg sync.WaitGroup
	errs := make(chan string, workers*iterations*len(fixtures))
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < iterations; i++ {
				for fi, f := range fixtures {
					got := stampvalidate.ValidateFinal(cat, f.stamp, f.vctx, now)
					if !reflect.DeepEqual(got, want[fi]) {
						errs <- f.name
					}
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	for name := range errs {
		t.Errorf("fixture %q produced a non-identical Verdict under concurrent evaluation", name)
	}
}
