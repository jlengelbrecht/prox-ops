package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/stampvalidate"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// realDigest is catalog 1.3.0's committed content digest
// (contracts/agent-router/README.md's digest table).
const realDigest = "sha256:2fd681e0e988bf6be94b8923b1485a0aa174a4e66aec580a2d383c764b60e229"

func fixedNow(t *testing.T) time.Time {
	t.Helper()
	now, err := time.Parse(time.RFC3339, "2026-08-20T12:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	return now
}

func realCatalogPath(t *testing.T) string {
	t.Helper()
	return testutil.ExtractCatalogYAML(t)
}

func writeJSON(t *testing.T, dir, name string, v any) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func validStampWire(now time.Time) map[string]any {
	return map[string]any{
		"harness":            "claude",
		"model_profile":      "claude/strong",
		"effort":             "xhigh",
		"cost_class":         "subscription",
		"entitlement_pool":   "anthropic-max",
		"metered":            false,
		"placement_policy":   "prefer-warm-local",
		"placement_required": false,
		"catalog_version":    realDigest,
		"expires_at":         now.Add(24 * time.Hour).Format(time.RFC3339),
		"task":               map[string]any{"story_id": "STORY-035-12", "title": "t", "tags": []string{}},
		"override":           nil,
	}
}

// Test_metered_cannot_self_authorize proves the MVP CLI has no code path
// that can produce metered-spend authority: newValidationContext takes no
// parameters and nothing in this binary can make it return
// MeteredSpendAuthorized: true, so a metered:true stamp always fails closed
// under this CLI - not because of a flag defaulting to false, but because no
// flag to set it exists at all (amendment 3).
func Test_metered_cannot_self_authorize(t *testing.T) {
	vctx := newValidationContext()
	if vctx.MeteredSpendAuthorized {
		t.Fatal("newValidationContext() must never grant metered-spend authority")
	}

	dir := t.TempDir()
	now := fixedNow(t)
	wire := validStampWire(now)
	wire["metered"] = true
	stampPath := writeJSON(t, dir, "stamp.json", wire)

	var stdout, stderr bytes.Buffer
	code := run([]string{"--catalog", realCatalogPath(t), "--stamp", stampPath}, &stdout, &stderr, now)
	if code != exitPolicyInvalid {
		t.Fatalf("exit code = %d, want %d (policy-invalid: metered stamp, no authority): stderr=%s", code, exitPolicyInvalid, stderr.String())
	}
	var v verdictWire
	if err := json.Unmarshal(stdout.Bytes(), &v); err != nil {
		t.Fatalf("decoding verdict stdout: %v (stdout=%s)", err, stdout.String())
	}
	if v.Valid {
		t.Fatal("verdict.valid = true, want false")
	}
	found := false
	for _, c := range v.Checks {
		if c.Name == stampvalidate.CheckMeteredDualKey {
			found = true
			if c.Passed || c.ReasonCode != stampvalidate.ReasonMeteredAuthorityMissing {
				t.Errorf("metered_dual_key check = %+v, want failed with %q", c, stampvalidate.ReasonMeteredAuthorityMissing)
			}
		}
	}
	if !found {
		t.Fatal("no metered_dual_key check in the verdict")
	}
}

// Test_failure_before_launch proves that on an invalid or malformed stamp
// this binary performs NO WRITES anywhere it was not explicitly told to
// write (it was told to write nothing - it only reads --catalog/--stamp/
// --evidence and writes stdout/stderr), and that the frozen exit codes (1
// for policy-invalid, 2 for malformed/tool failure) hold. Real
// launch-ordering proof against Orca is a 35.22 concern; this proves the
// half this binary owns: an invalid verdict never runs anything, because
// this tool never runs anything at all (see the boundary greps in
// contracts/agent-router/verify-stamp-cases.sh).
func Test_failure_before_launch(t *testing.T) {
	scratch := t.TempDir()
	before := listDir(t, scratch)

	now := fixedNow(t)

	t.Run("policy_invalid_stamp_exits_1_no_writes", func(t *testing.T) {
		dir := t.TempDir()
		wire := validStampWire(now)
		wire["harness"] = "codex" // codex+claude/strong is not an approved pair
		stampPath := writeJSON(t, dir, "stamp.json", wire)

		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", realCatalogPath(t), "--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitPolicyInvalid {
			t.Fatalf("exit code = %d, want %d: stderr=%s", code, exitPolicyInvalid, stderr.String())
		}
		var v verdictWire
		if err := json.Unmarshal(stdout.Bytes(), &v); err != nil {
			t.Fatalf("decoding verdict: %v", err)
		}
		if v.Valid {
			t.Fatal("verdict.valid = true, want false")
		}
	})

	t.Run("malformed_json_exits_2_no_verdict", func(t *testing.T) {
		dir := t.TempDir()
		stampPath := filepath.Join(dir, "stamp.json")
		if err := os.WriteFile(stampPath, []byte("{not valid json"), 0o600); err != nil {
			t.Fatal(err)
		}
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", realCatalogPath(t), "--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
		if stdout.Len() != 0 {
			t.Fatalf("stdout = %q, want empty: malformed input must print no Verdict", stdout.String())
		}
		if stderr.Len() == 0 {
			t.Fatal("stderr is empty, want a diagnostic")
		}
	})

	t.Run("missing_catalog_flag_exits_2", func(t *testing.T) {
		t.Setenv("AGENT_STAMP_VALIDATE_CATALOG_PATH", "")
		dir := t.TempDir()
		stampPath := writeJSON(t, dir, "stamp.json", validStampWire(now))
		var stdout, stderr bytes.Buffer
		code := run([]string{"--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
	})

	t.Run("unknown_field_exits_2", func(t *testing.T) {
		dir := t.TempDir()
		wire := validStampWire(now)
		wire["unexpected_field"] = "smuggled"
		stampPath := writeJSON(t, dir, "stamp.json", wire)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", realCatalogPath(t), "--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
	})

	after := listDir(t, scratch)
	if before != after {
		t.Fatalf("scratch directory changed across runs (before=%q after=%q): this binary must perform no writes of its own", before, after)
	}
}

// Test_run_ValidStamp_ExitsZero is a basic control proving the happy path
// exits 0 with valid: true, so the failure-path assertions above are
// meaningful contrasts rather than the only path ever exercised.
func Test_run_ValidStamp_ExitsZero(t *testing.T) {
	now := fixedNow(t)
	dir := t.TempDir()
	stampPath := writeJSON(t, dir, "stamp.json", validStampWire(now))
	var stdout, stderr bytes.Buffer
	code := run([]string{"--catalog", realCatalogPath(t), "--stamp", stampPath}, &stdout, &stderr, now)
	if code != exitValid {
		t.Fatalf("exit code = %d, want %d: stderr=%s", code, exitValid, stderr.String())
	}
	var v verdictWire
	if err := json.Unmarshal(stdout.Bytes(), &v); err != nil {
		t.Fatalf("decoding verdict: %v", err)
	}
	if !v.Valid {
		t.Fatalf("verdict.valid = false, want true: %+v", v)
	}
}

func listDir(t *testing.T, dir string) string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return filepath.Join(names...)
}
