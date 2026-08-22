// Package heartbeat defines the edge worker capacity heartbeat payload -
// the request body of POST /v1/capacity/heartbeat, and the shape GET
// /v1/status re-exposes verbatim for each edge placement
// (contracts/agent-router/schemas/heartbeat.schema.json,
// edge/EDGE-WORKER-CONTRACT.md section 1).
package heartbeat

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// States a node may self-report. State ownership is local: the host
// decides, the cluster never forces a node into SERVING.
const (
	StateAvailable   = "AVAILABLE"
	StateServing     = "SERVING"
	StateDraining    = "DRAINING"
	StateInteractive = "INTERACTIVE"
	StateOffline     = "OFFLINE"
)

var validStates = map[string]bool{
	StateAvailable:   true,
	StateServing:     true,
	StateDraining:    true,
	StateInteractive: true,
	StateOffline:     true,
}

var validGPUVendors = map[string]bool{"amd": true, "nvidia": true, "unknown": true}

var validCapabilities = map[string]bool{"chat": true, "tools": true, "vision": true, "audio": true}

// GPU is heartbeat.gpu.
type GPU struct {
	Vendor         string   `json:"vendor"`
	Model          string   `json:"model"`
	Arch           string   `json:"arch"`
	VRAMTotalGB    *float64 `json:"vram_total_gb"`
	VRAMFreeGB     *float64 `json:"vram_free_gb"`
	UtilizationPct float64  `json:"utilization_pct"`
}

// Runtime is heartbeat.runtime. Endpoint is observational status metadata
// only (owner ruling R9) - it must never be treated as an authoritative
// routing target anywhere in this service.
type Runtime struct {
	Kind     string `json:"kind"`
	Version  string `json:"version"`
	Endpoint string `json:"endpoint"`
}

// Heartbeat is the edge worker capacity heartbeat payload.
type Heartbeat struct {
	Node             string   `json:"node"`
	State            string   `json:"state"`
	GPU              GPU      `json:"gpu"`
	Runtime          Runtime  `json:"runtime"`
	ActiveModel      *string  `json:"active_model"`
	CachedModels     []string `json:"cached_models"`
	Preemptible      bool     `json:"preemptible"`
	Interactive      bool     `json:"interactive"`
	ACPower          bool     `json:"ac_power"`
	ClusterReachable bool     `json:"cluster_reachable"`
	LastHeartbeat    string   `json:"last_heartbeat"`
	Capabilities     []string `json:"capabilities"`
	MaxContext       int      `json:"max_context"`
}

// InvalidError describes why a heartbeat body failed validation. Field is
// a dotted path, matching the Error schema's details.field convention.
type InvalidError struct {
	Field   string
	Message string
}

func (e *InvalidError) Error() string {
	return fmt.Sprintf("%s: %s", e.Field, e.Message)
}

var requiredTopLevel = []string{
	"node", "state", "gpu", "runtime", "active_model", "cached_models",
	"preemptible", "interactive", "ac_power", "cluster_reachable",
	"last_heartbeat", "capabilities", "max_context",
}

var requiredGPU = []string{"vendor", "model", "arch", "vram_total_gb", "vram_free_gb", "utilization_pct"}

var requiredRuntime = []string{"kind", "version", "endpoint"}

