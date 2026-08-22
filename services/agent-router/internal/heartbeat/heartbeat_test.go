package heartbeat_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

func readExample(t *testing.T, name string) []byte {
	t.Helper()
	root := testutil.RepoRoot(t)
	path := filepath.Join(root, "contracts", "agent-router", "examples", "heartbeat", name)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("reading committed example %q: %v", name, err)
	}
	return raw
}

func TestParse_CommittedExamples(t *testing.T) {
	for _, name := range []string{
		"state-available.json",
		"state-serving.json",
		"state-draining.json",
		"state-interactive.json",
		"state-offline.json",
		"unmeasured-laptop-on-battery.json",
	} {
		t.Run(name, func(t *testing.T) {
			hb, err := heartbeat.Parse(readExample(t, name))
			if err != nil {
				t.Fatalf("Parse(%s): %v", name, err)
			}
			if hb.Node == "" {
				t.Error("Node is empty")
			}
		})
	}
}

func TestParse_MissingRequiredField(t *testing.T) {
	body := []byte(`{
		"node": "n1",
		"gpu": {"vendor":"amd","model":"RX 7900 XTX","arch":"gfx1100","vram_total_gb":24,"vram_free_gb":21.9,"utilization_pct":3},
		"runtime": {"kind":"llama-swap","version":"1.0","endpoint":"https://x"},
		"active_model": null,
		"cached_models": [],
		"preemptible": true,
		"interactive": false,
		"ac_power": true,
		"cluster_reachable": true,
		"last_heartbeat": "2026-08-19T15:00:12Z",
		"capabilities": [],
		"max_context": 8192
	}`)
	_, err := heartbeat.Parse(body)
	if err == nil {
		t.Fatal("Parse: expected an error for a missing required field (state), got nil")
	}
	var ierr *heartbeat.InvalidError
	if !asInvalid(err, &ierr) {
		t.Fatalf("Parse: got %v (%T), want *heartbeat.InvalidError", err, err)
	}
	if ierr.Field != "state" {
		t.Errorf("InvalidError.Field = %q, want %q", ierr.Field, "state")
	}
}

func TestParse_InvalidEnum(t *testing.T) {
	body := replaceState(t, readExample(t, "state-available.json"), "BOGUS")
	_, err := heartbeat.Parse(body)
	if err == nil {
		t.Fatal("Parse: expected an error for state=BOGUS, got nil")
	}
	var ierr *heartbeat.InvalidError
	if !asInvalid(err, &ierr) {
		t.Fatalf("Parse: got %v (%T), want *heartbeat.InvalidError", err, err)
	}
	if ierr.Field != "state" {
		t.Errorf("InvalidError.Field = %q, want %q", ierr.Field, "state")
	}
}

func TestParse_NotJSON(t *testing.T) {
	_, err := heartbeat.Parse([]byte("not json at all"))
	if err == nil {
		t.Fatal("Parse: expected an error for a non-JSON body, got nil")
	}
}

func TestParse_InteractiveStateRequiresInteractiveFlag(t *testing.T) {
	body := []byte(`{
		"node": "n1",
		"state": "INTERACTIVE",
		"gpu": {"vendor":"amd","model":"RX 7900 XTX","arch":"gfx1100","vram_total_gb":24,"vram_free_gb":21.9,"utilization_pct":3},
		"runtime": {"kind":"llama-swap","version":"1.0","endpoint":"https://x"},
		"active_model": null,
		"cached_models": [],
		"preemptible": true,
		"interactive": false,
		"ac_power": true,
		"cluster_reachable": true,
		"last_heartbeat": "2026-08-19T15:00:12Z",
		"capabilities": [],
		"max_context": 8192
	}`)
	_, err := heartbeat.Parse(body)
	if err == nil {
		t.Fatal("Parse: expected an error when state=INTERACTIVE but interactive=false, got nil")
	}
}

func asInvalid(err error, target **heartbeat.InvalidError) bool {
	ierr, ok := err.(*heartbeat.InvalidError)
	if !ok {
		return false
	}
	*target = ierr
	return true
}

func replaceState(t *testing.T, body []byte, newState string) []byte {
	t.Helper()
	s := string(body)
	old := `"state": "AVAILABLE"`
	replacement := `"state": "` + newState + `"`
	out := []byte(s)
	if idx := indexOf(s, old); idx >= 0 {
		out = []byte(s[:idx] + replacement + s[idx+len(old):])
	} else {
		t.Fatalf("fixture does not contain %q", old)
	}
	return out
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
