package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// Test_ExampleFixtures_ConformToSchemas proves every committed
// examples/stamp/*.stamp.json and *.evidence.json fixture validates against
// the frozen schemas it claims to be (gate 1: "contract artifacts exist,
// schemas closed, fixtures validate").
func Test_ExampleFixtures_ConformToSchemas(t *testing.T) {
	root := testutil.RepoRoot(t)
	dir := filepath.Join(root, "contracts", "agent-router", "examples", "stamp")

	stampSchema := testutil.CompileSchema(t, "execution-stamp.schema.json")
	evidenceSchema := testutil.CompileSchema(t, "placement-evidence.schema.json")

	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("reading %q: %v", dir, err)
	}
	if len(entries) == 0 {
		t.Fatal("no fixtures found in examples/stamp/")
	}

	var stampCount, evidenceCount int
	for _, e := range entries {
		name := e.Name()
		path := filepath.Join(dir, name)
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("reading %q: %v", path, err)
		}
		switch {
		case strings.HasSuffix(name, ".stamp.json"):
			stampCount++
			t.Run(name, func(t *testing.T) { testutil.ValidateJSON(t, stampSchema, raw) })
		case strings.HasSuffix(name, ".evidence.json"):
			evidenceCount++
			t.Run(name, func(t *testing.T) { testutil.ValidateJSON(t, evidenceSchema, raw) })
		default:
			t.Errorf("examples/stamp/%s does not match *.stamp.json or *.evidence.json - every fixture must be nameable as one or the other", name)
		}
	}
	if stampCount == 0 {
		t.Error("no *.stamp.json fixtures found")
	}
	if evidenceCount == 0 {
		t.Error("no *.evidence.json fixtures found")
	}
}

// Test_Verdict_ConformsToSchema proves the CLI's actual stdout - on both the
// valid and the policy-invalid path - validates against
// validation-verdict.schema.json.
func Test_Verdict_ConformsToSchema(t *testing.T) {
	root := testutil.RepoRoot(t)
	dir := filepath.Join(root, "contracts", "agent-router", "examples", "stamp")
	catalogPath := realCatalogPath(t)
	verdictSchema := testutil.CompileSchema(t, "validation-verdict.schema.json")
	now := fixedNow(t)

	cases := []struct {
		name     string
		stamp    string
		wantExit int
	}{
		{"valid", "valid-router-stamp.stamp.json", exitValid},
		{"policy_invalid", "invalid-pairing.stamp.json", exitPolicyInvalid},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			code := run([]string{"--catalog", catalogPath, "--stamp", filepath.Join(dir, c.stamp)}, &stdout, &stderr, now)
			if code != c.wantExit {
				t.Fatalf("exit code = %d, want %d: stderr=%s", code, c.wantExit, stderr.String())
			}
			testutil.ValidateJSON(t, verdictSchema, stdout.Bytes())
		})
	}
}
