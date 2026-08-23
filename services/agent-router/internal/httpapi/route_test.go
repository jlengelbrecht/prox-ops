package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/httpapi"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// executionProfile is the test-side decode target for a 200 /v1/route
// response - a plain mirror of execution-profile.schema.json, independent
// of the httpapi package's own wire type so a bug in one is not masked by
// reusing it in the other.
type executionProfile struct {
	Harness                string       `json:"harness"`
	ModelProfile           string       `json:"model_profile"`
	Effort                 string       `json:"effort"`
	CostClass              string       `json:"cost_class"`
	EntitlementPool        *string      `json:"entitlement_pool"`
	PlacementPolicy        string       `json:"placement_policy"`
	PlacementRequired      bool         `json:"placement_required"`
	Fallbacks              []fallback   `json:"fallbacks"`
	Metered                bool         `json:"metered"`
	Rationale              string       `json:"rationale"`
	CatalogVersion         string       `json:"catalog_version"`
	CatalogDocumentVersion string       `json:"catalog_document_version"`
	ExpiresAt              string       `json:"expires_at"`
	MeteredDenied          *meteredNote `json:"metered_denied"`
}

type fallback struct {
	Harness           string  `json:"harness"`
	ModelProfile      string  `json:"model_profile"`
	CostClass         string  `json:"cost_class"`
	EntitlementPool   *string `json:"entitlement_pool"`
	PlacementRequired bool    `json:"placement_required"`
}

type meteredNote struct {
	Code       string   `json:"code"`
	Obligation string   `json:"obligation"`
	Withheld   withheld `json:"withheld"`
}

type withheld struct {
	Harness      string `json:"harness"`
	ModelProfile string `json:"model_profile"`
	CostClass    string `json:"cost_class"`
}

func routeRequestBody(t *testing.T, fields map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatalf("marshaling route request: %v", err)
	}
	return raw
}

func postRoute(t *testing.T, e *env, token string, body []byte) *http.Response {
	t.Helper()
	return e.do(t, http.MethodPost, "/v1/route", token, body)
}

// decodeRaw unmarshals raw response bytes directly, sidestepping the
// decode[T](t, *http.Response) helper in server_test.go so a body already
// consumed by readAll (for schema validation) can still be decoded.
func decodeRaw[T any](t *testing.T, raw []byte) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("decoding response body %s: %v", raw, err)
	}
	return v
}

// --- hypothetical catalogs (amendment 1: "never the production catalog") ---
//
// The real catalog (kubernetes/apps/ai/agent-router-catalog/app/catalog-
// configmap.yaml) declares no metered entitlement pool at all, so the
// metered default-deny ladder cannot be exercised against it. These
// fixtures are Go-literal, test-only catalogs built directly from the
// catalog package's exported types - never loaded from a YAML file, never
// mistaken for production data.

func intp(i int) *int { return &i }

func fakeDigest(seed string) string {
	// 64 lowercase hex characters, deterministic per seed, obviously
	// fabricated - never mistaken for a real sha256 digest.
	h := strings.Repeat(seed, 64/len(seed)+1)[:64]
	return "sha256:" + h
}

func baseHarnesses() []catalog.Entry[catalog.Harness] {
	return []catalog.Entry[catalog.Harness]{
		{Name: "claude", Value: catalog.Harness{Supported: true, Selectable: true}},
		{Name: "codex", Value: catalog.Harness{Supported: true, Selectable: true}},
		{Name: "devin", Value: catalog.Harness{Supported: true, Selectable: true}},
	}
}

