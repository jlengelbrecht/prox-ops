package httpapi

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/placement"
)

// maxPlaceBodyBytes bounds how much of a /v1/place request body this
// service will read - the payload is a handful of fields, well under a KB in
// practice.
const maxPlaceBodyBytes = 64 * 1024

// placeTTLSeconds is the frozen MVP validity window (owner clarification 4):
// pinned at 30 for every /v1/place response, placed or unavailable alike.
const placeTTLSeconds = 30

var validModelProfile = map[string]bool{
	"local-code-fast":     true,
	"local-code-standard": true,
	"local-general":       true,
	"local-unrestricted":  true,
	"claude/strong":       true,
	"openai/strong":       true,
	"devin/free":          true,
	"minimax/strong":      true,
}

var validPlacementName = map[string]bool{
	"kserve-a5000":    true,
	"cachyos-7900xtx": true,
	"bazzite-5090":    true,
	"laptop-rtx5000":  true,
}

// handlePlace implements POST /v1/place (contracts/agent-router/openapi.yaml).
// Side-effect free and safe to retry: it never probes a placement and never
// wakes a scale-to-zero KServe model (invariant 7).
func (s *Server) handlePlace(w http.ResponseWriter, r *http.Request) {
	token := bearerToken(r)
	if !s.callerAuth.Valid(token) {
		writeError(w, http.StatusUnauthorized, "unauthenticated",
			"Missing or invalid bearer credential.", "abort", "/v1/place", nil, nil, nil)
		return
	}

	if s.catalog.Err != nil {
		s.writeCatalogError(w, "/v1/place", true)
		return
	}
	cat := s.catalog.Catalog
	digest := s.catalog.Digest

	body, err := io.ReadAll(io.LimitReader(r.Body, maxPlaceBodyBytes+1))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid_request", "could not read request body", "abort", "/v1/place", nil, nil, nil)
		return
	}
	if len(body) > maxPlaceBodyBytes {
		writeError(w, http.StatusBadRequest, "invalid_request", "request body too large", "abort", "/v1/place", nil, nil, nil)
		return
	}

	req, verr := parsePlaceRequest(body)
	if verr != nil {
		var details map[string]any
		if verr.Field != "" {
			details = map[string]any{"field": verr.Field}
		}
		writeError(w, http.StatusBadRequest, "invalid_request", verr.Message, "abort", "/v1/place", details, nil, nil)
		return
	}

	if req.CatalogVersion != nil && *req.CatalogVersion != digest {
		writeError(w, http.StatusConflict, "catalog_version_stale",
			"The catalog moved between planning and this call. The stamped profile was decided against a catalog that is no longer loaded, so it cannot be resolved as if nothing had changed. Call /v1/route again: re-planning is a new attempt, because a stamped profile is immutable for the attempt it belongs to (invariant 2).",
			"re-plan", "/v1/place",
			map[string]any{
				"received_catalog_version": *req.CatalogVersion,
				"expected_catalog_version": digest,
			}, nil, &digest)
		return
	}

	if !profileExists(cat, req.ModelProfile) {
		writeError(w, http.StatusNotFound, "unknown_profile",
			fmt.Sprintf("model_profile %q is a name this contract's vocabulary still carries, but the loaded catalog no longer defines it. Re-plan: call /v1/route again against the catalog that is actually loaded.", req.ModelProfile),
			"re-plan", "/v1/place",
			map[string]any{"requested": req.ModelProfile, "known": profileNames(cat)}, nil, &digest)
		return
	}
	if !policyExists(cat, req.PlacementPolicy) {
		writeError(w, http.StatusNotFound, "unknown_placement_policy",
			fmt.Sprintf("placement_policy %q was stamped on this attempt but the loaded catalog no longer defines it.", req.PlacementPolicy),
			"re-plan", "/v1/place",
			map[string]any{"requested": req.PlacementPolicy, "known": policyNames(cat)}, nil, &digest)
		return
	}

	// Facts are per-(placement, model): the profile's physical[] names which
	// model each placement would serve, and readiness is asserted against
	// exactly that model (security review, cycle 1 - a different model being
	// warm on the same placement must not claim "no load step").
	modelFor := map[string]string{}
	for _, e := range cat.Profiles {
		if e.Name != req.ModelProfile {
			continue
		}
		for _, ph := range e.Value.Physical {
			if ph.Placement != nil {
				modelFor[*ph.Placement] = ph.ModelID
			}
		}
	}
	facts := resolvePlaceFacts(cat, s.store, s.cfg, modelFor, s.logger)
	result := placement.Decide(cat, facts, placement.Input{
		ModelProfile:    req.ModelProfile,
		PlacementPolicy: req.PlacementPolicy,
		MinFreeVramGB:   req.MinFreeVramGB,
		Exclude:         req.Exclude,
	})

	wire := buildPlaceResultWire(result, digest)
	writeJSON(w, http.StatusOK, wire)
}

