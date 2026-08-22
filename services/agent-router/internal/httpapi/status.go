package httpapi

import (
	"log/slog"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
)

// placementResolution is the per-placement outcome buildStatus computes
// once and profiles[].physical[] then joins against - the same join
// contracts/agent-router/verify-digests.sh performs on the committed
// fixtures.
type placementResolution struct {
	eligible bool
	source   string
	state    string
}

// buildStatus renders the GET /v1/status response
// (contracts/agent-router/schemas/status.schema.json) from the loaded
// catalog and the live capacity store. now is threaded through explicitly
// so a fixed clock produces reproducible output in tests.
func buildStatus(cat *catalog.Catalog, digest string, store *capacity.Store, cfg Config, logger *slog.Logger, now time.Time) statusResponse {
	uptime := store.Uptime()
	capacityState := "steady"
	if uptime < cfg.HeartbeatInterval {
		capacityState = "learning"
	}

	resolved := make(map[string]placementResolution, len(cat.Placements))
	placements := make([]placementDTO, 0, len(cat.Placements))

	for _, entry := range cat.Placements {
		name := entry.Name
		p := entry.Value

		var state, source string
		var eligible bool
		var hb *heartbeat.Heartbeat
		var lastHeartbeat *string

		switch {
		case p.Kind == "kserve":
			// Static: a KServe placement has no heartbeat channel at all, so
			// its state comes from the catalog alone (status.schema.json
			// state_source: static). It is never probed (invariant 7).
			source = "static"
			if p.Status == "available" {
				state = heartbeat.StateAvailable
				eligible = p.Selectable
			} else {
				state = heartbeat.StateOffline
				eligible = false
			}
		case p.Status == "planned":
			// A reserved but not-yet-enrolled edge placement: no node exists
			// to report, so it is static too, and never eligible.
			source = "static"
			state = heartbeat.StateOffline
			eligible = false
		default:
			// An enrolled edge placement (kind: edge, status: available).
			// Its state is live: unseen, silence or heartbeat.
			res := store.Resolve(name, cfg.OfflineAfter)
			source = string(res.Source)
			state = res.State
			eligible = res.Eligible && p.Selectable
			hb = res.Heartbeat
			if res.LastHeartbeat != nil {
				s := res.LastHeartbeat.UTC().Format(time.RFC3339)
				lastHeartbeat = &s
			}
		}

		readiness := "unknown"
		if p.Kind == "edge" {
			readiness = computeReadiness(cat, name, hb, logger)
		}

		placements = append(placements, placementDTO{
			Name:               name,
			Kind:               p.Kind,
			Status:             p.Status,
			Selectable:         p.Selectable,
			State:              state,
			StateSource:        source,
			Readiness:          readiness,
			Eligible:           eligible,
			ColdStartSEstimate: p.ColdStartSEstimate,
			LastHeartbeat:      lastHeartbeat,
			Heartbeat:          hb,
		})
		resolved[name] = placementResolution{eligible: eligible, source: source, state: state}
	}

	profiles := make([]profileDTO, 0, len(cat.Profiles))
	for _, entry := range cat.Profiles {
		name := entry.Name
		pr := entry.Value

		capabilities := pr.Capabilities
		if capabilities == nil {
			capabilities = []string{}
		}
		forbidden := pr.ForbiddenFor
		if forbidden == nil {
			forbidden = []string{}
		}

		entitlements := make([]entitlementDTO, 0, len(pr.Entitlements))
		for _, e := range pr.Entitlements {
			entitlements = append(entitlements, entitlementDTO{Pool: e.Pool, CostClass: e.CostClass})
		}

		physical := make([]physicalDTO, 0, len(pr.Physical))
		for _, ph := range pr.Physical {
			item := physicalDTO{ModelID: ph.ModelID, Placement: ph.Placement}

			switch {
			case ph.Placement == nil:
				// Vendor candidate: no capacity dependency in this story's
				// scope, since vendor traffic never touches agentgateway.
				if pr.Selectable {
					item.Eligible = true
				} else {
					item.ReasonCode = "not_selectable"
				}
			default:
				res, ok := resolved[*ph.Placement]
				if !ok || !pr.Selectable {
					item.ReasonCode = "not_selectable"
					break
				}
				item.Eligible = res.eligible
				if !res.eligible {
					item.ReasonCode = reasonCodeFor(res)
				}
			}
			physical = append(physical, item)
		}

		profiles = append(profiles, profileDTO{
			Name:         name,
			CostClass:    pr.CostClass,
			Hosting:      pr.Hosting,
			Alignment:    pr.Alignment,
			Capabilities: capabilities,
			MinContext:   pr.MinContext,
			ForbiddenFor: forbidden,
			Selectable:   pr.Selectable,
			Entitlements: entitlements,
			Physical:     physical,
		})
	}

	harnesses := make([]harnessDTO, 0, len(cat.Harnesses))
	for _, entry := range cat.Harnesses {
		harnesses = append(harnesses, harnessDTO{
			Name:            entry.Name,
			Supported:       entry.Value.Supported,
			Selectable:      entry.Value.Selectable,
			RouterBehaviour: entry.Value.RouterBehaviour,
		})
	}

	pools := make([]poolDTO, 0, len(cat.EntitlementPools))
	for _, entry := range cat.EntitlementPools {
		var requiresAuth *bool
		if entry.Value.CostClass == "metered" {
			t := true
			requiresAuth = &t
		}
		pools = append(pools, poolDTO{
			Name:                  entry.Name,
			Provider:              entry.Value.Provider,
			CostClass:             entry.Value.CostClass,
			CredentialClass:       entry.Value.CredentialClass,
			Spillover:             entry.Value.Spillover,
			RequiresAuthorization: requiresAuth,
		})
	}

	policies := make([]policyDTO, 0, len(cat.Policies))
	for _, entry := range cat.Policies {
		po := entry.Value
		resolvesNow := false
		for _, pname := range po.PreferOrder {
			if r, ok := resolved[pname]; ok && r.eligible {
				resolvesNow = true
				break
			}
		}
		policies = append(policies, policyDTO{
			Name:               entry.Name,
			PreferOrder:        po.PreferOrder,
			WarmPreference:     po.WarmPreference,
			WarmBonusRankShift: po.WarmBonusRankShift,
			AllowColdStart:     po.AllowColdStart,
			MinVramGB:          po.MinVramGB,
			MinFreeVramGB:      po.MinFreeVramGB,
			ResolvesToday:      po.ResolvesToday,
			ResolvesNow:        resolvesNow,
		})
	}

	return statusResponse{
		CatalogVersion:         digest,
		CatalogDocumentVersion: cat.DocumentVersion,
		CatalogSchemaVersion:   cat.SchemaVersion,
		GeneratedAt:            now.UTC().Format(time.RFC3339),
		Router: routerInfo{
			Version:       cfg.Version,
			CapacityState: capacityState,
			UptimeSeconds: int64(uptime.Seconds()),
		},
		HeartbeatPolicy: heartbeatPolicy{
			IntervalSeconds:     int(cfg.HeartbeatInterval.Seconds()),
			OfflineAfterSeconds: int(cfg.OfflineAfter.Seconds()),
		},
		Harnesses:        harnesses,
		EntitlementPools: pools,
		Profiles:         profiles,
		Placements:       placements,
		Policies:         policies,
	}
}

