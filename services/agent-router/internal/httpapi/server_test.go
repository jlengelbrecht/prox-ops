package httpapi_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/auth"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/httpapi"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

const (
	callerToken = "test-caller-token"
	nodeToken   = "test-node-token-cachyos"
	nodeName    = "cachyos-7900xtx"
)

// env's clock is guarded by mu because it is written by the test goroutine
// (advance) and read from the HTTP server's own goroutine(s) via the
// capacity.Store's injected now func - concurrent, unsynchronized access to
// the same *time.Time would be a data race.
type env struct {
	server *httptest.Server
	mu     sync.Mutex
	clock  time.Time
}

func realCatalogState(t *testing.T) httpapi.CatalogState {
	t.Helper()
	path := testutil.ExtractCatalogYAML(t)
	cat, digest, err := catalog.Load(path)
	if err != nil {
		t.Fatalf("catalog.Load: %v", err)
	}
	return httpapi.CatalogState{Catalog: cat, Digest: digest}
}

func newEnv(t *testing.T, cs httpapi.CatalogState) *env {
	t.Helper()
	return newEnvFull(t, cs, nil, nil)
}

// newEnvFull is newEnv with the metered-authorization and entitlement-
// availability seams exposed, so route tests can exercise the authorized-
// principal and pool-exhaustion branches without a real identity provider
// or a real quota source (amendment 1).
func newEnvFull(t *testing.T, cs httpapi.CatalogState, meteredAuthority httpapi.MeteredAuthority, entitlementAvailability routing.EntitlementAvailability) *env {
	t.Helper()
	e := &env{clock: time.Date(2026, 8, 21, 12, 0, 0, 0, time.UTC)}
	store := capacity.NewStore(e.now)

	callerAuth := auth.NewCallerAuth([]string{callerToken})
	nodeAuth := auth.NewNodeAuth(map[string]string{nodeToken: nodeName})

	cfg := httpapi.Config{Version: "test", HeartbeatInterval: 30 * time.Second, OfflineAfter: 90 * time.Second}
	srv := httpapi.NewServer(cfg, cs, store, callerAuth, nodeAuth, meteredAuthority, entitlementAvailability, nil)

	ts := httptest.NewServer(srv.Handler())
	t.Cleanup(ts.Close)
	e.server = ts
	return e
}

func (e *env) now() time.Time {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.clock
}

func (e *env) advance(d time.Duration) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.clock = e.clock.Add(d)
}

func (e *env) do(t *testing.T, method, path, token string, body []byte) *http.Response {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	} else {
		reader = bytes.NewReader(nil)
	}
	req, err := http.NewRequest(method, e.server.URL+path, reader)
	if err != nil {
		t.Fatalf("NewRequest: %v", err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	t.Cleanup(func() { _ = resp.Body.Close() })
	return resp
}

func decode[T any](t *testing.T, resp *http.Response) T {
	t.Helper()
	var v T
	if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
		t.Fatalf("decoding response body: %v", err)
	}
	return v
}

type errorResponse struct {
	Error struct {
		Code       string `json:"code"`
		Obligation string `json:"obligation"`
		HTTPStatus int    `json:"http_status"`
	} `json:"error"`
}

type ackResponse struct {
	Accepted                  bool   `json:"accepted"`
	Node                      string `json:"node"`
	ObservedState             string `json:"observed_state"`
	EligibleForPlacement      bool   `json:"eligible_for_placement"`
	NextHeartbeatAfterSeconds int    `json:"next_heartbeat_after_seconds"`
	OfflineAfterSeconds       int    `json:"offline_after_seconds"`
	CatalogVersion            string `json:"catalog_version"`
}

type statusPlacement struct {
	Name        string `json:"name"`
	State       string `json:"state"`
	StateSource string `json:"state_source"`
	Readiness   string `json:"readiness"`
	Eligible    bool   `json:"eligible"`
}

type statusProfilePhysical struct {
	ModelID    string `json:"model_id"`
	Eligible   bool   `json:"eligible"`
	ReasonCode string `json:"reason_code"`
}

type statusProfile struct {
	Name         string                  `json:"name"`
	Selectable   bool                    `json:"selectable"`
	Capabilities []string                `json:"capabilities"`
	MinContext   *int                    `json:"min_context"`
	Physical     []statusProfilePhysical `json:"physical"`
}

