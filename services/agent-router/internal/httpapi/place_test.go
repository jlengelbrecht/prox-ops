package httpapi_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/httpapi"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// placeResult is the test-side decode target for a 200 /v1/place response -
// a plain mirror of place-result.schema.json, independent of the httpapi
// package's own wire type.
type placeResult struct {
	Status              string            `json:"status"`
	Model               *string           `json:"model"`
	Placement           *string           `json:"placement"`
	Readiness           *string           `json:"readiness"`
	EstimatedColdStartS *float64          `json:"estimated_cold_start_s"`
	Headers             map[string]string `json:"headers"`
	TTLSeconds          int               `json:"ttl_seconds"`
	Reason              placeReason       `json:"reason"`
	Alternatives        []placeAlt        `json:"alternatives"`
	CatalogVersion      string            `json:"catalog_version"`
}

type placeReason struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type placeAlt struct {
	Placement           string      `json:"placement"`
	Model               string      `json:"model"`
	Readiness           string      `json:"readiness"`
	EstimatedColdStartS *float64    `json:"estimated_cold_start_s"`
	Eligible            bool        `json:"eligible"`
	Reason              placeReason `json:"reason"`
}

func placeRequestBody(t *testing.T, fields map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(fields)
	if err != nil {
		t.Fatalf("marshaling place request: %v", err)
	}
	return raw
}

// discard closes a response consumed only for its side effect (test
// setup), keeping the bodyclose lint gate green without sprinkling raw
// Close calls around.
func discard(t *testing.T, resp *http.Response) {
	t.Helper()
	if err := resp.Body.Close(); err != nil {
		t.Fatalf("closing response body: %v", err)
	}
}

func postPlace(t *testing.T, e *env, token string, body []byte) *http.Response {
	t.Helper()
	return e.do(t, http.MethodPost, "/v1/place", token, body)
}

func findAlt(t *testing.T, alts []placeAlt, name string) placeAlt {
	t.Helper()
	for _, a := range alts {
		if a.Placement == name {
			return a
		}
	}
	t.Fatalf("alternative %q not found in %+v", name, alts)
	return placeAlt{}
}

// placeHeartbeatBody is heartbeatBody with an explicit free-VRAM figure, so
// place tests control VRAM feasibility independently of readiness -
// heartbeatBody's own default (21.9 GB) sits just under qwen36-27b's
// vram_gb_estimate (22), which would otherwise make every not-yet-warm
// cachyos-7900xtx candidate constraint_unsatisfiable regardless of what the
// test is actually trying to prove.
func placeHeartbeatBody(node, state string, activeModel *string, cachedModels []string, vramFreeGB float64) []byte {
	if cachedModels == nil {
		cachedModels = []string{}
	}
	body := map[string]any{
		"node":  node,
		"state": state,
		"gpu": map[string]any{
			"vendor": "amd", "model": "RX 7900 XTX", "arch": "gfx1100",
			"vram_total_gb": 24, "vram_free_gb": vramFreeGB, "utilization_pct": 3,
		},
		"runtime": map[string]any{
			"kind": "llama-swap+llama.cpp", "version": "1.0", "endpoint": "https://edge:8443",
		},
		"active_model":      activeModel,
		"cached_models":     cachedModels,
		"preemptible":       true,
		"interactive":       state == "INTERACTIVE",
		"ac_power":          true,
		"cluster_reachable": true,
		"last_heartbeat":    "2026-08-21T12:00:00Z",
		"capabilities":      []string{"chat", "tools"},
		"max_context":       65536,
	}
	raw, _ := json.Marshal(body)
	return raw
}

// catalogMissingProfile is a synthetic catalog (amendment 1: "never the
// production catalog") that declares no profiles at all - any
// model_profile enum member is 404 unknown_profile against it, exercising
// the "catalog dropped it after the attempt was stamped" window.
func catalogMissingProfile() *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Placements: []catalog.Entry[catalog.Placement]{
			{Name: "kserve-a5000", Value: catalog.Placement{Status: "available", Selectable: true, Kind: "kserve"}},
		},
		Policies: []catalog.Entry[catalog.Policy]{
			{Name: "prefer-warm-local", Value: catalog.Policy{PreferOrder: []string{"kserve-a5000"}, WarmPreference: "strict", ResolvesToday: true}},
		},
	}
}

