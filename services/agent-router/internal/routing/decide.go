package routing

import (
	"sort"
	"strings"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
)

// requiredCapabilities is fixed rather than request-supplied: every /v1/route
// caller is an autonomous coding agent (EPIC-035 scope), so tool calling is
// always required. RouteRequest carries no capabilities field - this is a
// router-side constant, not something a caller can widen or narrow.
var requiredCapabilities = []string{"tools"}

// Input is the routing-relevant subset of RouteRequest, decoded and
// pre-validated by httpapi. placement_policy has already been resolved to
// either the caller's own choice or the catalog default - Decide only
// stamps it, since it is inert unless the chosen profile is local.
type Input struct {
	Ambiguity       string
	BlastRadius     string
	ContextSize     *int
	VolumeHint      string
	Tags            []string
	TouchedPaths    []string
	PlacementPolicy string
}

// Candidate is one fully-resolved routing option: an approved pair, backed
// by one of its profile's declared entitlement candidates, in a specific
// band.
type Candidate struct {
	Pair              Pair
	CostClass         string
	EntitlementPool   *string
	PlacementRequired bool
}

// WithheldCandidate names the metered option the router would have
// returned had allow_metered carried authority - the shape
// ExecutionProfile.metered_denied.withheld and error.details.withheld both
// mirror.
type WithheldCandidate struct {
	Harness      string
	ModelProfile string
}

// Decision is the resolved routing answer, pre-serialization. httpapi wraps
// this with catalog_version, catalog_document_version, expires_at and a
// rendered rationale to build the wire ExecutionProfile.
type Decision struct {
	Harness           string
	ModelProfile      string
	Effort            string
	CostClass         string
	EntitlementPool   *string
	PlacementPolicy   string
	PlacementRequired bool
	Fallbacks         []Candidate
	Metered           bool
	Band              Band
	MeteredDenied     *WithheldCandidate
}

// Outcome enumerates what Decide can produce, mirroring the frozen error
// taxonomy so httpapi does not have to re-derive it from a Decision.
type Outcome int

const (
	OutcomeOK Outcome = iota
	OutcomeNoEligibleProfile
	OutcomeMeteredDenied
)

// Result is Decide's return value.
type Result struct {
	Outcome  Outcome
	Decision *Decision
	Withheld *WithheldCandidate
}

// Decide runs the normative route policy pipeline (STORY-035-10 "Route
// policy") against the loaded catalog. It is pure and deterministic: the
// same cat, in, authorizedForMetered and avail always yield the same
// Result, with no wall-clock or randomness involved (amendment 3).
//
// authorizedForMetered must already reflect BOTH allow_metered intent and
// independently-held metered-spend authority - the metered_authorization_required
// 403 is decided by httpapi before Decide is ever called, because that
// check applies even when no metered candidate would otherwise be
// constructed at all.
func Decide(cat *catalog.Catalog, in Input, authorizedForMetered bool, avail EntitlementAvailability) Result {
	band := bandFor(in)
	all := buildCandidates(cat, in, band, avail)
	if len(all) == 0 {
		return Result{Outcome: OutcomeNoEligibleProfile}
	}

	ranked := append([]Candidate(nil), all...)
	sortByPriority(ranked)

	top := ranked[0]
	var withheld *WithheldCandidate
	if top.CostClass == "metered" && !authorizedForMetered {
		withheld = &WithheldCandidate{Harness: top.Pair.Harness, ModelProfile: top.Pair.ModelProfile}
		var rest []Candidate
		for _, c := range ranked[1:] {
			if c.CostClass == "metered" {
				continue
			}
			rest = append(rest, c)
		}
		if len(rest) == 0 {
			return Result{Outcome: OutcomeMeteredDenied, Withheld: withheld}
		}
		top = rest[0]
	}

	// Fallbacks: every remaining survivor other than the chosen primary,
	// excluding any metered candidate that is not permitted, ordered purely
	// by the pair table's committed priority (step 5: "ranking-then-priority
	// order" - once a primary is chosen, priority alone orders the rest).
	var fallbacks []Candidate
	for _, c := range all {
		if c.Pair.Harness == top.Pair.Harness && c.Pair.ModelProfile == top.Pair.ModelProfile {
			continue
		}
		if c.CostClass == "metered" && !authorizedForMetered {
			continue
		}
		fallbacks = append(fallbacks, c)
	}
	sortByPriority(fallbacks)

	decision := &Decision{
		Harness:           top.Pair.Harness,
		ModelProfile:      top.Pair.ModelProfile,
		Effort:            effortFor(band, in.BlastRadius),
		CostClass:         top.CostClass,
		EntitlementPool:   top.EntitlementPool,
		PlacementPolicy:   in.PlacementPolicy,
		PlacementRequired: top.PlacementRequired,
		Fallbacks:         fallbacks,
		Metered:           top.CostClass == "metered",
		Band:              band,
		MeteredDenied:     withheld,
	}
	return Result{Outcome: OutcomeOK, Decision: decision}
}

