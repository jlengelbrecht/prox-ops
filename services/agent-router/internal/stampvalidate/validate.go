package stampvalidate

import (
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
)

// Frozen check names (Verdict.Checks[].Name), one per STORY-035-12 rule 1-8,
// always present in this order.
const (
	CheckApprovedPair       = "approved_pair"
	CheckForbiddenFor       = "forbidden_for"
	CheckEntitlementAllowed = "entitlement_permitted"
	CheckMeteredDualKey     = "metered_dual_key"
	CheckPlacement          = "placement"
	CheckCatalogDrift       = "catalog_drift_fails_closed"
	CheckNotExpired         = "not_expired"
	CheckOverrideWithin     = "override_within_policy"
)

// Frozen reason-code enumeration (contracts/agent-router/schemas/
// validation-verdict.schema.json). "incl." per the story: this is the
// complete set this build emits, and validation-verdict.schema.json's enum
// must be kept in lockstep with it.
const (
	ReasonOK                          = "ok"
	ReasonPairNotApproved             = "pair_not_approved"
	ReasonForbiddenForTag             = "forbidden_for_tag"
	ReasonProfileUnknownInCatalog     = "profile_unknown_in_catalog"
	ReasonEntitlementNotPermitted     = "entitlement_not_permitted"
	ReasonMeteredAuthorityMissing     = "metered_authority_missing"
	ReasonMeteredIntentMissing        = "metered_intent_missing"
	ReasonNotApplicable               = "not_applicable"
	ReasonVendorProfileWithPlacement  = "vendor_profile_with_placement"
	ReasonPlacementEvidenceMissing    = "placement_evidence_missing"
	ReasonPlacementNotPlaced          = "placement_not_placed"
	ReasonPlacementHeaderMismatch     = "placement_header_mismatch"
	ReasonPlacementNotAuthorized      = "placement_not_authorized"
	ReasonPlacementCatalogVersionDiff = "placement_catalog_version_mismatch"
	ReasonPlacementEvidenceExpired    = "placement_evidence_expired"
	ReasonPlacedAndFresh              = "placed_and_fresh"
	ReasonCatalogVersionStale         = "catalog_version_stale"
	ReasonStampExpired                = "stamp_expired"
	ReasonNoOverride                  = "no_override"
	ReasonOverrideRecorded            = "override_recorded"
)