type statusResp struct {
	CatalogVersion string `json:"catalog_version"`
	Router         struct {
		CapacityState string `json:"capacity_state"`
	} `json:"router"`
	Placements []statusPlacement `json:"placements"`
	Profiles   []statusProfile   `json:"profiles"`
}

func findPlacement(t *testing.T, s statusResp, name string) statusPlacement {
	t.Helper()
	for _, p := range s.Placements {
		if p.Name == name {
			return p
		}
	}
	t.Fatalf("placement %q not found in status response", name)
	return statusPlacement{}
}

func findProfile(t *testing.T, s statusResp, name string) statusProfile {
	t.Helper()
	for _, p := range s.Profiles {
		if p.Name == name {
			return p
		}
	}
	t.Fatalf("profile %q not found in status response", name)
	return statusProfile{}
}

func heartbeatBody(node, state string, activeModel *string, cachedModels []string) []byte {
	if cachedModels == nil {
		cachedModels = []string{}
	}
	body := map[string]any{
		"node":  node,
		"state": state,
		"gpu": map[string]any{
			"vendor": "amd", "model": "RX 7900 XTX", "arch": "gfx1100",
			"vram_total_gb": 24, "vram_free_gb": 21.9, "utilization_pct": 3,
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

// heartbeatBodyUnreachable is heartbeatBody with cluster_reachable forced to
// false, for TestNarrowingApplies_ClusterUnreachable: the one field that
// test needs to vary independently of state.
func heartbeatBodyUnreachable(node, state string) []byte {
	body := map[string]any{
		"node":  node,
		"state": state,
		"gpu": map[string]any{
			"vendor": "amd", "model": "RX 7900 XTX", "arch": "gfx1100",
			"vram_total_gb": 24, "vram_free_gb": 21.9, "utilization_pct": 3,
		},
		"runtime": map[string]any{
			"kind": "llama-swap+llama.cpp", "version": "1.0", "endpoint": "https://edge:8443",
		},
		"active_model":      nil,
		"cached_models":     []string{},
		"preemptible":       true,
		"interactive":       state == "INTERACTIVE",
		"ac_power":          true,
		"cluster_reachable": false,
		"last_heartbeat":    "2026-08-21T12:00:00Z",
		"capabilities":      []string{"chat", "tools"},
		"max_context":       65536,
	}
	raw, _ := json.Marshal(body)
	return raw
}

func strp(s string) *string { return &s }

// --- Auth ---------------------------------------------------------------

func TestStatus_Unauthenticated(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := e.do(t, http.MethodGet, "/v1/status", "", nil)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "unauthenticated" {
		t.Errorf("code = %q, want unauthenticated", body.Error.Code)
	}
}

func TestHeartbeat_Unauthenticated(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", "", heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "unauthenticated" {
		t.Errorf("code = %q, want unauthenticated", body.Error.Code)
	}
}

// TestBearerScheme_CaseInsensitive: RFC 7235 makes the auth-scheme token
// case-insensitive, so "bearer" and "BEARER" must be accepted exactly like
// "Bearer" - only the scheme is case-insensitive, not the credential.
func TestBearerScheme_CaseInsensitive(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	for _, scheme := range []string{"bearer", "Bearer", "BEARER"} {
		t.Run(scheme, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodGet, e.server.URL+"/v1/status", nil)
			if err != nil {
				t.Fatalf("NewRequest: %v", err)
			}
			req.Header.Set("Authorization", scheme+" "+callerToken)
			resp, err := http.DefaultClient.Do(req)
			if err != nil {
				t.Fatalf("Do: %v", err)
			}
			defer resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				t.Errorf("scheme %q: status = %d, want 200", scheme, resp.StatusCode)
			}
		})
	}
}

// --- Heartbeat accepted ---------------------------------------------------

func TestHeartbeat_Accepted(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}))
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", resp.StatusCode)
	}
	ack := decode[ackResponse](t, resp)
	if !ack.Accepted || ack.Node != nodeName || ack.ObservedState != "AVAILABLE" || !ack.EligibleForPlacement {
		t.Errorf("unexpected ack: %+v", ack)
	}
	if ack.NextHeartbeatAfterSeconds != 30 || ack.OfflineAfterSeconds != 90 {
		t.Errorf("unexpected policy fields: %+v", ack)
	}
}

func TestHeartbeat_MalformedBody(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, []byte("{not valid json"))
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "invalid_request" {
		t.Errorf("code = %q, want invalid_request", body.Error.Code)
	}
}

// --- Node identity --------------------------------------------------------