// sortByPriority is the whole ranking-and-tie-break order (steps 3 and 4 of
// the route policy) collapsed into one deterministic key: the pair table's
// committed Priority integer. Priority already encodes the qualitative
// judgment step 3 describes (intelligence tier fit, cost preference,
// availability) as a single committed number per pair, which is what makes
// it usable as a total order rather than only a tie-break - two pairs never
// share a priority, so no secondary key is ever needed. This is never Go
// map iteration order and never incidental catalog serialization order.
func sortByPriority(cands []Candidate) {
	sort.SliceStable(cands, func(i, j int) bool {
		return cands[i].Pair.Priority < cands[j].Pair.Priority
	})
}

// bandFor implements step 3's "blast-radius/ambiguity/risk tags select the
// intelligence tier". A docs-only change (every touched_paths entry looks
// like documentation, low ambiguity, no more than medium blast radius) is
// routed to the non-code bucket first; otherwise high ambiguity or a
// high/critical blast radius escalates to the strong tier, and everything
// else is standard. This reproduces the three real-catalog fixtures
// exactly: local-code-standard.json (standard), security-tagged-
// excludes-unrestricted.json (strong), docs-low-risk-cluster-only.json
// (docs).
func bandFor(in Input) Band {
	if isDocsOnly(in.TouchedPaths) && in.Ambiguity == "low" && in.BlastRadius != "high" && in.BlastRadius != "critical" {
		return BandDocs
	}
	if in.Ambiguity == "high" || in.BlastRadius == "high" || in.BlastRadius == "critical" {
		return BandStrong
	}
	return BandStandard
}

func isDocsOnly(paths []string) bool {
	if len(paths) == 0 {
		return false
	}
	for _, p := range paths {
		if !looksLikeDocs(p) {
			return false
		}
	}
	return true
}

func looksLikeDocs(p string) bool {
	lower := strings.ToLower(p)
	return strings.Contains(lower, "docs/") || strings.HasSuffix(lower, ".md") || strings.HasSuffix(lower, ".mdx") || strings.Contains(lower, "readme")
}

// effortFor maps the selected band, and blast_radius within the strong
// band, to a reasoning-effort tier. critical blast radius inside the strong
// band gets xhigh (security-tagged-excludes-unrestricted.json); everything
// else in the strong band gets high; standard gets medium
// (local-code-standard.json); docs gets low (docs-low-risk-cluster-only.json).
func effortFor(band Band, blastRadius string) string {
	switch band {
	case BandDocs:
		return "low"
	case BandStrong:
		if blastRadius == "critical" {
			return "xhigh"
		}
		return "high"
	default:
		return "medium"
	}
}

func profileIndex(cat *catalog.Catalog) map[string]catalog.Profile {
	m := make(map[string]catalog.Profile, len(cat.Profiles))
	for _, e := range cat.Profiles {
		m[e.Name] = e.Value
	}
	return m
}