// ValidateFinal is the final pre-execution policy validator (STORY-035-12).
// It is a PURE function of its four arguments: no I/O, no wall-clock, no
// randomness, no environment reads. now is injected rather than read from
// time.Now() specifically so the same (cat, stamp, vctx, now) always produces
// a byte-identical Verdict (the story's "Determinism and purity", frozen).
//
// Every one of the eight rules below is evaluated unconditionally and
// appears in Verdict.Checks, whether it passed or failed - there is no
// short-circuit and no rule that a failure elsewhere skips. Verdict.Valid is
// the AND of all eight.
func ValidateFinal(cat *catalog.Catalog, stamp Stamp, vctx ValidationContext, now time.Time) Verdict {
	profile, profileOK := findProfile(cat, stamp.ModelProfile)

	checks := make([]CheckResult, 0, 8)

	// 1. approved_pair - routing.ApprovedPairs is the single source of truth
	// for which {harness, model_profile} pairs the router itself may ever
	// emit (amendment 5). A validator that kept its own copy could disagree
	// with /v1/route about what is approved; this reads the same table.
	pairOK := approvedPair(stamp.Harness, stamp.ModelProfile)
	checks = append(checks, mkCheck(CheckApprovedPair, pairOK, ReasonOK, ReasonPairNotApproved,
		"the stamped {harness, model_profile} pair is in routing.ApprovedPairs",
		"the stamped {harness, model_profile} pair does not appear in routing.ApprovedPairs at all"))

	// 2. forbidden_for - profile.ForbiddenFor ∩ stamp.Task.Tags must be
	// empty. A hard exclusion, never a score, and never bypassable by an
	// override (amendment 4/6, ADE-BOUNDARY.md §7).
	var forbiddenOK bool
	var forbiddenReason string
	switch {
	case !profileOK:
		forbiddenOK, forbiddenReason = false, ReasonProfileUnknownInCatalog
	case routing.TagsForbidden(profile.ForbiddenFor, stamp.Task.Tags):
		forbiddenOK, forbiddenReason = false, ReasonForbiddenForTag
	default:
		forbiddenOK, forbiddenReason = true, ReasonOK
	}
	checks = append(checks, CheckResult{
		Name: CheckForbiddenFor, Passed: forbiddenOK, ReasonCode: forbiddenReason,
		Message: forbiddenForMessage(forbiddenOK, forbiddenReason, profile, stamp),
	})

	// 3. entitlement_permitted - the stamped pool/cost_class must still be a
	// declared, permitted funding source for the profile in the LOADED
	// catalog. This checks the catalog's static declaration only; it never
	// consults live availability (routing.EntitlementAvailability is a
	// route-time concept, not a validation-time one - amendment 1's "no live
	// signal for subscription exhaustion" applies here too).
	entOK := profileOK && entitlementPermitted(cat, profile, stamp.EntitlementPool, stamp.CostClass)
	entReason := ReasonOK
	if !entOK {
		if !profileOK {
			entReason = ReasonProfileUnknownInCatalog
		} else {
			entReason = ReasonEntitlementNotPermitted
		}
	}
	checks = append(checks, CheckResult{
		Name: CheckEntitlementAllowed, Passed: entOK, ReasonCode: entReason,
		Message: entitlementMessage(entOK, entReason, stamp),
	})

	// 4. metered_dual_key - metered:true states INTENT only (amendment 3);
	// spend AUTHORITY exists only in ValidationContext, independently of the
	// stamp. Either alone fails: intent without authority, or authority
	// without intent (which would mean the caller is granting spend
	// authority the attempt never asked to use - also wrong, and dual-key
	// tests must prove both directions).
	meteredOK, meteredReason := true, ReasonOK
	switch {
	case stamp.Metered && !vctx.MeteredSpendAuthorized:
		meteredOK, meteredReason = false, ReasonMeteredAuthorityMissing
	case !stamp.Metered && vctx.MeteredSpendAuthorized:
		meteredOK, meteredReason = false, ReasonMeteredIntentMissing
	}
	checks = append(checks, CheckResult{
		Name: CheckMeteredDualKey, Passed: meteredOK, ReasonCode: meteredReason,
		Message: meteredMessage(meteredReason, stamp, vctx),
	})

	// 5. placement rules - see checkPlacement. Covers both "vendor profile
	// must carry no evidence" and "local profile must carry fresh, matching,
	// authorized evidence" (rule 5, including the frozen freshness boundary).
	placeOK, placeReason, placeMsg := checkPlacement(cat, profile, profileOK, stamp, vctx.PlacementEvidence, now)
	checks = append(checks, CheckResult{Name: CheckPlacement, Passed: placeOK, ReasonCode: placeReason, Message: placeMsg})

	// 6. catalog_drift_fails_closed - the stamp's catalog_version must equal
	// the LOADED catalog's own digest. A mismatch fails closed regardless of
	// whether the stamped pair also exists in the newer catalog - this never
	// reinterprets a stamp against a catalog it was not computed against.
	catalogOK := stamp.CatalogVersion == cat.Digest
	catalogReason := ReasonOK
	if !catalogOK {
		catalogReason = ReasonCatalogVersionStale
	}
	checks = append(checks, CheckResult{
		Name: CheckCatalogDrift, Passed: catalogOK, ReasonCode: catalogReason,
		Message: catalogDriftMessage(catalogOK, stamp, cat),
	})

	// 7. not_expired - injected now must be <= stamp.ExpiresAt (note: this
	// boundary is inclusive, unlike rule 5's strict placement-freshness
	// boundary - the two are frozen independently and intentionally differ).
	notExpired := !now.After(stamp.ExpiresAt)
	expiredReason := ReasonOK
	if !notExpired {
		expiredReason = ReasonStampExpired
	}
	checks = append(checks, CheckResult{
		Name: CheckNotExpired, Passed: notExpired, ReasonCode: expiredReason,
		Message: notExpiredMessage(notExpired, stamp, now),
	})

	// 8. override_within_policy - amendment 4/6: an override record is
	// PROVENANCE ONLY. It grants no bypass of any kind, so this check never
	// fails on its own account - the enforcement is structural: checks 1-7
	// above already ran against the FINAL stamped values unconditionally,
	// override or not, so there is no separate code path an override could
	// take that skips them. This check exists so that fact is visible in
	// every Verdict rather than merely true by construction.
	overrideReason := ReasonNoOverride
	if stamp.Override != nil {
		overrideReason = ReasonOverrideRecorded
	}
	checks = append(checks, CheckResult{
		Name: CheckOverrideWithin, Passed: true, ReasonCode: overrideReason,
		Message: overrideMessage(stamp.Override),
	})

	valid := true
	for _, c := range checks {
		if !c.Passed {
			valid = false
			break
		}
	}

	return Verdict{Valid: valid, Checks: checks, CatalogVersion: cat.Digest}
}

