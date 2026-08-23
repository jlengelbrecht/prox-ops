package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
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

// validEvidenceWire is a well-formed "placed" PlacementEvidence document -
// the baseline the structural-validation test cases below each mutate one
// field of at a time.
func validEvidenceWire(resolvedAt time.Time) map[string]any {
	return map[string]any{
		"resolved_at": resolvedAt.Format(time.RFC3339),
		"result": map[string]any{
			"status":                 "placed",
			"model":                  "qwen36-27b",
			"placement":              "cachyos-7900xtx",
			"readiness":              "warm",
			"estimated_cold_start_s": 0,
			"headers":                map[string]any{"x-placement": "cachyos-7900xtx"},
			"ttl_seconds":            30,
			"reason":                 map[string]any{"code": "placed_warm", "message": "warm edge candidate"},
			"alternatives":           []any{},
			"catalog_version":        realDigest,
		},
	}
}

// validUnavailableEvidenceWire is a well-formed "unavailable" PlacementEvidence
// document - the EXPLICIT EMPTY PLACEMENT shape place-result.schema.json
// requires (no model, no placement, no headers).
func validUnavailableEvidenceWire(resolvedAt time.Time) map[string]any {
	return map[string]any{
		"resolved_at": resolvedAt.Format(time.RFC3339),
		"result": map[string]any{
			"status":                 "unavailable",
			"model":                  nil,
			"placement":              nil,
			"readiness":              nil,
			"estimated_cold_start_s": nil,
			"headers":                map[string]any{},
			"ttl_seconds":            30,
			"reason":                 map[string]any{"code": "no_eligible_placement", "message": "nothing eligible right now"},
			"alternatives":           []any{},
			"catalog_version":        realDigest,
		},
	}
}

// validAlternativeWire is a well-formed alternatives[] entry - the baseline
// the alternative-focused structural-validation cases below each mutate one
// field of.
func validAlternativeWire() map[string]any {
	return map[string]any{
		"placement":              "kserve-a5000",
		"model":                  "qwen36-27b",
		"readiness":              "cached",
		"estimated_cold_start_s": 5,
		"eligible":               true,
		"reason":                 map[string]any{"code": "not_selected_lower_rank", "message": "ranked below the chosen candidate"},
	}
}

