// Package httpapi implements the agent-router HTTP surface: GET /v1/status,
// POST /v1/capacity/heartbeat, POST /v1/route (35.9a, 35.10) and
// POST /v1/place (35.11), plus the credential-free /health and /ready
// endpoints.
package httpapi

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/auth"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/routing"
)

// maxHeartbeatBodyBytes bounds how much of a heartbeat request body this
// service will read, so a misbehaving or hostile caller cannot exhaust
// memory through this endpoint. The payload is a few hundred bytes in
// practice; this is generous headroom, not a working limit.
const maxHeartbeatBodyBytes = 64 * 1024

// Config carries the router-side constants that are not catalog data:
// build version and the heartbeat interval policy. The interval and its
// 3x-derived offline threshold are router-side constants, not fields on
// the heartbeat payload itself (heartbeat.schema.json) - a node discovers
// them from the ack and from GET /v1/status rather than hard-coding them.
type Config struct {
	Version           string
	HeartbeatInterval time.Duration
	OfflineAfter      time.Duration
}

// CatalogState is the outcome of the one catalog load this router attempts
// at startup. Err is nil only when Catalog is usable. A *catalog.UnsupportedSchemaError
// is the PERMANENT case; any other error is the TRANSIENT "nothing usable
// loaded" case.
type CatalogState struct {
	Catalog *catalog.Catalog
	Digest  string
	Err     error
}

// Server holds everything the HTTP handlers need. The catalog is loaded
// once at startup and held fixed for this story's scope; there is no
// hot-reload requirement here.
type Server struct {
	cfg                     Config
	catalog                 CatalogState
	store                   *capacity.Store
	callerAuth              *auth.CallerAuth
	nodeAuth                *auth.NodeAuth
	meteredAuthority        MeteredAuthority
	entitlementAvailability routing.EntitlementAvailability
	logger                  *slog.Logger
}

// NewServer builds a Server. logger defaults to slog.Default() when nil.
// meteredAuthority defaults to DenyAllMeteredAuthority (production posture:
// no caller today independently holds metered-spend authority) when nil.
// entitlementAvailability defaults to routing.AlwaysAvailable (production
// has no live signal for entitlement exhaustion) when nil.
func NewServer(cfg Config, cs CatalogState, store *capacity.Store, callerAuth *auth.CallerAuth, nodeAuth *auth.NodeAuth, meteredAuthority MeteredAuthority, entitlementAvailability routing.EntitlementAvailability, logger *slog.Logger) *Server {
	if logger == nil {
		logger = slog.Default()
	}
	if meteredAuthority == nil {
		meteredAuthority = DenyAllMeteredAuthority{}
	}
	if entitlementAvailability == nil {
		entitlementAvailability = routing.AlwaysAvailable{}
	}
	return &Server{
		cfg:                     cfg,
		catalog:                 cs,
		store:                   store,
		callerAuth:              callerAuth,
		nodeAuth:                nodeAuth,
		meteredAuthority:        meteredAuthority,
		entitlementAvailability: entitlementAvailability,
		logger:                  logger,
	}
}

// Handler builds the request router. Listen port 8080 is fixed by the
// contract and is the caller's responsibility (cmd/agent-router), not this
// package's.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", s.handleHealth)
	mux.HandleFunc("GET /ready", s.handleReady)
	mux.HandleFunc("GET /v1/status", s.handleStatus)
	mux.HandleFunc("POST /v1/capacity/heartbeat", s.handleHeartbeat)
	mux.HandleFunc("POST /v1/route", s.handleRoute)
	mux.HandleFunc("POST /v1/place", s.handlePlace)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"status":  "ok",
		"version": s.cfg.Version,
	})
}