// catalogMissingPolicy is the mirror: a profile exists, but no policy does,
// exercising 404 unknown_placement_policy.
func catalogMissingPolicy() *catalog.Catalog {
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Placements: []catalog.Entry[catalog.Placement]{
			{Name: "kserve-a5000", Value: catalog.Placement{Status: "available", Selectable: true, Kind: "kserve"}},
		},
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "local-code-standard", Value: catalog.Profile{
				Hosting: "local", Selectable: true,
				Physical: []catalog.ProfilePhysical{{ModelID: "qwen36-27b", Placement: strp("kserve-a5000")}},
			}},
		},
	}
}

func localCodeStandardBody(policy string) map[string]any {
	return map[string]any{
		"model_profile":    "local-code-standard",
		"placement_policy": policy,
	}
}

// TestPlaceDecisionTable is the mandated table-driven test suite for
// POST /v1/place (STORY-035-11 gate 9). Subtest names are the literal
// tokens gate 9 greps for; do not rename them without updating the gate.
func TestPlaceDecisionTable(t *testing.T) {
	schema := testutil.CompileSchema(t, "place-result.schema.json")
	errSchema := testutil.CompileSchema(t, "error.schema.json")

	// warm_edge: the edge is warm, the cluster GPU is never probed (readiness
	// unknown), and prefer-warm-local's strict warm preference picks the
	// warm edge over prefer_order alone (examples/place/placed-warm-edge.json).
	t.Run("warm_edge", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		resp := postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))
		defer discard(t, resp)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)

		if p.Status != "placed" || p.Placement == nil || *p.Placement != nodeName {
			t.Fatalf("placement = %+v, want placed on %s", p, nodeName)
		}
		if p.Readiness == nil || *p.Readiness != "warm" {
			t.Errorf("readiness = %v, want warm", p.Readiness)
		}
		if p.EstimatedColdStartS == nil || *p.EstimatedColdStartS != 0 {
			t.Errorf("estimated_cold_start_s = %v, want 0", p.EstimatedColdStartS)
		}
		if p.Reason.Code != "placed_warm" {
			t.Errorf("reason.code = %q, want placed_warm", p.Reason.Code)
		}
		if p.Headers["x-placement"] != nodeName {
			t.Errorf("headers = %+v, want x-placement=%s", p.Headers, nodeName)
		}
		if p.TTLSeconds != 30 {
			t.Errorf("ttl_seconds = %d, want 30", p.TTLSeconds)
		}
		alt := findAlt(t, p.Alternatives, "kserve-a5000")
		if !alt.Eligible || alt.Reason.Code != "not_selected_colder" {
			t.Errorf("kserve-a5000 alternative = %+v, want eligible/not_selected_colder", alt)
		}
	})

	// cached_vs_absent: cached and absent are distinct costs and distinct
	// readiness values, never collapsed (owner ruling R11).
	t.Run("cached_vs_absent", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", nil, []string{"qwen36-27b"}, 24)))
		pCached := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
		if pCached.Readiness == nil || *pCached.Readiness != "cached" {
			t.Fatalf("readiness = %v, want cached", pCached.Readiness)
		}
		if pCached.Reason.Code != "placed_cached" {
			t.Errorf("reason.code = %q, want placed_cached", pCached.Reason.Code)
		}
		if pCached.EstimatedColdStartS == nil || *pCached.EstimatedColdStartS != 6 {
			t.Errorf("estimated_cold_start_s = %v, want 6 (cachyos-7900xtx catalog estimate)", pCached.EstimatedColdStartS)
		}

		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", nil, nil, 24)))
		pAbsent := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
		if pAbsent.Readiness == nil || *pAbsent.Readiness != "absent" {
			t.Fatalf("readiness = %v, want absent", pAbsent.Readiness)
		}
		if pAbsent.Reason.Code != "placed_cold" {
			t.Errorf("reason.code = %q, want placed_cold", pAbsent.Reason.Code)
		}
		if pAbsent.EstimatedColdStartS != nil {
			t.Errorf("estimated_cold_start_s = %v, want null: no download+load estimate exists for absent", pAbsent.EstimatedColdStartS)
		}
	})

	// interactive_withdraw: INTERACTIVE narrows eligibility immediately, no
	// timeout.
	t.Run("interactive_withdraw", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "INTERACTIVE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "placed" || p.Placement == nil || *p.Placement != "kserve-a5000" {
			t.Fatalf("placement = %+v, want placed on kserve-a5000 (the only eligible candidate)", p)
		}
		if p.Reason.Code != "placed_only_candidate" {
			t.Errorf("reason.code = %q, want placed_only_candidate", p.Reason.Code)
		}
		alt := findAlt(t, p.Alternatives, nodeName)
		if alt.Eligible || alt.Reason.Code != "withdrawn_interactive" {
			t.Errorf("%s alternative = %+v, want ineligible/withdrawn_interactive", nodeName, alt)
		}
	})

	// offline_stale: silence past the offline window removes a placement.
	t.Run("offline_stale", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))
		e.advance(91 * time.Second)

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "placed" || p.Placement == nil || *p.Placement != "kserve-a5000" {
			t.Fatalf("placement = %+v, want placed on kserve-a5000 after the edge goes silent", p)
		}
		alt := findAlt(t, p.Alternatives, nodeName)
		if alt.Eligible || alt.Reason.Code != "offline" {
			t.Errorf("%s alternative = %+v, want ineligible/offline", nodeName, alt)
		}
	})

	// unreachable: cluster_reachable:false narrows eligibility even while
	// state is AVAILABLE - a different mechanism from silence, same code.
	t.Run("unreachable", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBodyUnreachable(nodeName, "AVAILABLE")))

		p := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
		if p.Status != "placed" || p.Placement == nil || *p.Placement != "kserve-a5000" {
			t.Fatalf("placement = %+v, want placed on kserve-a5000", p)
		}
		alt := findAlt(t, p.Alternatives, nodeName)
		if alt.Eligible || alt.Reason.Code != "offline" {
			t.Errorf("%s alternative = %+v, want ineligible/offline (unreachable, not not_selectable)", nodeName, alt)
		}
	})

	// cluster_only: never leaves the cluster, even when the edge is warm and
	// eligible - the edge is never even a candidate under this policy.
	t.Run("cluster_only", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("cluster-only"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "placed" || p.Placement == nil || *p.Placement != "kserve-a5000" {
			t.Fatalf("placement = %+v, want placed on kserve-a5000 even with a warm edge", p)
		}
		if len(p.Alternatives) != 0 {
			t.Errorf("alternatives = %+v, want none: the edge is never a candidate under cluster-only", p.Alternatives)
		}
	})

	// edge_only: never silently chooses the cluster - an unavailable edge
	// under edge-only is an explicit empty result, not a KServe fallback.
	t.Run("edge_only", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "INTERACTIVE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("edge-only"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "unavailable" || p.Placement != nil {
			t.Fatalf("result = %+v, want an explicit unavailable result, never a substitute placement", p)
		}
		if len(p.Headers) != 0 {
			t.Errorf("headers = %+v, want empty", p.Headers)
		}
		for _, a := range p.Alternatives {
			if a.Placement == "kserve-a5000" {
				t.Error("kserve-a5000 appears as an alternative under edge-only: it must never even be considered")
			}
		}
	})

	// any24: the VRAM floor is honoured from catalog-measured capacity only -
	// the unmeasured edge card does not qualify merely because it is warm.
	t.Run("any24", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("any-24gb"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "placed" || p.Placement == nil || *p.Placement != "kserve-a5000" {
			t.Fatalf("placement = %+v, want placed on kserve-a5000 (the only measured 24GB-class placement)", p)
		}
		alt := findAlt(t, p.Alternatives, nodeName)
		if alt.Eligible || alt.Reason.Code != "constraint_unsatisfiable" {
			t.Errorf("%s alternative = %+v, want ineligible/constraint_unsatisfiable despite being warm", nodeName, alt)
		}
	})

	// unavailable: every candidate withdrawn under a policy that resolves
	// today produces the frozen explicit-empty-result shape, never a
	// substitute.
	t.Run("unavailable", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "DRAINING", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))

		raw := readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("edge-only"))))
		testutil.ValidateJSON(t, schema, raw)
		p := decodeRaw[placeResult](t, raw)
		if p.Status != "unavailable" {
			t.Fatalf("status = %q, want unavailable", p.Status)
		}
		if p.Model != nil || p.Placement != nil || p.Readiness != nil || p.EstimatedColdStartS != nil {
			t.Errorf("result = %+v, want model/placement/readiness/estimated_cold_start_s all null", p)
		}
		if p.Reason.Code != "all_candidates_withdrawn" {
			t.Errorf("reason.code = %q, want all_candidates_withdrawn", p.Reason.Code)
		}
		if p.TTLSeconds != 30 {
			t.Errorf("ttl_seconds = %d, want 30 even on an unavailable result", p.TTLSeconds)
		}
	})

	// unknown_profile: the catalog no longer defines a profile this
	// contract's vocabulary still carries -> 404.
	t.Run("unknown_profile", func(t *testing.T) {
		cs := httpapi.CatalogState{Catalog: catalogMissingProfile(), Digest: fakeDigest("aaaa")}
		e := newEnvFull(t, cs, nil, nil)
		resp := postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))
		defer discard(t, resp)
		raw := readAll(t, resp)
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("status = %d, want 404; body %.200s", resp.StatusCode, raw)
		}
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "unknown_profile" {
			t.Errorf("code = %q, want unknown_profile", errBody.Error.Code)
		}
	})

	// unknown_policy: the catalog no longer defines a policy this contract's
	// vocabulary still carries -> 404.
	t.Run("unknown_policy", func(t *testing.T) {
		cs := httpapi.CatalogState{Catalog: catalogMissingPolicy(), Digest: fakeDigest("bbbb")}
		e := newEnvFull(t, cs, nil, nil)
		resp := postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))
		defer discard(t, resp)
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("status = %d, want 404", resp.StatusCode)
		}
		raw := readAll(t, resp)
		testutil.ValidateJSON(t, errSchema, raw)
		errBody := decodeRaw[errorResponse](t, raw)
		if errBody.Error.Code != "unknown_placement_policy" {
			t.Errorf("code = %q, want unknown_placement_policy", errBody.Error.Code)
		}
	})

	// stale_catalog: a caller-pinned catalog_version that does not match the
	// digest the router is serving -> 409.
	t.Run("stale_catalog", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		body := localCodeStandardBody("prefer-warm-local")
		body["catalog_version"] = fakeDigest("dead")
		resp := postPlace(t, e, callerToken, placeRequestBody(t, body))
		defer discard(t, resp)
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

	// determinism: identical input against an identical catalog and
	// live-capacity snapshot, under an injected fixed clock, yields a
	// byte-identical serialized response - repeated.
	t.Run("determinism", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))
		body := placeRequestBody(t, localCodeStandardBody("prefer-warm-local"))
		raw1 := readAll(t, postPlace(t, e, callerToken, body))
		raw2 := readAll(t, postPlace(t, e, callerToken, body))
		if !bytes.Equal(raw1, raw2) {
			t.Fatalf("responses differ across identical calls:\n%s\n---\n%s", raw1, raw2)
		}
	})

	// no_expand: an unauthorized active_model claim must never be treated as
	// warm, in either direction - contrast with the authorized case.
	t.Run("no_expand", func(t *testing.T) {
		e := newEnv(t, realCatalogState(t))
		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("a-model-the-catalog-never-placed-here"), nil, 24)))

		p1 := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
		if p1.Readiness == nil || *p1.Readiness == "warm" {
			t.Errorf("readiness = %v, want anything but warm for an unauthorized active_model claim", p1.Readiness)
		}
		if p1.Reason.Code == "placed_warm" {
			t.Error("reason.code = placed_warm, want anything else: an unauthorized claim must not expand eligibility")
		}

		discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
			placeHeartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}, 24)))
		p2 := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
		if p2.Readiness == nil || *p2.Readiness != "warm" {
			t.Errorf("readiness = %v, want warm for a catalog-authorized active_model claim (contrast case)", p2.Readiness)
		}
		if p2.Reason.Code != "placed_warm" {
			t.Errorf("reason.code = %q, want placed_warm (contrast case)", p2.Reason.Code)
		}
	})
}