// cloneJSON deep-copies a JSON-shaped map via a marshal/unmarshal round trip,
// so each structural-validation test case mutates its own private copy of
// the baseline fixture rather than sharing nested maps across subtests.
func cloneJSON(t *testing.T, v map[string]any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// Test_evidence_structural_validation proves the CLI parser enforces the
// FROZEN place-result.schema.json's structural invariants on
// PlacementEvidence.result - header-key closure, the ttl floor, the
// readiness/reason-code vocabularies, and the status-dependent {placed,
// unavailable} shape - rejecting a violation as malformed input (exit 2,
// no verdict printed) rather than letting a schema-shape violation reach
// stampvalidate.ValidateFinal as if it were a policy question (owner
// directive, 2026-08-23 correctness round).
func Test_evidence_structural_validation(t *testing.T) {
	now := fixedNow(t)
	catalogPath := realCatalogPath(t)
	stampPath := writeJSON(t, t.TempDir(), "stamp.json", validStampWire(now))

	assertMalformed := func(t *testing.T, evPath string) {
		t.Helper()
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath, "--evidence", evPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d (malformed placement evidence): stderr=%s", code, exitToolFailure, stderr.String())
		}
		if stdout.Len() != 0 {
			t.Fatalf("stdout = %q, want empty: malformed evidence must print no Verdict", stdout.String())
		}
	}

	placedCases := []struct {
		name   string
		mutate func(result map[string]any)
	}{
		{"missing_readiness", func(r map[string]any) { r["readiness"] = nil }},
		{"extra_header_key", func(r map[string]any) {
			r["headers"] = map[string]any{"x-placement": "cachyos-7900xtx", "x-extra": "smuggled"}
		}},
		{"ttl_seconds_below_floor", func(r map[string]any) { r["ttl_seconds"] = 0 }},
		{"invalid_placement_vocab", func(r map[string]any) { r["placement"] = "made-up-placement" }},
		{"invalid_readiness_vocab", func(r map[string]any) { r["readiness"] = "kinda-warm" }},
		{"reason_code_wrong_subset", func(r map[string]any) {
			// no_eligible_placement is an UNAVAILABLE-only reason code; illegal
			// on a placed result even though it is a member of the frozen
			// reason.code enum overall.
			r["reason"] = map[string]any{"code": "no_eligible_placement", "message": "wrong subset for a placed result"}
		}},
		// --- required-member PRESENCE (owner directive, 2026-08-23 FINAL
		// round): a decode gap, not a value-range gap - the zero value a
		// missing/null field decodes to would otherwise look legal. ---
		{"missing_catalog_version", func(r map[string]any) { delete(r, "catalog_version") }},
		{"missing_alternatives", func(r map[string]any) { delete(r, "alternatives") }},
		{"null_alternatives", func(r map[string]any) { r["alternatives"] = nil }},
		// --- alternatives[] entries, previously copied through with NO
		// validation at all. ---
		{"alternative_missing_eligible", func(r map[string]any) {
			alt := validAlternativeWire()
			delete(alt, "eligible")
			r["alternatives"] = []any{alt}
		}},
		{"alternative_eligible_null", func(r map[string]any) {
			// Null decodes into a non-pointer bool as false, and
			// eligible: false is a LEGAL value - only the raw-presence
			// layer can tell them apart.
			alt := validAlternativeWire()
			alt["eligible"] = nil
			r["alternatives"] = []any{alt}
		}},
		{"alternative_invalid_placement", func(r map[string]any) {
			alt := validAlternativeWire()
			alt["placement"] = "made-up-placement"
			r["alternatives"] = []any{alt}
		}},
		{"alternative_empty_model", func(r map[string]any) {
			alt := validAlternativeWire()
			alt["model"] = ""
			r["alternatives"] = []any{alt}
		}},
		{"alternative_invalid_readiness", func(r map[string]any) {
			alt := validAlternativeWire()
			alt["readiness"] = "kinda-warm"
			r["alternatives"] = []any{alt}
		}},
		{"alternative_negative_cold_start", func(r map[string]any) {
			alt := validAlternativeWire()
			alt["estimated_cold_start_s"] = -1
			r["alternatives"] = []any{alt}
		}},
		{"alternative_reason_code_placed_warm", func(r map[string]any) {
			// placed_warm is a RESULT-LEVEL "placed" code, illegal inside any
			// alternative regardless of the result's own status.
			alt := validAlternativeWire()
			alt["reason"] = map[string]any{"code": "placed_warm", "message": "wrong vocabulary for a candidate"}
			r["alternatives"] = []any{alt}
		}},
		{"alternative_reason_code_no_eligible_placement", func(r map[string]any) {
			// no_eligible_placement is a RESULT-LEVEL "unavailable" code,
			// equally illegal inside an alternative.
			alt := validAlternativeWire()
			alt["reason"] = map[string]any{"code": "no_eligible_placement", "message": "wrong vocabulary for a candidate"}
			r["alternatives"] = []any{alt}
		}},
	}
	for _, tc := range placedCases {
		t.Run("placed_"+tc.name, func(t *testing.T) {
			ev := cloneJSON(t, validEvidenceWire(now))
			result := ev["result"].(map[string]any)
			tc.mutate(result)
			evPath := writeJSON(t, t.TempDir(), "evidence.json", ev)
			assertMalformed(t, evPath)
		})
	}

	t.Run("unavailable_leaks_placed_data", func(t *testing.T) {
		ev := cloneJSON(t, validUnavailableEvidenceWire(now))
		result := ev["result"].(map[string]any)
		result["model"] = "qwen36-27b"
		result["headers"] = map[string]any{"x-placement": "cachyos-7900xtx"}
		evPath := writeJSON(t, t.TempDir(), "evidence.json", ev)
		assertMalformed(t, evPath)
	})

	t.Run("valid_placed_evidence_is_accepted", func(t *testing.T) {
		// Control: proves the cases above are meaningful contrasts, not an
		// overly strict parser rejecting a legitimately well-formed document.
		evPath := writeJSON(t, t.TempDir(), "evidence.json", validEvidenceWire(now))
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath, "--evidence", evPath}, &stdout, &stderr, now)
		if code != exitValid && code != exitPolicyInvalid {
			t.Fatalf("exit code = %d, want %d or %d (well-formed evidence must parse): stderr=%s", code, exitValid, exitPolicyInvalid, stderr.String())
		}
	})

	t.Run("alternative_eligible_false_is_accepted", func(t *testing.T) {
		// Control for alternative_eligible_null: an ordinary false is a
		// legal, meaningful value (an ineligible alternative) and must
		// still parse - the null rejection is about the raw bytes, not
		// about the boolean's value.
		ev := validEvidenceWire(now)
		alt := validAlternativeWire()
		alt["eligible"] = false
		alt["reason"] = map[string]any{"code": "offline", "message": "silent past the offline window"}
		ev["result"].(map[string]any)["alternatives"] = []any{alt}
		evPath := writeJSON(t, t.TempDir(), "evidence.json", ev)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath, "--evidence", evPath}, &stdout, &stderr, now)
		if code != exitValid && code != exitPolicyInvalid {
			t.Fatalf("exit code = %d, want %d or %d (eligible: false is legal): stderr=%s", code, exitValid, exitPolicyInvalid, stderr.String())
		}
	})
}