// meteredCatalog declares openai/strong with two entitlement candidates
// (subscription, then a metered one) and, when withSubstitute is true, a
// free devin/free alternative in the same (standard) band - exactly the
// shape amendment 1's "hypothetical-catalog tests" call for.
func meteredCatalog(withSubstitute bool) *catalog.Catalog {
	profiles := []catalog.Entry[catalog.Profile]{
		{Name: "openai/strong", Value: catalog.Profile{
			CostClass: "subscription", Hosting: "vendor", Selectable: true,
			Capabilities: []string{"chat", "tools"},
			Entitlements: []catalog.ProfileEntitlement{
				{Pool: strp("openai-plus"), CostClass: "subscription"},
				{Pool: strp("openai-payg"), CostClass: "metered"},
			},
			Physical: []catalog.ProfilePhysical{{ModelID: "openai/gpt-5.6-sol", Placement: nil}},
		}},
	}
	if withSubstitute {
		profiles = append(profiles, catalog.Entry[catalog.Profile]{Name: "devin/free", Value: catalog.Profile{
			CostClass: "free", Hosting: "vendor", Selectable: true,
			Capabilities: []string{"chat", "tools"},
			Entitlements: []catalog.ProfileEntitlement{{Pool: strp("devin-free"), CostClass: "free"}},
			Physical:     []catalog.ProfilePhysical{{ModelID: "swe-1-7", Placement: nil}},
		}})
	}
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Harnesses:              baseHarnesses(),
		Profiles:               profiles,
		EntitlementPools: []catalog.Entry[catalog.EntitlementPool]{
			{Name: "openai-plus", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "subscription", CredentialClass: "subscription-session", Spillover: "none"}},
			{Name: "openai-payg", Value: catalog.EntitlementPool{Provider: "openai", CostClass: "metered", CredentialClass: "provider-api-key", Spillover: "none"}},
			{Name: "devin-free", Value: catalog.EntitlementPool{Provider: "cognition", CostClass: "free", CredentialClass: "account-session", Spillover: "none"}},
		},
	}
}

// excludePools is a routing.EntitlementAvailability test seam: every pool
// is available unless explicitly listed. It never reads the request body -
// amendment 1's "request contents cannot flip the authorization answer"
// proof is for MeteredAuthority, and this seam is the separate, orthogonal
// one for entitlement exhaustion.
type excludePools map[string]bool

func (e excludePools) Available(pool string) bool { return !e[pool] }

// staticMeteredAuthority is a MeteredAuthority test seam that ignores the
// token entirely and always answers the same way - used to prove the
// authorized-principal branch without inventing a real identity mechanism.
type staticMeteredAuthority bool

func (s staticMeteredAuthority) Authorized(string) bool { return bool(s) }

// forbiddenPairCatalog declares one approved pair (claude/local-code-standard)
// whose profile is forbidden for the "security" tag, plus one alternative
// (devin/devin-free) so a hard-excluded pair still leaves a non-vacuous 200
// behind it.
func forbiddenPairCatalog() *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Harnesses:              baseHarnesses(),
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "local-code-standard", Value: catalog.Profile{
				CostClass: "free", Hosting: "local", Selectable: true,
				Capabilities: []string{"chat", "tools"}, MinContext: intp(65536),
				ForbiddenFor: []string{"security", "iam", "secrets", "prod-iac", "destructive-tools"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: nil, CostClass: "free"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "qwen36-27b", Placement: nil}},
			}},
			{Name: "devin/free", Value: catalog.Profile{
				CostClass: "free", Hosting: "vendor", Selectable: true,
				Capabilities: []string{"chat", "tools"},
				Entitlements: []catalog.ProfileEntitlement{{Pool: strp("devin-free"), CostClass: "free"}},
				Physical:     []catalog.ProfilePhysical{{ModelID: "swe-1-7", Placement: nil}},
			}},
		},
		EntitlementPools: []catalog.Entry[catalog.EntitlementPool]{
			{Name: "devin-free", Value: catalog.EntitlementPool{Provider: "cognition", CostClass: "free", CredentialClass: "account-session", Spillover: "none"}},
		},
	}
}

func contained200() map[string]any {
	return map[string]any{
		"story_id":      "STORY-TEST-1",
		"title":         "Add a DNS record for the gateway hostname",
		"summary":       "One manifest, one test.",
		"touched_paths": []string{"kubernetes/apps/network/"},
		"ambiguity":     "low",
		"blast_radius":  "low",
		"volume_hint":   "low",
		"tags":          []string{"iac"},
		"repo":          "prox-ops",
		"requester":     "bmad-pm",
	}
}