// The node/heartbeat credential reports capacity; it must not obtain
// placements. Same security boundary TestRoute_role_separation pins for
// /v1/route, held here for /v1/place specifically.
func TestPlace_role_separation_node_token_cannot_place(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := postPlace(t, e, nodeToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))
	defer discard(t, resp)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("node credential placed: status = %d, want 401", resp.StatusCode)
	}
}

// An oversized body is rejected before decode - shared limit logic, pinned
// for the place path specifically.
func TestPlace_oversized_body_rejected(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	big := make([]byte, 70*1024)
	for i := range big {
		big[i] = 'a'
	}
	resp := postPlace(t, e, callerToken, big)
	defer discard(t, resp)
	if resp.StatusCode != http.StatusBadRequest && resp.StatusCode != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized body: status = %d, want 400 or 413", resp.StatusCode)
	}
}

// PlaceRequest is a closed schema (additionalProperties: false); an unknown
// member is 400 invalid_request naming the field, never silently dropped.
func TestPlace_unknown_field_rejected(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	body := localCodeStandardBody("prefer-warm-local")
	body["bogus_field"] = 1
	resp := postPlace(t, e, callerToken, placeRequestBody(t, body))
	defer discard(t, resp)
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("unknown field: status = %d, want 400", resp.StatusCode)
	}
}

