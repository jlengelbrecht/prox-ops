// Package routing implements the POST /v1/route decision: the frozen
// pipeline in contracts/agent-router/openapi.yaml's RouteRequest/
// ExecutionProfile description and STORY-035-10's "Route policy" section.
// It answers only what ADE-BOUNDARY.md allows: harness, model profile,
// effort, entitlement/cost decision and (when local) placement policy. It
// holds no session state, calls no Kubernetes API and never addresses the
// predictor - contracts/agent-router/verify-ade-boundary.sh and this
// story's gate 8 enforce that mechanically.
package routing

// Band is the intelligence-tier bucket a task is routed within. Chosen from
// task-signal practice (EPIC-035 section 11): contained/low-ambiguity work
// needs no more than the standard tier; high-ambiguity or high/critical
// blast-radius work escalates to the strong tier; a task that touches
// nothing but documentation is routed separately from either.
type Band string

const (
	BandDocs     Band = "docs"
	BandStandard Band = "standard"
	BandStrong   Band = "strong"
)

// Pair is one approved {harness, model_profile} candidate - normative
// router data, committed with the code per amendment 2 ("No Cartesian
// product"). Bands lists which task bands this pair may serve. Priority is
// the explicit, unique-per-pair integer amendment 2 requires as the
// deterministic tie-break: it orders fallbacks (step 5 of the route policy)
// and breaks ties in primary selection. Never derived from map iteration or
// catalog serialization order.
//
// Sourced from the committed route fixtures
// (contracts/agent-router/examples/execution-profile/*.json), which pair
// harness and profile concretely, and from ADE-BOUNDARY.md/README's ruling
// that the MiniMax execution path is harness:claude + model_profile:
// minimax/strong. No pair is listed by assumption or naming convention: in
// particular no cloud harness (codex, devin) is paired with a local-
// profile, because that reachability is unproven (amendment 2).
type Pair struct {
	Harness      string
	ModelProfile string
	Bands        []Band
	Priority     int
}

func (p Pair) inBand(b Band) bool {
	for _, x := range p.Bands {
		if x == b {
			return true
		}
	}
	return false
}

// ApprovedPairs is the complete, finite candidate table step 1 of the route
// policy joins against catalog selectability. A pair whose harness, profile
// or pool goes non-selectable in the catalog is dead automatically - the
// catalog stays the authority on that, this table only says which pairings
// are approved at all.
var ApprovedPairs = []Pair{
	// claude/strong: the strong-tier vendor default, reserved for work that
	// needs judgement (security-tagged-excludes-unrestricted.json).
	{Harness: "claude", ModelProfile: "claude/strong", Bands: []Band{BandStrong}, Priority: 0},
	// local-code-standard: the default free local coding profile
	// (local-code-standard.json).
	{Harness: "claude", ModelProfile: "local-code-standard", Bands: []Band{BandStandard}, Priority: 1},
	// openai/strong: the standing fallback tier in both bands (EPIC-035
	// section 11), reachable via the codex harness's own subscription.
	{Harness: "codex", ModelProfile: "openai/strong", Bands: []Band{BandStandard, BandStrong}, Priority: 2},
	// devin/free: free capacity, standard band only - not offered as an
	// alternative for work that needed the strong tier in the first place.
	{Harness: "devin", ModelProfile: "devin/free", Bands: []Band{BandStandard}, Priority: 3},
	// local-general: the non-code/docs bucket's sole candidate
	// (docs-low-risk-cluster-only.json).
	{Harness: "claude", ModelProfile: "local-general", Bands: []Band{BandDocs}, Priority: 4},
	// minimax/strong: declared per ADE-BOUNDARY.md "MiniMax is a model, not
	// a harness" - the approved path is harness:claude + model_profile:
	// minimax/strong. selectable:false in catalog 1.3.0 makes it dead
	// automatically at candidate construction; listed here so a later
	// catalog flip needs no router change (amendment 2).
	{Harness: "claude", ModelProfile: "minimax/strong", Bands: []Band{BandStrong}, Priority: 5},
}