// Test_stamp_required_field_presence proves the CLI parser rejects a stamp
// that OMITS a required member or supplies an explicit null where the
// schema does not allow one, even when the resulting Go zero value would
// otherwise look like a legal value - task.tags in particular: an omitted
// or null tags array must never be silently treated as "no tags", since
// tags is policy input to the forbidden_for check (owner directive,
// 2026-08-23 FINAL round).
func Test_stamp_required_field_presence(t *testing.T) {
	now := fixedNow(t)
	catalogPath := realCatalogPath(t)

	cases := []struct {
		name   string
		mutate func(wire map[string]any)
	}{
		{"missing_task_tags", func(wire map[string]any) {
			task := wire["task"].(map[string]any)
			delete(task, "tags")
		}},
		{"task_tags_null", func(wire map[string]any) {
			task := wire["task"].(map[string]any)
			task["tags"] = nil
		}},
		// Required booleans: encoding/json accepts null into a non-pointer
		// bool as false, so only the raw-presence layer can reject it.
		{"metered_null", func(wire map[string]any) {
			wire["metered"] = nil
		}},
		{"placement_required_null", func(wire map[string]any) {
			wire["placement_required"] = nil
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			wire := cloneJSON(t, validStampWire(now))
			tc.mutate(wire)
			stampPath := writeJSON(t, t.TempDir(), "stamp.json", wire)

			var stdout, stderr bytes.Buffer
			code := run([]string{"--catalog", catalogPath, "--stamp", stampPath}, &stdout, &stderr, now)
			if code != exitToolFailure {
				t.Fatalf("exit code = %d, want %d (malformed stamp): stderr=%s", code, exitToolFailure, stderr.String())
			}
			if stdout.Len() != 0 {
				t.Fatalf("stdout = %q, want empty: malformed stamp must print no Verdict", stdout.String())
			}
		})
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
	wire["cost_class"] = "metered" // keep cost_class/metered internally consistent so this isolates authority, not the mismatch check
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

// snapshotDir hashes the content of every file under dir (recursively),
// keyed by path relative to dir. Comparing two snapshots catches BOTH a new
// file appearing and an existing file being overwritten in place - a
// before/after name-only listing would miss the latter.
func snapshotDir(t *testing.T, dir string) map[string][32]byte {
	t.Helper()
	snap := make(map[string][32]byte)
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		snap[rel] = sha256.Sum256(data)
		return nil
	})
	if err != nil {
		t.Fatalf("snapshotting %q: %v", dir, err)
	}
	return snap
}