// catalogTwoModelsOnePlacement authorizes TWO models on the same edge
// placement, with the profile under test serving model-b there. The
// production catalog never does this today (every physical[] placement
// carries exactly one model), which is why per-placement readiness was
// inert - this synthetic catalog makes the distinction observable.
func catalogTwoModelsOnePlacement() *catalog.Catalog {
	six := 6.0
	eight := 8.0
	return &catalog.Catalog{
		DocumentVersion:        "9.9.9",
		SchemaVersion:          1,
		DefaultPlacementPolicy: "prefer-warm-local",
		Placements: []catalog.Entry[catalog.Placement]{
			{Name: nodeName, Value: catalog.Placement{
				Status: "available", Selectable: true, Kind: "edge",
				Capacity:           catalog.Capacity{VramGB: &[]float64{24}[0]},
				ColdStartSEstimate: &six,
			}},
		},
		Models: map[string]catalog.Model{
			"model-a": {Hosting: "local", Placements: []string{nodeName}, VramGbEstimate: &eight},
			"model-b": {Hosting: "local", Placements: []string{nodeName}, VramGbEstimate: &eight},
		},
		Profiles: []catalog.Entry[catalog.Profile]{
			{Name: "local-code-standard", Value: catalog.Profile{
				Hosting: "local", Selectable: true,
				Physical: []catalog.ProfilePhysical{{ModelID: "model-b", Placement: strp(nodeName)}},
			}},
		},
		Policies: []catalog.Entry[catalog.Policy]{
			{Name: "prefer-warm-local", Value: catalog.Policy{
				PreferOrder: []string{nodeName}, WarmPreference: "strict",
				AllowColdStart: true, ResolvesToday: true,
			}},
		},
	}
}