func profileExists(cat *catalog.Catalog, name string) bool {
	for _, e := range cat.Profiles {
		if e.Name == name {
			return true
		}
	}
	return false
}

func policyExists(cat *catalog.Catalog, name string) bool {
	for _, e := range cat.Policies {
		if e.Name == name {
			return true
		}
	}
	return false
}

func profileNames(cat *catalog.Catalog) []string {
	out := make([]string, 0, len(cat.Profiles))
	for _, e := range cat.Profiles {
		out = append(out, e.Name)
	}
	return out
}

func policyNames(cat *catalog.Catalog) []string {
	out := make([]string, 0, len(cat.Policies))
	for _, e := range cat.Policies {
		out = append(out, e.Name)
	}
	return out
}

// parsePlaceRequest strictly decodes and validates a PlaceRequest body
// against contracts/agent-router/openapi.yaml components.schemas.PlaceRequest.
func parsePlaceRequest(body []byte) (*placeRequestWire, *invalidRequestError) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var req placeRequestWire
	if err := dec.Decode(&req); err != nil {
		var typeErr *json.UnmarshalTypeError
		if errors.As(err, &typeErr) {
			return nil, &invalidRequestError{Field: typeErr.Field, Message: fmt.Sprintf("field %q has the wrong type: %v", typeErr.Field, err)}
		}
		if strings.Contains(err.Error(), "unknown field") {
			field := strings.TrimSuffix(strings.TrimPrefix(err.Error(), `json: unknown field "`), `"`)
			return nil, &invalidRequestError{Field: field, Message: fmt.Sprintf("unknown field %q: PlaceRequest is closed and does not accept it.", field)}
		}
		return nil, &invalidRequestError{Message: fmt.Sprintf("could not parse request body: %v", err)}
	}
	var trailing json.RawMessage
	if err := dec.Decode(&trailing); err != io.EOF {
		return nil, &invalidRequestError{Message: "request body contains trailing data after the JSON object"}
	}

	if !validModelProfile[req.ModelProfile] {
		return nil, &invalidRequestError{Field: "model_profile", Message: fmt.Sprintf("model_profile must be one of the catalog vocabulary's closed enum. Received %q.", req.ModelProfile)}
	}
	if !validPlacementPolicy[req.PlacementPolicy] {
		return nil, &invalidRequestError{Field: "placement_policy", Message: fmt.Sprintf("placement_policy must be one of prefer-warm-local, cluster-only, edge-only, any-24gb. Received %q.", req.PlacementPolicy)}
	}
	if req.MinFreeVramGB != nil && *req.MinFreeVramGB < 0 {
		return nil, &invalidRequestError{Field: "min_free_vram_gb", Message: "min_free_vram_gb must be >= 0"}
	}
	if req.EstimatedRequestBytes != nil && *req.EstimatedRequestBytes < 0 {
		return nil, &invalidRequestError{Field: "estimated_request_bytes", Message: "estimated_request_bytes must be >= 0"}
	}
	seen := make(map[string]bool, len(req.Exclude))
	for _, name := range req.Exclude {
		if !validPlacementName[name] {
			return nil, &invalidRequestError{Field: "exclude", Message: fmt.Sprintf("exclude entries must be a known placement name. Received %q.", name)}
		}
		if seen[name] {
			return nil, &invalidRequestError{Field: "exclude", Message: "exclude must not contain duplicate entries"}
		}
		seen[name] = true
	}
	if req.CatalogVersion != nil && !catalogVersionPattern.MatchString(*req.CatalogVersion) {
		return nil, &invalidRequestError{Field: "catalog_version", Message: "catalog_version must match ^sha256:[0-9a-f]{64}$"}
	}
	return &req, nil
}

