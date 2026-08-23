// Package catalog loads and represents the agent-router model & capability
// catalog (kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml,
// key catalog.yaml). It conforms to that document, not the reverse: this
// package refuses a schema_version it does not implement rather than
// best-effort parsing an unknown shape.
package catalog

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// SupportedSchemaVersion is the structural catalog schema_version this
// router build implements. contracts/agent-router/openapi.yaml requires a
// router to refuse any other major rather than best-effort parse it.
const SupportedSchemaVersion = 1

// UnsupportedSchemaError reports a catalog whose schema_version this build
// does not implement. It is the PERMANENT case (503
// catalog_schema_unsupported, obligation abort): retrying never helps, only
// deploying a router that understands the new schema or reverting the
// catalog does. Kept distinct from a generic load failure (503
// catalog_unavailable, obligation retry) so a caller is never left to guess
// which one it got.
type UnsupportedSchemaError struct {
	Received  int
	Supported int
}

func (e *UnsupportedSchemaError) Error() string {
	return fmt.Sprintf("catalog schema_version %d is not implemented by this router (supports %d)", e.Received, e.Supported)
}

// Entry is one named row from a catalog table, kept in source document
// order. Order matters: the frozen contract's fixtures render harnesses,
// placements, entitlement pools, profiles and policies in exactly the order
// the catalog document declares them, and this package preserves that
// instead of re-sorting it (contracts/agent-router/README.md "Vocabulary
// coupling").
type Entry[V any] struct {
	Name  string
	Value V
}

// orderedMap decodes a YAML mapping into a slice of Entry, preserving key
// order. gopkg.in/yaml.v3 decoding into a Go map loses order; decoding
// against the mapping node directly keeps it.
type orderedMap[V any] []Entry[V]

func (om *orderedMap[V]) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind != yaml.MappingNode {
		return fmt.Errorf("expected a YAML mapping, got kind %v", node.Kind)
	}
	result := make(orderedMap[V], 0, len(node.Content)/2)
	for i := 0; i+1 < len(node.Content); i += 2 {
		var v V
		if err := node.Content[i+1].Decode(&v); err != nil {
			return fmt.Errorf("key %q: %w", node.Content[i].Value, err)
		}
		result = append(result, Entry[V]{Name: node.Content[i].Value, Value: v})
	}
	*om = result
	return nil
}

type defaults struct {
	PlacementPolicy string `yaml:"placement_policy"`
}

// Harness is one row of catalog table 1 (harnesses).
type Harness struct {
	Description     string `yaml:"description"`
	Supported       bool   `yaml:"supported"`
	Selectable      bool   `yaml:"selectable"`
	RouterBehaviour string `yaml:"router_behaviour"`
}

// Capacity is a placement's catalog table 2 `capacity` block. VramGB is only
// ever a number this repo can stand behind - nil means no measurement
// exists, and a hard VRAM floor (policy min_vram_gb) must never be
// satisfied from anything else, in particular never from a SKU nameplate
// figure (contracts/agent-router/README.md placements table notes).
type Capacity struct {
	VramGB *float64 `yaml:"vram_gb"`
}

// Placement is one row of catalog table 2 (placements). Only the fields
// /v1/status and /v1/place render or that capacity computation needs are
// decoded; the full catalog carries more (gpu, node) that this story's scope
// does not consume.
type Placement struct {
	Description        string   `yaml:"description"`
	Status             string   `yaml:"status"` // available | planned
	Selectable         bool     `yaml:"selectable"`
	Kind               string   `yaml:"kind"` // kserve | edge
	Capacity           Capacity `yaml:"capacity"`
	ColdStartSEstimate *float64 `yaml:"cold_start_s_estimate"`
	// ScaleToZero is the placement's own declaration that idle means scaled
	// to zero (catalog table 2). It is what allow_cold_start: false forbids
	// waking - nil (undeclared) placements are unaffected by that rule.
	ScaleToZero *bool `yaml:"scale_to_zero"`
}

// Model is one row of catalog table 3 (models), the referent for every
// profiles[].physical[].model_id.
type Model struct {
	Hosting      string   `yaml:"hosting"`
	Placements   []string `yaml:"placements"`
	Capabilities []string `yaml:"capabilities"`
	MaxContext   *int     `yaml:"max_context"`
	// VramGbEstimate is the minimum load requirement /v1/place uses for VRAM
	// feasibility (owner clarification 3, STORY-035-11): budgeted VRAM to
	// load and run this model, not a measurement of any specific placement.
	// nil for vendor-hosted models, which never touch this check.
	VramGbEstimate *float64 `yaml:"vram_gb_estimate"`
}

// EntitlementPool is one row of catalog table 4 (entitlement_pools).
type EntitlementPool struct {
	Provider        string `yaml:"provider"`
	CostClass       string `yaml:"cost_class"`
	CredentialClass string `yaml:"credential_class"`
	Spillover       string `yaml:"spillover"`
}

// ProfileEntitlement is one ordered funding candidate for a profile.
type ProfileEntitlement struct {
	Pool      *string `yaml:"pool"`
	CostClass string  `yaml:"cost_class"`
}

// ProfilePhysical is one ordered {model_id, placement} candidate for a
// profile. Placement is nil for vendor-hosted candidates, which never touch
// agentgateway.
type ProfilePhysical struct {
	ModelID   string  `yaml:"model_id"`
	Placement *string `yaml:"placement"`
}