// Readiness is a property of the CANDIDATE (placement, model) pair, not of
// the placement: model-a being active on the node must not let model-b's
// candidate claim "warm, no load step" (security review, cycle 1). With
// model-a warm and model-b uncached, model-b places cold with a null
// estimate; caching model-b upgrades it to cached with the placement's
// load estimate - never to warm.
func TestPlace_readiness_is_per_model_not_per_placement(t *testing.T) {
	e := newEnv(t, httpapi.CatalogState{Catalog: catalogTwoModelsOnePlacement(), Digest: "sha256:synthetic-two-models"})
	discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
		placeHeartbeatBody(nodeName, "AVAILABLE", strp("model-a"), []string{"model-a"}, 24)))

	p := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
	if p.Status != "placed" || p.Placement == nil || *p.Placement != nodeName {
		t.Fatalf("placement = %+v, want placed on %s", p, nodeName)
	}
	if p.Model == nil || *p.Model != "model-b" {
		t.Fatalf("model = %v, want model-b (the profile's physical pair)", p.Model)
	}
	if p.Readiness == nil || *p.Readiness == "warm" {
		t.Fatalf("readiness = %v: model-a's warmth leaked onto model-b's candidate", p.Readiness)
	}
	if *p.Readiness != "absent" {
		t.Errorf("readiness = %s, want absent", *p.Readiness)
	}
	if p.Reason.Code == "placed_warm" {
		t.Errorf("reason.code = placed_warm: another model's warmth was claimed")
	}
	if p.EstimatedColdStartS != nil {
		t.Errorf("estimated_cold_start_s = %v, want null for absent", p.EstimatedColdStartS)
	}

	discard(t, e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
		placeHeartbeatBody(nodeName, "AVAILABLE", strp("model-a"), []string{"model-a", "model-b"}, 24)))
	p2 := decodeRaw[placeResult](t, readAll(t, postPlace(t, e, callerToken, placeRequestBody(t, localCodeStandardBody("prefer-warm-local")))))
	if p2.Readiness == nil || *p2.Readiness != "cached" {
		t.Fatalf("cached model-b: readiness = %v, want cached", p2.Readiness)
	}
	if p2.Reason.Code == "placed_warm" {
		t.Errorf("reason.code = placed_warm: cached must never be presented as warm")
	}
	if p2.EstimatedColdStartS == nil || *p2.EstimatedColdStartS != 6 {
		t.Errorf("estimated_cold_start_s = %v, want 6 (the placement's load estimate)", p2.EstimatedColdStartS)
	}
}