func TestHeartbeat_NodeIdentityMismatch(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	// Credential is bound to cachyos-7900xtx; payload claims a different,
	// real placement name.
	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody("bazzite-5090", "AVAILABLE", nil, nil))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "node_identity_mismatch" {
		t.Errorf("code = %q, want node_identity_mismatch", body.Error.Code)
	}
}

func TestHeartbeat_UnknownNode(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	// Same valid credential, but the payload names a node that is not any
	// placement in the catalog at all.
	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody("totally-unknown-node-xyz", "AVAILABLE", nil, nil))
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("status = %d, want 403", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "node_identity_mismatch" {
		t.Errorf("code = %q, want node_identity_mismatch", body.Error.Code)
	}
}

// --- Stale -> OFFLINE, restart re-learns, narrowing/expanding -------------

func TestStatus_StaleGoesOffline(t *testing.T) {
	e := newEnv(t, realCatalogState(t))

	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("heartbeat status = %d, want 202", resp.StatusCode)
	}

	statusResp1 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, statusResp1, nodeName)
	if p.StateSource != "heartbeat" || !p.Eligible {
		t.Fatalf("immediately after heartbeat: %+v, want heartbeat-sourced and eligible", p)
	}

	e.advance(91 * time.Second)

	statusResp2 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p = findPlacement(t, statusResp2, nodeName)
	if p.StateSource != "silence" {
		t.Errorf("StateSource = %q, want silence after 91s of silence", p.StateSource)
	}
	if p.State != "OFFLINE" {
		t.Errorf("State = %q, want OFFLINE", p.State)
	}
	if p.Eligible {
		t.Error("Eligible = true, want false once stale")
	}
}

func TestStatus_RestartRelearns(t *testing.T) {
	cs := realCatalogState(t)

	e1 := newEnv(t, cs)
	e1.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	s1 := decode[statusResp](t, e1.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	if findPlacement(t, s1, nodeName).StateSource != "heartbeat" {
		t.Fatal("expected heartbeat-sourced state before simulated restart")
	}

	// A fresh Server (and therefore a fresh capacity.Store) represents a
	// router restart: capacity is in-memory only, so nothing here should
	// carry state from e1 forward.
	e2 := newEnv(t, cs)
	s2 := decode[statusResp](t, e2.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, s2, nodeName)
	if p.StateSource != "unseen" {
		t.Errorf("post-restart StateSource = %q, want unseen", p.StateSource)
	}
	if s2.Router.CapacityState != "learning" {
		t.Errorf("post-restart capacity_state = %q, want learning", s2.Router.CapacityState)
	}
}

// TestNarrowingApplies: a heartbeat reporting INTERACTIVE narrows the
// placement's eligibility to false even though the catalog marks it
// selectable and available - narrowing takes effect immediately.
func TestNarrowingApplies(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "INTERACTIVE", nil, nil))

	s := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, s, nodeName)
	if p.Eligible {
		t.Error("Eligible = true, want false: an INTERACTIVE heartbeat must narrow eligibility immediately")
	}
	if p.State != "INTERACTIVE" {
		t.Errorf("State = %q, want INTERACTIVE", p.State)
	}
}

// TestNarrowingApplies_ClusterUnreachable: a node reporting AVAILABLE but
// cluster_reachable: false is narrowing itself, same as INTERACTIVE or
// DRAINING - heartbeat.schema.json says the router SHOULD treat it as
// ineligible until a heartbeat reports true again.
func TestNarrowingApplies_ClusterUnreachable(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBodyUnreachable(nodeName, "AVAILABLE"))

	s := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, s, nodeName)
	if p.Eligible {
		t.Error("Eligible = true, want false: cluster_reachable=false must narrow eligibility even while state is AVAILABLE")
	}

	// Contrast: the same state with cluster_reachable=true is eligible. This
	// is the base case the narrowing is defined relative to.
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	s2 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	if !findPlacement(t, s2, nodeName).Eligible {
		t.Error("Eligible = false, want true once cluster_reachable is true again with state AVAILABLE")
	}
}