func assertDirUnchanged(t *testing.T, dir string, before map[string][32]byte) {
	t.Helper()
	after := snapshotDir(t, dir)
	if !reflect.DeepEqual(before, after) {
		t.Fatalf("directory %q changed across the run (before=%v after=%v): this binary must perform no writes of its own", dir, before, after)
	}
}

func copyFile(t *testing.T, src, dst string) {
	t.Helper()
	data, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(dst, data, 0o600); err != nil {
		t.Fatal(err)
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
//
// Each subtest below points BOTH --catalog and --stamp at paths inside its
// own watched directory (the catalog is a copy, not the shared fixture, so a
// hypothetical in-place write to it is also caught) and snapshots that exact
// directory's file contents before and after `run`. That is deliberate: any
// write this binary could plausibly make given the arguments it was actually
// handed - new file, or corrupted-in-place existing file - lands somewhere
// this snapshot would catch, unlike a scratch directory `run` is never told
// about at all.
func Test_failure_before_launch(t *testing.T) {
	now := fixedNow(t)
	realCatalog := realCatalogPath(t)

	newWatchedDir := func(t *testing.T) (dir, catalogPath string) {
		t.Helper()
		dir = t.TempDir()
		catalogPath = filepath.Join(dir, "catalog.yaml")
		copyFile(t, realCatalog, catalogPath)
		return dir, catalogPath
	}

	t.Run("policy_invalid_stamp_exits_1_no_writes", func(t *testing.T) {
		dir, catalogPath := newWatchedDir(t)
		wire := validStampWire(now)
		wire["harness"] = "codex" // codex+claude/strong is not an approved pair
		stampPath := writeJSON(t, dir, "stamp.json", wire)

		before := snapshotDir(t, dir)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath}, &stdout, &stderr, now)
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
		assertDirUnchanged(t, dir, before)
	})

	t.Run("malformed_json_exits_2_no_verdict", func(t *testing.T) {
		dir, catalogPath := newWatchedDir(t)
		stampPath := filepath.Join(dir, "stamp.json")
		if err := os.WriteFile(stampPath, []byte("{not valid json"), 0o600); err != nil {
			t.Fatal(err)
		}
		before := snapshotDir(t, dir)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
		if stdout.Len() != 0 {
			t.Fatalf("stdout = %q, want empty: malformed input must print no Verdict", stdout.String())
		}
		if stderr.Len() == 0 {
			t.Fatal("stderr is empty, want a diagnostic")
		}
		assertDirUnchanged(t, dir, before)
	})

	t.Run("missing_catalog_flag_exits_2", func(t *testing.T) {
		t.Setenv("AGENT_STAMP_VALIDATE_CATALOG_PATH", "")
		dir, _ := newWatchedDir(t)
		stampPath := writeJSON(t, dir, "stamp.json", validStampWire(now))
		before := snapshotDir(t, dir)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
		assertDirUnchanged(t, dir, before)
	})

	t.Run("unknown_field_exits_2", func(t *testing.T) {
		dir, catalogPath := newWatchedDir(t)
		wire := validStampWire(now)
		wire["unexpected_field"] = "smuggled"
		stampPath := writeJSON(t, dir, "stamp.json", wire)
		before := snapshotDir(t, dir)
		var stdout, stderr bytes.Buffer
		code := run([]string{"--catalog", catalogPath, "--stamp", stampPath}, &stdout, &stderr, now)
		if code != exitToolFailure {
			t.Fatalf("exit code = %d, want %d", code, exitToolFailure)
		}
		assertDirUnchanged(t, dir, before)
	})
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