func securityTagged200() map[string]any {
	return map[string]any{
		"story_id":      "STORY-TEST-2",
		"title":         "Implement machine identity and tool-level authz",
		"summary":       "Autonomous workers authenticate as themselves.",
		"touched_paths": []string{"kubernetes/apps/mcp/"},
		"ambiguity":     "high",
		"blast_radius":  "critical",
		"volume_hint":   "high",
		"tags":          []string{"security", "iam", "secrets"},
		"repo":          "prox-ops",
		"requester":     "bmad-pm",
	}
}

// TestRouteDecisionTable is the mandated table-driven test suite for
// POST /v1/route (STORY-035-10 gate 9). Subtest names are the literal
// tokens gate 9 greps for; do not rename them without updating the gate.
func TestRouteDecisionTable(t *testing.T) {
	schema := testutil.CompileSchema(t, "execution-profile.schema.json")
	errSchema := testutil.CompileSchema(t, "error.schema.json")

	// contained: ordinary contained code change -> local-code-standard,
	// vendor fallbacks in committed priority order
	// (examples/execution-profile/local-code-standard.json).
	t.Run("contained", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		resp := postRoute(t, e, callerToken, routeRequestBody(t, contained200()))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[executionProfile](t, raw)
		if p.Harness != "claude" || p.ModelProfile != "local-code-standard" {
			t.Fatalf("primary = %s/%s, want claude/local-code-standard", p.Harness, p.ModelProfile)
		}
		if p.Effort != "medium" || p.CostClass != "free" || p.EntitlementPool != nil {
			t.Errorf("effort/cost_class/entitlement_pool = %q/%q/%v, want medium/free/nil", p.Effort, p.CostClass, p.EntitlementPool)
		}
		if !p.PlacementRequired {
			t.Error("placement_required = false, want true for a local profile")
		}
		if len(p.Fallbacks) != 2 || p.Fallbacks[0].ModelProfile != "openai/strong" || p.Fallbacks[1].ModelProfile != "devin/free" {
			t.Fatalf("fallbacks = %+v, want [openai/strong, devin/free] in that order", p.Fallbacks)
		}
		if p.Metered {
			t.Error("metered = true, want false")
		}
	})

	// security_tagged: hard exclusion holds - the unrestricted-class profile
	// is never emitted, and the strong tier is selected
	// (examples/execution-profile/security-tagged-excludes-unrestricted.json).
	t.Run("security_tagged", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		resp := postRoute(t, e, callerToken, routeRequestBody(t, securityTagged200()))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[executionProfile](t, raw)
		if p.Harness != "claude" || p.ModelProfile != "claude/strong" {
			t.Fatalf("primary = %s/%s, want claude/claude/strong", p.Harness, p.ModelProfile)
		}
		if p.Effort != "xhigh" || p.CostClass != "subscription" || p.EntitlementPool == nil || *p.EntitlementPool != "anthropic-max" {
			t.Errorf("effort/cost_class/entitlement_pool = %q/%q/%v, want xhigh/subscription/anthropic-max", p.Effort, p.CostClass, p.EntitlementPool)
		}
		if p.PlacementRequired {
			t.Error("placement_required = true, want false for a vendor profile")
		}
		if len(p.Fallbacks) != 1 || p.Fallbacks[0].ModelProfile != "openai/strong" {
			t.Fatalf("fallbacks = %+v, want exactly [openai/strong]", p.Fallbacks)
		}
		for _, name := range []string{p.ModelProfile, p.Fallbacks[0].ModelProfile} {
			if name == "local-unrestricted" {
				t.Error("local-unrestricted was emitted; invariant 12 hard exclusion violated")
			}
		}
	})

	// docs: non-code/docs work routes to the chat-only local profile with no
	// fallbacks (examples/execution-profile/docs-low-risk-cluster-only.json).
	t.Run("docs", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := routeRequestBody(t, map[string]any{
			"story_id":         "STORY-TEST-3",
			"title":            "Update architecture docs",
			"summary":          "Documentation edit across three files, no code paths touched.",
			"touched_paths":    []string{"docs/README.md", "docs/architecture.md"},
			"ambiguity":        "low",
			"blast_radius":     "low",
			"tags":             []string{"docs"},
			"repo":             "prox-ops",
			"requester":        "bmad-pm",
			"placement_policy": "cluster-only",
		})
		resp := postRoute(t, e, callerToken, body)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[executionProfile](t, raw)
		if p.Harness != "claude" || p.ModelProfile != "local-general" {
			t.Fatalf("primary = %s/%s, want claude/local-general", p.Harness, p.ModelProfile)
		}
		if p.Effort != "low" || p.PlacementPolicy != "cluster-only" || !p.PlacementRequired {
			t.Errorf("effort/placement_policy/placement_required = %q/%q/%v, want low/cluster-only/true", p.Effort, p.PlacementPolicy, p.PlacementRequired)
		}
		if len(p.Fallbacks) != 0 {
			t.Errorf("fallbacks = %+v, want none", p.Fallbacks)
		}
	})

	// stale_catalog: a caller-pinned catalog_version that does not match the
	// digest the router is serving -> 409 catalog_version_stale.
	t.Run("stale_catalog", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := contained200()
		body["catalog_version"] = fakeDigest("dead")
		resp := postRoute(t, e, callerToken, routeRequestBody(t, body))
		if resp.StatusCode != http.StatusConflict {
			t.Fatalf("status = %d, want 409", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "catalog_version_stale" {
			t.Errorf("code = %q, want catalog_version_stale", errBody.Error.Code)
		}
	})

	// no_eligible: an explicit context_size no candidate can satisfy (local
	// profiles cap at 65536, vendor profiles carry no guarantee at all) ->
	// 409 no_eligible_profile.
	t.Run("no_eligible", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := contained200()
		body["context_size"] = 200000
		resp := postRoute(t, e, callerToken, routeRequestBody(t, body))
		if resp.StatusCode != http.StatusConflict {
			t.Fatalf("status = %d, want 409", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "no_eligible_profile" {
			t.Errorf("code = %q, want no_eligible_profile", errBody.Error.Code)
		}
	})

	// metered_substitute: the best-ranked candidate resolves metered (its
	// subscription entitlement is exhausted) and a non-metered substitute
	// exists -> 200 with the substitute and a metered_denied note.
	t.Run("metered_substitute", func(t *testing.T) {
		cs := httpapi.CatalogState{Catalog: meteredCatalog(true), Digest: fakeDigest("cafe")}
		e := newEnvFull(t, cs, nil, excludePools{"openai-plus": true})
		resp := postRoute(t, e, callerToken, routeRequestBody(t, map[string]any{
			"story_id": "STORY-TEST-4", "title": "x", "ambiguity": "low", "blast_radius": "low",
		}))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[executionProfile](t, raw)
		if p.Harness != "devin" || p.ModelProfile != "devin/free" || p.CostClass != "free" {
			t.Fatalf("primary = %s/%s (%s), want devin/devin/free (free)", p.Harness, p.ModelProfile, p.CostClass)
		}
		if p.MeteredDenied == nil {
			t.Fatal("metered_denied is nil, want a note naming the withheld metered candidate")
		}
		if p.MeteredDenied.Withheld.Harness != "codex" || p.MeteredDenied.Withheld.ModelProfile != "openai/strong" || p.MeteredDenied.Withheld.CostClass != "metered" {
			t.Errorf("metered_denied.withheld = %+v, want codex/openai/strong/metered", p.MeteredDenied.Withheld)
		}
	})

	// metered_denied_no_alt: same exhaustion, but no non-metered candidate
	// remains at all -> 409 metered_denied, no profile returned.
	t.Run("metered_denied_no_alt", func(t *testing.T) {
		cs := httpapi.CatalogState{Catalog: meteredCatalog(false), Digest: fakeDigest("beef")}
		e := newEnvFull(t, cs, nil, excludePools{"openai-plus": true})
		resp := postRoute(t, e, callerToken, routeRequestBody(t, map[string]any{
			"story_id": "STORY-TEST-5", "title": "x", "ambiguity": "low", "blast_radius": "low",
		}))
		if resp.StatusCode != http.StatusConflict {
			t.Fatalf("status = %d, want 409", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "metered_denied" {
			t.Errorf("code = %q, want metered_denied", errBody.Error.Code)
		}
	})

	// metered_authorization: allow_metered:true from the ordinary principal
	// is always 403, request contents cannot flip that, and an authorized
	// principal may select a genuinely catalog-declared metered candidate.
	t.Run("metered_authorization", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body1 := contained200()
		body1["allow_metered"] = true
		resp1 := postRoute(t, e, callerToken, routeRequestBody(t, body1))
		if resp1.StatusCode != http.StatusForbidden {
			t.Fatalf("ordinary principal: status = %d, want 403", resp1.StatusCode)
		}
		raw1 := readAll(t, resp1)
		testutil.ValidateJSON(t, errSchema, raw1)
		if c := decodeRaw[errorResponse](t, raw1).Error.Code; c != "metered_authorization_required" {
			t.Errorf("code = %q, want metered_authorization_required", c)
		}

		// Request contents (requester, tags, repo) must never flip the
		// authorization answer - two requests differing only in those
		// fields both get 403 from the same (unauthorized) production seam.
		body2 := securityTagged200()
		body2["allow_metered"] = true
		resp2 := postRoute(t, e, callerToken, routeRequestBody(t, body2))
		if resp2.StatusCode != http.StatusForbidden {
			t.Fatalf("differently-shaped request, still unauthorized: status = %d, want 403", resp2.StatusCode)
		}

		// An authorized principal, against a catalog that actually declares
		// a metered candidate, may select it.
		cs := httpapi.CatalogState{Catalog: meteredCatalog(true), Digest: fakeDigest("f00d")}
		eAuthorized := newEnvFull(t, cs, staticMeteredAuthority(true), excludePools{"openai-plus": true})
		body3 := map[string]any{
			"story_id": "STORY-TEST-6", "title": "x", "ambiguity": "low", "blast_radius": "low",
			"allow_metered": true,
		}
		resp3 := postRoute(t, eAuthorized, callerToken, routeRequestBody(t, body3))
		if resp3.StatusCode != http.StatusOK {
			t.Fatalf("authorized principal: status = %d, want 200", resp3.StatusCode)
		}
		p := decodeRaw[executionProfile](t, readAll(t, resp3))
		if p.Harness != "codex" || p.ModelProfile != "openai/strong" || p.CostClass != "metered" || !p.Metered {
			t.Errorf("authorized principal got %+v, want codex/openai/strong metered=true", p)
		}
		if p.MeteredDenied != nil {
			t.Error("metered_denied present on an authorized, successfully-metered response")
		}
	})

	// non_selectable: the real catalog's non-selectable profiles
	// (local-code-fast, local-unrestricted, minimax/strong) never appear in
	// any response, primary or fallback.
	t.Run("non_selectable", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		nonSelectable := map[string]bool{"local-code-fast": true, "local-unrestricted": true, "minimax/strong": true}
		for _, body := range []map[string]any{contained200(), securityTagged200()} {
			resp := postRoute(t, e, callerToken, routeRequestBody(t, body))
			p := decodeRaw[executionProfile](t, readAll(t, resp))
			if nonSelectable[p.ModelProfile] {
				t.Errorf("primary %q is non-selectable and must never be emitted", p.ModelProfile)
			}
			for _, f := range p.Fallbacks {
				if nonSelectable[f.ModelProfile] {
					t.Errorf("fallback %q is non-selectable and must never be emitted", f.ModelProfile)
				}
			}
		}
	})

	// forbidden_pair: a profile that IS in the approved pair table is still
	// hard-excluded when a forbidden_for tag matches, leaving a lesser
	// candidate as the answer rather than silently ignoring the exclusion.
	t.Run("forbidden_pair", func(t *testing.T) {
		cs := httpapi.CatalogState{Catalog: forbiddenPairCatalog(), Digest: fakeDigest("aaaa")}
		e := newEnvFull(t, cs, nil, nil)
		resp := postRoute(t, e, callerToken, routeRequestBody(t, map[string]any{
			"story_id": "STORY-TEST-7", "title": "x", "ambiguity": "low", "blast_radius": "low",
			"tags": []string{"security"},
		}))
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		p := decodeRaw[executionProfile](t, readAll(t, resp))
		if p.ModelProfile == "local-code-standard" {
			t.Fatal("local-code-standard was emitted despite a forbidden_for tag match")
		}
		if p.Harness != "devin" || p.ModelProfile != "devin/free" {
			t.Errorf("primary = %s/%s, want devin/devin/free (the sole remaining candidate)", p.Harness, p.ModelProfile)
		}
	})

	// cross_pair: an unapproved {harness, model_profile} pairing never
	// appears even though both axis values are independently selectable in
	// the real catalog (codex and local-code-standard both are; the pair is
	// not in routing.ApprovedPairs).
	t.Run("cross_pair", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		for _, body := range []map[string]any{contained200(), securityTagged200()} {
			resp := postRoute(t, e, callerToken, routeRequestBody(t, body))
			p := decodeRaw[executionProfile](t, readAll(t, resp))
			assertApprovedPair(t, p.Harness, p.ModelProfile)
			for _, f := range p.Fallbacks {
				assertApprovedPair(t, f.Harness, f.ModelProfile)
			}
			if p.Harness == "codex" && p.ModelProfile == "local-code-standard" {
				t.Fatal("codex/local-code-standard emitted: an unapproved cross-pair, never proven reachable")
			}
		}
	})

	// determinism: identical input against an identical catalog and
	// authorization state, under an injected fixed clock, yields a
	// byte-identical serialized response - repeated.
	t.Run("determinism", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := routeRequestBody(t, contained200())
		raw1 := readAll(t, postRoute(t, e, callerToken, body))
		raw2 := readAll(t, postRoute(t, e, callerToken, body))
		if !bytes.Equal(raw1, raw2) {
			t.Fatalf("responses differ across identical calls:\n%s\n---\n%s", raw1, raw2)
		}
	})

	// unknown_field: RouteRequest is closed (additionalProperties: false);
	// an unrecognized top-level field is rejected at the wire.
	t.Run("unknown_field", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := contained200()
		body["session_id"] = "should-never-be-accepted"
		resp := postRoute(t, e, callerToken, routeRequestBody(t, body))
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "invalid_request" {
			t.Errorf("code = %q, want invalid_request", errBody.Error.Code)
		}
	})
}

func assertApprovedPair(t *testing.T, harness, modelProfile string) {
	t.Helper()
	for _, p := range routing.ApprovedPairs {
		if p.Harness == harness && p.ModelProfile == modelProfile {
			return
		}
	}
	t.Errorf("{%s, %s} is not in routing.ApprovedPairs - an unapproved pairing was emitted", harness, modelProfile)
}

// The node/heartbeat credential authenticates capacity reports, not routing.
// CallerAuth and NodeAuth are disjoint sets, so this holds by construction -
// but the property is a security boundary (a compromised edge node must not
// be able to route), so it gets a test rather than only a code reading.
func TestRoute_role_separation_node_token_cannot_route(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := postRoute(t, e, nodeToken, routeRequestBody(t, contained200()))
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("node credential routed: status = %d, want 401", resp.StatusCode)
	}
}

// An oversized body is rejected before decode - shared limit logic, pinned
// here for the route path specifically.
func TestRoute_oversized_body_rejected(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	big := make([]byte, 70*1024)
	for i := range big {
		big[i] = 'a'
	}
	resp := postRoute(t, e, callerToken, big)
	if resp.StatusCode != http.StatusBadRequest && resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body: status = %d, want 400 or 413", resp.StatusCode)
	}
}