// resolvePlaceFacts derives every placement's placement.Facts from the
// loaded catalog and live capacity store - the same hard-eligibility and
// readiness rules buildStatus renders, reused here rather than re-derived,
// so /v1/status and /v1/place can never quietly disagree about what a
// placement's live state is.
func resolvePlaceFacts(cat *catalog.Catalog, store *capacity.Store, cfg Config, modelFor map[string]string, logger *slog.Logger) map[string]placement.Facts {
	out := make(map[string]placement.Facts, len(cat.Placements))
	for _, entry := range cat.Placements {
		name := entry.Name
		p := entry.Value
		f := placement.Facts{MeasuredVramGB: p.Capacity.VramGB}

		switch {
		case p.Kind == "kserve":
			// Static: never probed (invariant 7). Readiness stays unknown
			// forever, but the placement's own provisional estimate still
			// travels with it (owner clarification 2).
			f.Readiness = placement.ReadinessUnknown
			f.EstimatedColdStartS = p.ColdStartSEstimate
			if p.Status == "available" && p.Selectable {
				f.HardEligible = true
			} else {
				f.HardReasonCode = "not_selectable"
			}
		case p.Status == "planned":
			f.Readiness = placement.ReadinessUnknown
			f.HardReasonCode = "not_selectable"
		default:
			res := store.Resolve(name, cfg.OfflineAfter)
			f.HardEligible = res.Eligible && p.Selectable
			if !f.HardEligible {
				f.HardReasonCode = reasonCodeFor(placementResolution{
					eligible:  f.HardEligible,
					source:    string(res.Source),
					state:     res.State,
					heartbeat: res.Heartbeat,
				})
			}
			if res.Heartbeat != nil {
				f.FreeVramGB = res.Heartbeat.GPU.VRAMFreeGB
			}
			if res.State == heartbeat.StateOffline {
				// An OFFLINE placement (silence or self-reported) is not
				// servable now; readiness must not claim warm/cached for it
				// (same rule buildStatus applies).
				f.Readiness = placement.ReadinessUnknown
			} else {
				if mid, ok := modelFor[name]; ok {
					f.Readiness = computeReadinessForModel(cat, name, mid, res.Heartbeat, logger)
				} else {
					f.Readiness = computeReadiness(cat, name, res.Heartbeat, logger)
				}
				switch f.Readiness {
				case placement.ReadinessWarm:
					zero := 0.0
					f.EstimatedColdStartS = &zero
				case placement.ReadinessCached:
					f.EstimatedColdStartS = p.ColdStartSEstimate
				default:
					f.EstimatedColdStartS = nil
				}
			}
		}
		out[name] = f
	}
	return out
}

// buildPlaceResultWire renders a pure placement.Result into the wire
// PlaceResult shape (place-result.schema.json).
func buildPlaceResultWire(r placement.Result, digest string) placeResultWire {
	alternatives := make([]placeAlternativeWire, 0, len(r.Alternatives))
	for _, a := range r.Alternatives {
		alternatives = append(alternatives, placeAlternativeWire{
			Placement:           a.Placement,
			Model:               a.Model,
			Readiness:           a.Readiness,
			EstimatedColdStartS: a.EstimatedColdStartS,
			Eligible:            a.Eligible,
			Reason:              placeReasonWire{Code: a.ReasonCode, Message: a.ReasonMessage},
		})
	}

	headers := map[string]string{}
	if r.Status == "placed" {
		headers["x-placement"] = *r.Placement
	}

	return placeResultWire{
		Status:              r.Status,
		Model:               r.Model,
		Placement:           r.Placement,
		Readiness:           r.Readiness,
		EstimatedColdStartS: r.EstimatedColdStartS,
		Headers:             headers,
		TTLSeconds:          placeTTLSeconds,
		Reason:              placeReasonWire{Code: r.ReasonCode, Message: r.ReasonMessage},
		Alternatives:        alternatives,
		CatalogVersion:      digest,
	}
}