// Profile is one row of catalog table 5 (profiles), the only thing BMAD
// stamps as model_profile.
type Profile struct {
	Description  string               `yaml:"description"`
	CostClass    string               `yaml:"cost_class"`
	Hosting      string               `yaml:"hosting"`
	Selectable   bool                 `yaml:"selectable"`
	Capabilities []string             `yaml:"capabilities"`
	MinContext   *int                 `yaml:"min_context"`
	Alignment    string               `yaml:"alignment"`
	ForbiddenFor []string             `yaml:"forbidden_for"`
	Entitlements []ProfileEntitlement `yaml:"entitlements"`
	Physical     []ProfilePhysical    `yaml:"physical"`
}

// Policy is one row of catalog table 6 (policies.placement_policy).
type Policy struct {
	Description        string   `yaml:"description"`
	PreferOrder        []string `yaml:"prefer_order"`
	WarmPreference     string   `yaml:"warm_preference"`
	WarmBonusRankShift *int     `yaml:"warm_bonus_rank_shift"`
	AllowColdStart     bool     `yaml:"allow_cold_start"`
	MinVramGB          *float64 `yaml:"min_vram_gb"`
	MinFreeVramGB      *float64 `yaml:"min_free_vram_gb"`
	ResolvesToday      bool     `yaml:"resolves_today"`
}

type policiesTable struct {
	PlacementPolicy orderedMap[Policy] `yaml:"placement_policy"`
}

// document is the raw decode target; Catalog below is the flattened,
// public view built from it.
type document struct {
	Version          string                      `yaml:"version"`
	SchemaVersion    int                         `yaml:"schema_version"`
	Defaults         defaults                    `yaml:"defaults"`
	Harnesses        orderedMap[Harness]         `yaml:"harnesses"`
	Placements       orderedMap[Placement]       `yaml:"placements"`
	Models           orderedMap[Model]           `yaml:"models"`
	EntitlementPools orderedMap[EntitlementPool] `yaml:"entitlement_pools"`
	Profiles         orderedMap[Profile]         `yaml:"profiles"`
	Policies         policiesTable               `yaml:"policies"`
}

// Catalog is the parsed, order-preserved view of the agent-router model &
// capability catalog.
type Catalog struct {
	DocumentVersion        string
	SchemaVersion          int
	DefaultPlacementPolicy string
	Harnesses              []Entry[Harness]
	Placements             []Entry[Placement]
	Models                 map[string]Model
	EntitlementPools       []Entry[EntitlementPool]
	Profiles               []Entry[Profile]
	Policies               []Entry[Policy]
}

// ModelAuthorizedOnPlacement reports whether the catalog places modelID on
// placementName at all - the Git/catalog authority half of "effective
// capability = Git/catalog authority INTERSECT heartbeat observation"
// (contracts/agent-router/openapi.yaml). A heartbeat's active_model or
// cached_models entry only counts as warm/cached when this is true; an
// entry with no catalog counterpart is a reconciliation alarm and is
// ignored for eligibility rather than acted on.
func (c *Catalog) ModelAuthorizedOnPlacement(modelID, placementName string) bool {
	model, ok := c.Models[modelID]
	if !ok {
		return false
	}
	for _, p := range model.Placements {
		if p == placementName {
			return true
		}
	}
	return false
}

// Digest computes the catalog_version content digest: sha256 over the raw
// bytes exactly as read, before any YAML parsing. This mirrors
// contracts/agent-router/verify-digests.sh and the definition in
// contracts/agent-router/README.md "Vocabulary coupling" - it is never
// computed from a re-serialized or re-indented document.
func Digest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// Load reads and parses the catalog document at path, returning the parsed
// catalog and its content digest. The digest is returned even on a parse or
// schema-version failure where possible, since the raw bytes are read
// before parsing is attempted; callers should not rely on it in that case.
//
// In story 35.9a path is supplied by env/flag so tests can point it at
// fixtures. 35.9b mounts the same bytes via a ConfigMap volume with
// subPath: catalog.yaml, so the file this function reads is always the
// catalog document itself - never the ConfigMap wrapper - in both cases.
func Load(path string) (*Catalog, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, "", fmt.Errorf("reading catalog file %q: %w", path, err)
	}
	digest := Digest(raw)

	var doc document
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		return nil, digest, fmt.Errorf("parsing catalog YAML: %w", err)
	}
	if doc.SchemaVersion != SupportedSchemaVersion {
		return nil, digest, &UnsupportedSchemaError{Received: doc.SchemaVersion, Supported: SupportedSchemaVersion}
	}

	models := make(map[string]Model, len(doc.Models))
	for _, e := range doc.Models {
		models[e.Name] = e.Value
	}

	cat := &Catalog{
		DocumentVersion:        doc.Version,
		SchemaVersion:          doc.SchemaVersion,
		DefaultPlacementPolicy: doc.Defaults.PlacementPolicy,
		Harnesses:              doc.Harnesses,
		Placements:             doc.Placements,
		Models:                 models,
		EntitlementPools:       doc.EntitlementPools,
		Profiles:               doc.Profiles,
		Policies:               doc.Policies.PlacementPolicy,
	}
	return cat, digest, nil
}