// reasonCodeFor explains why an ineligible placement's candidate is
// ineligible, restricted to the placement-derived subset of
// place-result.schema.json candidate_ineligible_reason_code
// (constraint_unsatisfiable is a candidate-specific fact this story never
// computes, since it belongs to /v1/place's policy constraints).
func reasonCodeFor(res placementResolution) string {
	switch res.source {
	case "unseen":
		return "not_yet_observed"
	case "silence":
		return "offline"
	case "heartbeat":
		switch res.state {
		case heartbeat.StateInteractive:
			return "withdrawn_interactive"
		case heartbeat.StateDraining:
			return "withdrawn_draining"
		default:
			return "offline"
		}
	default: // "static": planned, or a catalog-ineligible kserve placement.
		return "not_selectable"
	}
}

// computeReadiness derives placements[].readiness from the most recent
// heartbeat, per owner ruling R11 and the narrowing rule: "active_model
// counts as warm only when that model is catalog-authorized for that
// placement... a claim with no catalog counterpart is a reconciliation
// alarm and is ignored for eligibility" (contracts/agent-router/openapi.yaml).
// A claim the catalog does not authorize is logged and never treated as
// warm or cached - this is where "a heartbeat may narrow static authority;
// it may never expand it" is enforced.
func computeReadiness(cat *catalog.Catalog, placementName string, hb *heartbeat.Heartbeat, logger *slog.Logger) string {
	if hb == nil {
		return "unknown"
	}
	if hb.ActiveModel != nil {
		if cat.ModelAuthorizedOnPlacement(*hb.ActiveModel, placementName) {
			return "warm"
		}
		logger.Warn("heartbeat claims an active_model the catalog does not authorize on this placement; ignored for eligibility",
			"placement", placementName, "active_model", *hb.ActiveModel)
	}
	for _, m := range hb.CachedModels {
		if cat.ModelAuthorizedOnPlacement(m, placementName) {
			return "cached"
		}
		logger.Warn("heartbeat claims a cached model the catalog does not authorize on this placement; ignored for eligibility",
			"placement", placementName, "cached_model", m)
	}
	return "absent"
}