// handleReady is honest: not ready until the catalog has actually loaded
// (story acceptance). It exposes nothing beyond a coarse status, since
// /ready is credential-free like /health.
func (s *Server) handleReady(w http.ResponseWriter, _ *http.Request) {
	if s.catalog.Err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not-ready"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	if !s.callerAuth.Valid(bearerToken(r)) {
		writeError(w, http.StatusUnauthorized, "unauthenticated",
			"Missing or invalid bearer credential.", "abort", "/v1/status", nil, nil, nil)
		return
	}

	if s.catalog.Err != nil {
		s.writeCatalogError(w, "/v1/status", true)
		return
	}

	resp := buildStatus(s.catalog.Catalog, s.catalog.Digest, s.store, s.cfg, s.logger, s.store.Now())
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleHeartbeat(w http.ResponseWriter, r *http.Request) {
	token := bearerToken(r)
	node, ok := s.nodeAuth.NodeFor(token)
	if !ok {
		writeError(w, http.StatusUnauthorized, "unauthenticated",
			"Missing or invalid bearer credential. Heartbeats are authenticated per node; a rejected credential is never treated as a heartbeat from an unknown node.",
			"abort", "/v1/capacity/heartbeat", nil, nil, nil)
		return
	}

	body, err := io.ReadAll(io.LimitReader(r.Body, maxHeartbeatBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "could not read request body", "abort", "/v1/capacity/heartbeat", nil, nil, nil)
		return
	}
	if len(body) > maxHeartbeatBodyBytes {
		writeError(w, http.StatusBadRequest, "invalid_request", "request body too large", "abort", "/v1/capacity/heartbeat", nil, nil, nil)
		return
	}

	hb, err := heartbeat.Parse(body)
	if err != nil {
		var ierr *heartbeat.InvalidError
		msg := "The request body is malformed or a field is out of range."
		var details map[string]any
		if errors.As(err, &ierr) {
			msg = ierr.Message
			details = map[string]any{"field": ierr.Field}
		}
		writeError(w, http.StatusBadRequest, "invalid_request", msg, "abort", "/v1/capacity/heartbeat", details, nil, nil)
		return
	}

	// A valid credential presenting a different node id than the one it is
	// bound to is rejected outright, whether that named node is some other
	// real placement or not a placement at all - either way the credential
	// cannot speak for it.
	if hb.Node != node {
		writeError(w, http.StatusForbidden, "node_identity_mismatch",
			"The bearer credential is valid but is not bound to the node named in the payload. The heartbeat was discarded and no capacity state was updated.",
			"abort", "/v1/capacity/heartbeat", map[string]any{"node": hb.Node}, nil, nil)
		return
	}

	// Recording a self-reported state needs no catalog (README "the loop
	// terminates only on a failure the node itself has to fix"), so this
	// happens before the catalog check below and unconditionally on a
	// valid, identity-matched heartbeat.
	s.store.Record(*hb)

	if s.catalog.Err != nil {
		// This endpoint returns only the TRANSIENT catalog_unavailable and
		// NEVER catalog_schema_unsupported: an abort obligation aimed at an
		// edge node means "stop reporting", and a node that obeyed it would
		// stay invisible after an operator had fixed the router.
		s.writeCatalogError(w, "/v1/capacity/heartbeat", false)
		return
	}

	ack := heartbeatAck{
		Accepted:                  true,
		Node:                      hb.Node,
		ObservedState:             hb.State,
		EligibleForPlacement:      hb.State == heartbeat.StateAvailable || hb.State == heartbeat.StateServing,
		NextHeartbeatAfterSeconds: int(s.cfg.HeartbeatInterval.Seconds()),
		OfflineAfterSeconds:       int(s.cfg.OfflineAfter.Seconds()),
		CatalogVersion:            s.catalog.Digest,
	}
	writeJSON(w, http.StatusAccepted, ack)
}

// writeCatalogError renders the current catalog load failure.
// allowSchemaUnsupported selects whether a schema-version failure is
// reported as the permanent catalog_schema_unsupported (GET /v1/status) or
// downgraded to the transient catalog_unavailable (the heartbeat endpoint,
// which must never hand an edge node a permanent-abort obligation over a
// condition on the router's own side of the wire).
func (s *Server) writeCatalogError(w http.ResponseWriter, endpoint string, allowSchemaUnsupported bool) {
	var unsupported *catalog.UnsupportedSchemaError
	if allowSchemaUnsupported && errors.As(s.catalog.Err, &unsupported) {
		writeError(w, http.StatusServiceUnavailable, "catalog_schema_unsupported",
			fmt.Sprintf("The mounted catalog declares schema_version %d and this router implements %d. It refuses the document rather than best-effort parsing a shape it does not understand.", unsupported.Received, unsupported.Supported),
			"abort", endpoint,
			map[string]any{
				"received_catalog_schema_version":  unsupported.Received,
				"supported_catalog_schema_version": unsupported.Supported,
			}, nil, nil)
		return
	}
	retry := 5
	writeError(w, http.StatusServiceUnavailable, "catalog_unavailable",
		"No catalog is loaded yet. The router has started but has not finished reading the mounted document.",
		"retry", endpoint, nil, &retry, nil)
}

// bearerToken extracts the credential from an Authorization header. RFC
// 7235 makes the auth-scheme token case-insensitive, so "bearer", "Bearer"
// and "BEARER" are all accepted; the credential itself is matched
// case-sensitively by the auth package.
func bearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	scheme, credential, found := strings.Cut(h, " ")
	if !found || !strings.EqualFold(scheme, "Bearer") {
		return ""
	}
	return strings.TrimSpace(credential)
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, code, message, obligation, endpoint string, details map[string]any, retryAfterSeconds *int, catalogVersion *string) {
	w.Header().Set("Content-Type", "application/json")
	if retryAfterSeconds != nil {
		w.Header().Set("Retry-After", strconv.Itoa(*retryAfterSeconds))
	}
	w.WriteHeader(status)
	body := errorBody{Error: errorDetail{
		Code:              code,
		Message:           message,
		Obligation:        obligation,
		HTTPStatus:        status,
		Endpoint:          endpoint,
		Details:           details,
		RetryAfterSeconds: retryAfterSeconds,
		CatalogVersion:    catalogVersion,
	}}
	_ = json.NewEncoder(w).Encode(body)
}
