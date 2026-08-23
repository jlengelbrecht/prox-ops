package httpapi

import "github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"

// The types in this file are JSON wire shapes and mirror
// contracts/agent-router/schemas/*.json field-for-field. Field order and
// names are load-bearing: they are the frozen contract, not a convenience
// restatement of it.

type errorBody struct {
	Error errorDetail `json:"error"`
}

type errorDetail struct {
	Code              string         `json:"code"`
	Message           string         `json:"message"`
	Obligation        string         `json:"obligation"`
	HTTPStatus        int            `json:"http_status"`
	Endpoint          string         `json:"endpoint,omitempty"`
	Details           map[string]any `json:"details,omitempty"`
	RetryAfterSeconds *int           `json:"retry_after_seconds,omitempty"`
	CatalogVersion    *string        `json:"catalog_version,omitempty"`
}

type heartbeatAck struct {
	Accepted                  bool   `json:"accepted"`
	Node                      string `json:"node"`
	ObservedState             string `json:"observed_state"`
	EligibleForPlacement      bool   `json:"eligible_for_placement"`
	NextHeartbeatAfterSeconds int    `json:"next_heartbeat_after_seconds"`
	OfflineAfterSeconds       int    `json:"offline_after_seconds"`
	CatalogVersion            string `json:"catalog_version,omitempty"`
}

type statusResponse struct {
	CatalogVersion         string          `json:"catalog_version"`
	CatalogDocumentVersion string          `json:"catalog_document_version"`
	CatalogSchemaVersion   int             `json:"catalog_schema_version"`
	GeneratedAt            string          `json:"generated_at"`
	Router                 routerInfo      `json:"router"`
	HeartbeatPolicy        heartbeatPolicy `json:"heartbeat_policy"`
	Harnesses              []harnessDTO    `json:"harnesses"`
	EntitlementPools       []poolDTO       `json:"entitlement_pools"`
	Profiles               []profileDTO    `json:"profiles"`
	Placements             []placementDTO  `json:"placements"`
	Policies               []policyDTO     `json:"policies"`
}

type routerInfo struct {
	Version       string `json:"version"`
	CapacityState string `json:"capacity_state"`
	UptimeSeconds int64  `json:"uptime_seconds"`
}

type heartbeatPolicy struct {
	IntervalSeconds     int `json:"interval_seconds"`
	OfflineAfterSeconds int `json:"offline_after_seconds"`
}

type harnessDTO struct {
	Name            string `json:"name"`
	Supported       bool   `json:"supported"`
	Selectable      bool   `json:"selectable"`
	RouterBehaviour string `json:"router_behaviour,omitempty"`
}

type poolDTO struct {
	Name                  string `json:"name"`
	Provider              string `json:"provider"`
	CostClass             string `json:"cost_class"`
	CredentialClass       string `json:"credential_class"`
	Spillover             string `json:"spillover"`
	RequiresAuthorization *bool  `json:"requires_authorization,omitempty"`
}

type entitlementDTO struct {
	Pool      *string `json:"pool"`
	CostClass string  `json:"cost_class"`
}

type physicalDTO struct {
	ModelID    string  `json:"model_id"`
	Placement  *string `json:"placement"`
	Eligible   bool    `json:"eligible"`
	ReasonCode string  `json:"reason_code,omitempty"`
}

type profileDTO struct {
	Name         string           `json:"name"`
	CostClass    string           `json:"cost_class"`
	Hosting      string           `json:"hosting"`
	Alignment    string           `json:"alignment"`
	Capabilities []string         `json:"capabilities"`
	MinContext   *int             `json:"min_context"`
	ForbiddenFor []string         `json:"forbidden_for"`
	Selectable   bool             `json:"selectable"`
	Entitlements []entitlementDTO `json:"entitlements"`
	Physical     []physicalDTO    `json:"physical"`
}

type placementDTO struct {
	Name               string               `json:"name"`
	Kind               string               `json:"kind"`
	Status             string               `json:"status"`
	Selectable         bool                 `json:"selectable"`
	State              string               `json:"state"`
	StateSource        string               `json:"state_source"`
	Readiness          string               `json:"readiness"`
	Eligible           bool                 `json:"eligible"`
	ColdStartSEstimate *float64             `json:"cold_start_s_estimate"`
	LastHeartbeat      *string              `json:"last_heartbeat"`
	Heartbeat          *heartbeat.Heartbeat `json:"heartbeat"`
}

// routeRequestWire mirrors contracts/agent-router/openapi.yaml
// components.schemas.RouteRequest field-for-field. Decoded with
// DisallowUnknownFields so an unrecognized property is rejected at the
// wire, matching the schema's additionalProperties: false closure.
type routeRequestWire struct {
	StoryID         string   `json:"story_id"`
	Title           string   `json:"title"`
	Summary         string   `json:"summary"`
	TouchedPaths    []string `json:"touched_paths"`
	Ambiguity       string   `json:"ambiguity"`
	BlastRadius     string   `json:"blast_radius"`
	ContextSize     *int     `json:"context_size"`
	VolumeHint      string   `json:"volume_hint"`
	Tags            []string `json:"tags"`
	Repo            string   `json:"repo"`
	Requester       string   `json:"requester"`
	PlacementPolicy *string  `json:"placement_policy"`
	AllowMetered    *bool    `json:"allow_metered"`
	CatalogVersion  *string  `json:"catalog_version"`
}

// executionProfileWire mirrors
// contracts/agent-router/schemas/execution-profile.schema.json
// field-for-field. Field order and names are the frozen contract.
type executionProfileWire struct {
	Harness                string            `json:"harness"`
	ModelProfile           string            `json:"model_profile"`
	Effort                 string            `json:"effort"`
	CostClass              string            `json:"cost_class"`
	EntitlementPool        *string           `json:"entitlement_pool"`
	PlacementPolicy        string            `json:"placement_policy"`
	PlacementRequired      bool              `json:"placement_required"`
	Fallbacks              []fallbackWire    `json:"fallbacks"`
	Metered                bool              `json:"metered"`
	Rationale              string            `json:"rationale"`
	CatalogVersion         string            `json:"catalog_version"`
	CatalogDocumentVersion string            `json:"catalog_document_version,omitempty"`
	ExpiresAt              string            `json:"expires_at"`
	MeteredDenied          *meteredDeniedDTO `json:"metered_denied,omitempty"`
}

type fallbackWire struct {
	Harness           string  `json:"harness"`
	ModelProfile      string  `json:"model_profile"`
	CostClass         string  `json:"cost_class"`
	EntitlementPool   *string `json:"entitlement_pool"`
	PlacementRequired bool    `json:"placement_required"`
}

type meteredDeniedDTO struct {
	Code       string               `json:"code"`
	Message    string               `json:"message"`
	Obligation string               `json:"obligation"`
	Withheld   withheldCandidateDTO `json:"withheld"`
}

type withheldCandidateDTO struct {
	Harness      string `json:"harness"`
	ModelProfile string `json:"model_profile"`
	CostClass    string `json:"cost_class"`
}

type policyDTO struct {
	Name               string   `json:"name"`
	PreferOrder        []string `json:"prefer_order"`
	WarmPreference     string   `json:"warm_preference"`
	WarmBonusRankShift *int     `json:"warm_bonus_rank_shift"`
	AllowColdStart     bool     `json:"allow_cold_start"`
	MinVramGB          *float64 `json:"min_vram_gb"`
	MinFreeVramGB      *float64 `json:"min_free_vram_gb"`
	ResolvesToday      bool     `json:"resolves_today"`
	ResolvesNow        bool     `json:"resolves_now"`
}