func mkCheck(name string, ok bool, okReason, failReason, okMsg, failMsg string) CheckResult {
	if ok {
		return CheckResult{Name: name, Passed: true, ReasonCode: okReason, Message: okMsg}
	}
	return CheckResult{Name: name, Passed: false, ReasonCode: failReason, Message: failMsg}
}

// approvedPair reports whether {harness, modelProfile} appears in
// routing.ApprovedPairs - read live off the package variable every call, not
// cached or copied, so a test that mutates routing.ApprovedPairs observes an
// immediate change in this package's verdicts (the acceptance case
// pair_authority_not_duplicated proves exactly this).
func approvedPair(harness, modelProfile string) bool {
	for _, p := range routing.ApprovedPairs {
		if p.Harness == harness && p.ModelProfile == modelProfile {
			return true
		}
	}
	return false
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

// entitlementPermitted reports whether the stamped pool/cost_class is still
// one of profile's declared entitlement candidates in the loaded catalog,
// and (when the pool is named) that the pool itself is still declared. This
// is a static catalog lookup only - never a live-availability check, which
// is a route-time-only concept (routing.EntitlementAvailability).
func entitlementPermitted(cat *catalog.Catalog, profile catalog.Profile, pool *string, costClass string) bool {
	for _, e := range profile.Entitlements {
		if !samePool(e.Pool, pool) {
			continue
		}
		if e.CostClass != costClass {
			continue
		}
		if pool != nil && !poolDeclared(cat, *pool) {
			continue
		}
		return true
	}
	return false
}

func samePool(a, b *string) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func poolDeclared(cat *catalog.Catalog, name string) bool {
	for _, e := range cat.EntitlementPools {
		if e.Name == name {
			return true
		}
	}
	return false
}

// checkPlacement implements rule 5 in full: the vendor-profile absence
// requirement, and the local-profile presence/matching/authorization/
// freshness requirements, including the frozen strict freshness boundary
// (owner preference: valid iff now < resolved_at + ttl_seconds; equality at
// expiry is INVALID).
func checkPlacement(cat *catalog.Catalog, profile catalog.Profile, profileOK bool, stamp Stamp, ev *PlacementEvidence, now time.Time) (ok bool, reason, message string) {
	if !stamp.PlacementRequired {
		if ev != nil {
			return false, ReasonVendorProfileWithPlacement,
				"placement_required is false (a vendor-hosted profile is resolved by the harness itself), but PlacementEvidence is present - a vendor attempt must never carry a fabricated or stray placement"
		}
		return true, ReasonNotApplicable, "placement_required is false; no PlacementEvidence is expected or present"
	}

	if ev == nil {
		return false, ReasonPlacementEvidenceMissing,
			"placement_required is true, but no PlacementEvidence was supplied - call /v1/place and revalidate with the result"
	}

	res := ev.Result
	if res.Status != "placed" {
		return false, ReasonPlacementNotPlaced,
			"placement_required is true, but the supplied PlacementEvidence has status \"" + res.Status + "\", not \"placed\""
	}
	if res.Placement == nil || res.Headers["x-placement"] != *res.Placement {
		return false, ReasonPlacementHeaderMismatch,
			"the evidence's headers[\"x-placement\"] does not equal its own placement field - the caller must copy PlaceResult.headers verbatim"
	}
	if res.Model == nil {
		return false, ReasonPlacementNotAuthorized, "a placed result with no model cannot be authorized against the stamped profile"
	}

	authorized := false
	if profileOK {
		for _, ph := range profile.Physical {
			if ph.Placement != nil && *ph.Placement == *res.Placement && ph.ModelID == *res.Model {
				authorized = true
				break
			}
		}
	}
	if authorized {
		authorized = cat.ModelAuthorizedOnPlacement(*res.Model, *res.Placement)
	}
	if authorized {
		if pol, ok := findPolicy(cat, stamp.PlacementPolicy); ok {
			authorized = false
			for _, name := range pol.PreferOrder {
				if name == *res.Placement {
					authorized = true
					break
				}
			}
		} else {
			authorized = false
		}
	}
	if !authorized {
		return false, ReasonPlacementNotAuthorized,
			"the evidence names a {model, placement} that is not authorized for the stamped model_profile and placement_policy in the loaded catalog"
	}

	if res.CatalogVersion != stamp.CatalogVersion || res.CatalogVersion != cat.Digest {
		return false, ReasonPlacementCatalogVersionDiff,
			"the evidence's catalog_version does not equal both the stamp's catalog_version and the loaded catalog's digest"
	}

	expiry := ev.ResolvedAt.Add(time.Duration(res.TTLSeconds) * time.Second)
	if !now.Before(expiry) {
		return false, ReasonPlacementEvidenceExpired,
			"the evidence has expired: now is not strictly before resolved_at + ttl_seconds (equality at expiry is invalid by design)"
	}

	return true, ReasonPlacedAndFresh, "placement_required is true and the supplied PlacementEvidence is placed, matching, authorized, on the loaded catalog, and unexpired"
}

func forbiddenForMessage(ok bool, reason string, profile catalog.Profile, stamp Stamp) string {
	if ok {
		return "no tag in task.tags appears in the profile's forbidden_for list"
	}
	if reason == ReasonProfileUnknownInCatalog {
		return "model_profile " + stamp.ModelProfile + " is not declared in the loaded catalog; forbidden_for cannot be confirmed, so this fails closed"
	}
	return "a tag in task.tags matches the profile's forbidden_for list - a hard exclusion no override may bypass"
}

func entitlementMessage(ok bool, reason string, stamp Stamp) string {
	if ok {
		return "the stamped pool/cost_class is still a declared entitlement candidate for this profile in the loaded catalog"
	}
	if reason == ReasonProfileUnknownInCatalog {
		return "model_profile " + stamp.ModelProfile + " is not declared in the loaded catalog; entitlement cannot be confirmed, so this fails closed"
	}
	return "the stamped pool/cost_class is no longer a declared entitlement candidate for this profile in the loaded catalog"
}

func meteredMessage(reason string, stamp Stamp, vctx ValidationContext) string {
	switch reason {
	case ReasonMeteredAuthorityMissing:
		return "the stamp states metered intent (metered: true), but ValidationContext carries no metered-spend authority"
	case ReasonMeteredIntentMissing:
		return "ValidationContext carries metered-spend authority, but the stamp does not state metered intent (metered: false) - authority with no stated intent is also refused"
	default:
		if stamp.Metered {
			return "metered intent and metered-spend authority are both present"
		}
		return "no metered intent is stated and no metered-spend authority is asserted"
	}
}

func catalogDriftMessage(ok bool, stamp Stamp, cat *catalog.Catalog) string {
	if ok {
		return "the stamp's catalog_version equals the loaded catalog's digest"
	}
	return "the stamp's catalog_version (" + stamp.CatalogVersion + ") does not equal the loaded catalog's digest (" + cat.Digest + ") - the catalog moved since this stamp was created; re-plan a new stamp"
}

func notExpiredMessage(ok bool, stamp Stamp, now time.Time) string {
	if ok {
		return "now is not after the stamp's expires_at"
	}
	return "now (" + now.Format(time.RFC3339) + ") is after the stamp's expires_at (" + stamp.ExpiresAt.Format(time.RFC3339) + ")"
}

func overrideMessage(o *Override) string {
	if o == nil {
		return "no override is recorded on this stamp"
	}
	return "an override is recorded (actor: " + o.Actor + "), but it grants no bypass - every other check above ran against the final stamped values on their own merits"
}