// Parse decodes and validates a heartbeat request body against
// heartbeat.schema.json. PRESENCE matters: unlike the catalog, a missing
// key here means a broken agent, not an unmeasured value (schema
// description, "PRESENCE"), so this checks key presence explicitly rather
// than trusting Go's zero-value decode to stand in for "absent".
func Parse(body []byte) (*Heartbeat, error) {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(body, &top); err != nil {
		return nil, &InvalidError{Field: "", Message: "body is not a JSON object: " + err.Error()}
	}
	if err := requirePresent(top, requiredTopLevel, ""); err != nil {
		return nil, err
	}

	var gpuTop map[string]json.RawMessage
	if err := json.Unmarshal(top["gpu"], &gpuTop); err != nil {
		return nil, &InvalidError{Field: "gpu", Message: "gpu is not a JSON object"}
	}
	if err := requirePresent(gpuTop, requiredGPU, "gpu."); err != nil {
		return nil, err
	}

	var runtimeTop map[string]json.RawMessage
	if err := json.Unmarshal(top["runtime"], &runtimeTop); err != nil {
		return nil, &InvalidError{Field: "runtime", Message: "runtime is not a JSON object"}
	}
	if err := requirePresent(runtimeTop, requiredRuntime, "runtime."); err != nil {
		return nil, err
	}

	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var hb Heartbeat
	if err := dec.Decode(&hb); err != nil {
		return nil, &InvalidError{Field: "", Message: "body does not match the heartbeat shape: " + err.Error()}
	}

	if err := hb.validateFields(); err != nil {
		return nil, err
	}
	return &hb, nil
}

func (hb *Heartbeat) validateFields() error {
	if hb.Node == "" {
		return &InvalidError{Field: "node", Message: "must not be empty"}
	}
	if !validStates[hb.State] {
		return &InvalidError{Field: "state", Message: "must be one of AVAILABLE, SERVING, DRAINING, INTERACTIVE, OFFLINE"}
	}
	if hb.State == StateInteractive && !hb.Interactive {
		return &InvalidError{Field: "interactive", Message: "must be true when state is INTERACTIVE"}
	}
	if !validGPUVendors[hb.GPU.Vendor] {
		return &InvalidError{Field: "gpu.vendor", Message: "must be one of amd, nvidia, unknown"}
	}
	if hb.GPU.Model == "" {
		return &InvalidError{Field: "gpu.model", Message: "must not be empty"}
	}
	if hb.GPU.Arch == "" {
		return &InvalidError{Field: "gpu.arch", Message: "must not be empty"}
	}
	if hb.GPU.UtilizationPct < 0 || hb.GPU.UtilizationPct > 100 {
		return &InvalidError{Field: "gpu.utilization_pct", Message: "must be between 0 and 100"}
	}
	if hb.GPU.VRAMTotalGB != nil && *hb.GPU.VRAMTotalGB < 0 {
		return &InvalidError{Field: "gpu.vram_total_gb", Message: "must not be negative"}
	}
	if hb.GPU.VRAMFreeGB != nil && *hb.GPU.VRAMFreeGB < 0 {
		return &InvalidError{Field: "gpu.vram_free_gb", Message: "must not be negative"}
	}
	if hb.Runtime.Kind == "" {
		return &InvalidError{Field: "runtime.kind", Message: "must not be empty"}
	}
	if hb.Runtime.Endpoint == "" {
		return &InvalidError{Field: "runtime.endpoint", Message: "must not be empty"}
	}
	if hb.Capabilities == nil {
		return &InvalidError{Field: "capabilities", Message: "must be an array, not null"}
	}
	for i, c := range hb.Capabilities {
		if !validCapabilities[c] {
			return &InvalidError{Field: fmt.Sprintf("capabilities[%d]", i), Message: "must be one of chat, tools, vision, audio"}
		}
	}
	if hb.MaxContext < 1 {
		return &InvalidError{Field: "max_context", Message: "must be at least 1"}
	}
	if hb.CachedModels == nil {
		return &InvalidError{Field: "cached_models", Message: "must be an array, not null"}
	}
	for i, m := range hb.CachedModels {
		if m == "" {
			return &InvalidError{Field: fmt.Sprintf("cached_models[%d]", i), Message: "must not be empty"}
		}
	}
	return nil
}

func requirePresent(m map[string]json.RawMessage, keys []string, prefix string) error {
	for _, k := range keys {
		if _, ok := m[k]; !ok {
			return &InvalidError{Field: prefix + k, Message: "is required"}
		}
	}
	return nil
}