// TestExpandingRejected: a heartbeat claiming an active_model the catalog
// does not authorize for this placement must never be treated as warm -
// the router may not let a node grant itself capability the Git catalog
// does not already give it.
func TestExpandingRejected(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
		heartbeatBody(nodeName, "AVAILABLE", strp("a-model-the-catalog-never-placed-here"), nil))

	s := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, s, nodeName)
	if p.Readiness == "warm" {
		t.Error("Readiness = warm, want anything but warm: an unauthorized active_model claim must be ignored for eligibility")
	}
	if p.Readiness != "absent" {
		t.Errorf("Readiness = %q, want absent (no authorized model claimed)", p.Readiness)
	}

	// Contrast: a catalog-authorized model claim IS honoured as warm. This
	// is the base case narrowing/expansion is defined relative to.
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
		heartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}))
	s2 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	if got := findPlacement(t, s2, nodeName).Readiness; got != "warm" {
		t.Errorf("Readiness = %q, want warm for a catalog-authorized active_model", got)
	}
}

// TestStatus_OfflineNeverReportsWarm: once a placement's state is OFFLINE
// (here via silence), readiness must not claim warm or cached even though
// the retained heartbeat still lists an authorized active_model - retaining
// the heartbeat for diagnostics is intentional, but an OFFLINE node is not
// servable now and readiness must not assert otherwise.
func TestStatus_OfflineNeverReportsWarm(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken,
		heartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}))

	s1 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	if got := findPlacement(t, s1, nodeName).Readiness; got != "warm" {
		t.Fatalf("Readiness = %q before going offline, want warm", got)
	}

	e.advance(91 * time.Second)

	s2 := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))
	p := findPlacement(t, s2, nodeName)
	if p.State != "OFFLINE" {
		t.Fatalf("State = %q, want OFFLINE after silence", p.State)
	}
	if p.Readiness == "warm" || p.Readiness == "cached" {
		t.Errorf("Readiness = %q, want neither warm nor cached for an OFFLINE placement", p.Readiness)
	}
}

// --- Catalog failure modes -------------------------------------------------

func TestCatalogUnavailable(t *testing.T) {
	e := newEnv(t, httpapi.CatalogState{Err: errors.New("boom")})

	resp := e.do(t, http.MethodGet, "/v1/status", callerToken, nil)
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status endpoint: status = %d, want 503", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "catalog_unavailable" || body.Error.Obligation != "retry" {
		t.Errorf("status endpoint: %+v, want catalog_unavailable/retry", body.Error)
	}

	resp2 := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	if resp2.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("heartbeat endpoint: status = %d, want 503", resp2.StatusCode)
	}
	body2 := decode[errorResponse](t, resp2)
	if body2.Error.Code != "catalog_unavailable" {
		t.Errorf("heartbeat endpoint: code = %q, want catalog_unavailable", body2.Error.Code)
	}
}

// TestCatalogSchemaUnsupported checks the asymmetric rule: GET /v1/status
// reports the PERMANENT catalog_schema_unsupported, but POST
// /v1/capacity/heartbeat reports only the TRANSIENT catalog_unavailable for
// the exact same underlying failure, because an abort obligation aimed at
// an edge node means "stop reporting for good" and this condition is not
// the node's to fix.
func TestCatalogSchemaUnsupported(t *testing.T) {
	cs := httpapi.CatalogState{Err: &catalog.UnsupportedSchemaError{Received: 2, Supported: 1}}
	e := newEnv(t, cs)

	resp := e.do(t, http.MethodGet, "/v1/status", callerToken, nil)
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status endpoint: status = %d, want 503", resp.StatusCode)
	}
	body := decode[errorResponse](t, resp)
	if body.Error.Code != "catalog_schema_unsupported" || body.Error.Obligation != "abort" {
		t.Errorf("status endpoint: %+v, want catalog_schema_unsupported/abort", body.Error)
	}

	resp2 := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", nil, nil))
	if resp2.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("heartbeat endpoint: status = %d, want 503", resp2.StatusCode)
	}
	body2 := decode[errorResponse](t, resp2)
	if body2.Error.Code != "catalog_unavailable" {
		t.Errorf("heartbeat endpoint: code = %q, want catalog_unavailable (never catalog_schema_unsupported)", body2.Error.Code)
	}
	if body2.Error.Obligation != "retry" {
		t.Errorf("heartbeat endpoint: obligation = %q, want retry", body2.Error.Obligation)
	}
}

// --- Non-selectable profile rendering --------------------------------------