func harnessIndex(cat *catalog.Catalog) map[string]catalog.Harness {
	m := make(map[string]catalog.Harness, len(cat.Harnesses))
	for _, e := range cat.Harnesses {
		m[e.Name] = e.Value
	}
	return m
}

func poolIndex(cat *catalog.Catalog) map[string]catalog.EntitlementPool {
	m := make(map[string]catalog.EntitlementPool, len(cat.EntitlementPools))
	for _, e := range cat.EntitlementPools {
		m[e.Name] = e.Value
	}
	return m
}

// buildCandidates is step 1 (candidate construction) and step 2 (hard
// filters a-d; the metered ladder of step 2e is applied later in Decide,
// since it depends on the ranked winner, not on each candidate in
// isolation) of the route policy.
func buildCandidates(cat *catalog.Catalog, in Input, band Band, avail EntitlementAvailability) []Candidate {
	profiles := profileIndex(cat)
	harnesses := harnessIndex(cat)
	pools := poolIndex(cat)

	var out []Candidate
	for _, pair := range ApprovedPairs {
		if !pair.inBand(band) {
			continue
		}
		h, ok := harnesses[pair.Harness]
		if !ok || !h.Supported || !h.Selectable || h.RouterBehaviour == "refuse-to-emit" {
			continue
		}
		p, ok := profiles[pair.ModelProfile]
		if !ok || !p.Selectable {
			continue
		}
		if tagsForbidden(p.ForbiddenFor, in.Tags) {
			continue
		}
		if !contextSatisfied(in.ContextSize, p.MinContext) {
			continue
		}
		if !capabilitiesSatisfied(p.Capabilities) {
			continue
		}
		costClass, pool, ok := resolveEntitlement(p, pools, avail)
		if !ok {
			continue
		}
		out = append(out, Candidate{Pair: pair, CostClass: costClass, EntitlementPool: pool, PlacementRequired: p.Hosting == "local"})
	}
	return out
}

func tagsForbidden(forbidden, tags []string) bool {
	if len(forbidden) == 0 {
		return false
	}
	set := make(map[string]bool, len(forbidden))
	for _, f := range forbidden {
		set[f] = true
	}
	for _, t := range tags {
		if set[t] {
			return true
		}
	}
	return false
}

// contextSatisfied implements the RouteRequest.context_size rule verbatim:
// "checked against a profile's guaranteed min_context; a profile whose
// guarantee is null cannot satisfy an explicit requirement here." No
// requirement (want == nil) always passes.
func contextSatisfied(want, guarantee *int) bool {
	if want == nil {
		return true
	}
	if guarantee == nil {
		return false
	}
	return *want <= *guarantee
}

func capabilitiesSatisfied(have []string) bool {
	set := make(map[string]bool, len(have))
	for _, c := range have {
		set[c] = true
	}
	for _, need := range requiredCapabilities {
		if !set[need] {
			return false
		}
	}
	return true
}

// resolveEntitlement walks a profile's declared entitlement candidates in
// order and returns the first one that is actually fundable: a nil pool
// (local execution, free) is always fundable, and a named pool is fundable
// when it exists in the catalog's entitlement_pools table and avail reports
// it available. This is the only place entitlement exhaustion is
// consulted, and avail is a test seam in every caller but the production
// AlwaysAvailable (amendment 1: "Production must not claim it can observe
// subscription exhaustion").
func resolveEntitlement(p catalog.Profile, pools map[string]catalog.EntitlementPool, avail EntitlementAvailability) (costClass string, pool *string, ok bool) {
	for _, e := range p.Entitlements {
		if e.Pool == nil {
			return e.CostClass, nil, true
		}
		if _, exists := pools[*e.Pool]; !exists {
			continue
		}
		if avail.Available(*e.Pool) {
			name := *e.Pool
			return e.CostClass, &name, true
		}
	}
	return "", nil, false
}