func TestStatus_NonSelectableProfileRendering(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	s := decode[statusResp](t, e.do(t, http.MethodGet, "/v1/status", callerToken, nil))

	for _, name := range []string{"local-code-fast", "local-unrestricted"} {
		p := findProfile(t, s, name)
		if p.Selectable {
			t.Errorf("profile %q: selectable = true, want false", name)
		}
		if len(p.Physical) != 0 {
			t.Errorf("profile %q: physical = %v, want empty", name, p.Physical)
		}
		if len(p.Capabilities) != 0 {
			t.Errorf("profile %q: capabilities = %v, want empty", name, p.Capabilities)
		}
		if p.MinContext != nil {
			t.Errorf("profile %q: min_context = %v, want null", name, *p.MinContext)
		}
	}

	standard := findProfile(t, s, "local-code-standard")
	if !standard.Selectable || len(standard.Physical) == 0 {
		t.Errorf("profile local-code-standard: %+v, want selectable with candidates", standard)
	}

	// minimax/strong: selectable false at the profile level, but its
	// physical[] candidate must say so explicitly via reason_code rather
	// than being silently omitted.
	minimax := findProfile(t, s, "minimax/strong")
	if minimax.Selectable {
		t.Error("profile minimax/strong: selectable = true, want false")
	}
	if len(minimax.Physical) != 1 || minimax.Physical[0].Eligible || minimax.Physical[0].ReasonCode != "not_selectable" {
		t.Errorf("profile minimax/strong: physical = %+v, want one ineligible candidate reason not_selectable", minimax.Physical)
	}
}

// --- /health, /ready --------------------------------------------------------

func TestHealthAndReady(t *testing.T) {
	e := newEnv(t, realCatalogState(t))
	if resp := e.do(t, http.MethodGet, "/health", "", nil); resp.StatusCode != http.StatusOK {
		t.Errorf("/health status = %d, want 200", resp.StatusCode)
	}
	if resp := e.do(t, http.MethodGet, "/ready", "", nil); resp.StatusCode != http.StatusOK {
		t.Errorf("/ready status = %d, want 200 with a loaded catalog", resp.StatusCode)
	}

	bad := newEnv(t, httpapi.CatalogState{Err: errors.New("boom")})
	if resp := bad.do(t, http.MethodGet, "/ready", "", nil); resp.StatusCode != http.StatusServiceUnavailable {
		t.Errorf("/ready status = %d, want 503 with no catalog loaded", resp.StatusCode)
	}
	if resp := bad.do(t, http.MethodGet, "/health", "", nil); resp.StatusCode != http.StatusOK {
		t.Errorf("/health status = %d, want 200 even with no catalog loaded (liveness only)", resp.StatusCode)
	}
}

// --- Contract conformance ---------------------------------------------------

// TestStatus_SchemaConformance validates a real /v1/status response against
// the committed JSON Schema, not just against this package's own DTOs - a
// test suite that only checks the code against itself does not prove
// contract conformance.
func TestStatus_SchemaConformance(t *testing.T) {
	schema := testutil.CompileSchema(t, "status.schema.json")
	e := newEnv(t, realCatalogState(t))

	resp := e.do(t, http.MethodGet, "/v1/status", callerToken, nil)
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	raw := readAll(t, resp)
	testutil.ValidateJSON(t, schema, raw)

	// And again after a heartbeat has been recorded, so the heartbeat
	// re-exposure path is validated too.
	e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, heartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"}))
	resp2 := e.do(t, http.MethodGet, "/v1/status", callerToken, nil)
	raw2 := readAll(t, resp2)
	testutil.ValidateJSON(t, schema, raw2)
}

// TestHeartbeatRequest_SchemaConformance proves the request bodies these
// tests send are themselves valid heartbeat.schema.json instances, so a
// heartbeat test accepted by the router is also a heartbeat the frozen
// contract accepts.
func TestHeartbeatRequest_SchemaConformance(t *testing.T) {
	schema := testutil.CompileSchema(t, "heartbeat.schema.json")

	e := newEnv(t, realCatalogState(t))
	body := heartbeatBody(nodeName, "AVAILABLE", strp("qwen36-27b"), []string{"qwen36-27b"})
	testutil.ValidateJSON(t, schema, body)

	resp := e.do(t, http.MethodPost, "/v1/capacity/heartbeat", nodeToken, body)
	if resp.StatusCode != http.StatusAccepted {
		t.Fatalf("status = %d, want 202", resp.StatusCode)
	}
}

func readAll(t *testing.T, resp *http.Response) []byte {
	t.Helper()
	defer resp.Body.Close()
	buf := new(bytes.Buffer)
	if _, err := buf.ReadFrom(resp.Body); err != nil {
		t.Fatalf("reading response body: %v", err)
	}
	return buf.Bytes()
}
