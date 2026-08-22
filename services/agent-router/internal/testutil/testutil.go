// Package testutil provides shared test fixtures: locating the repository
// root from within this module (which lives at services/agent-router, a
// subdirectory of the repo the frozen contract and the committed catalog
// live in), extracting the real catalog document the same way
// contracts/agent-router/verify-digests.sh does, and compiling a committed
// JSON Schema for conformance checks. It is not a _test.go file so multiple
// packages' tests can import it.
package testutil

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v5"
	"gopkg.in/yaml.v3"
)

// RepoRoot returns the absolute path to the repository root, located by
// walking up from this source file's own location. Robust to whatever
// directory `go test` happens to run from.
func RepoRoot(t testing.TB) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("testutil: could not determine caller for RepoRoot")
	}
	// this file: services/agent-router/internal/testutil/testutil.go
	dir := filepath.Dir(file)
	root := filepath.Clean(filepath.Join(dir, "..", "..", "..", ".."))
	marker := filepath.Join(root, "contracts", "agent-router", "openapi.yaml")
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("testutil: expected repo root at %q (looking for %q): %v", root, marker, err)
	}
	return root
}

// ExtractCatalogYAML reads the real committed ConfigMap
// (kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml),
// extracts data["catalog.yaml"] exactly as
// contracts/agent-router/verify-digests.sh does, and writes it to a fresh
// temp file. Tests load the router's catalog package against that file so
// conformance is checked against the real catalog, not a hand-copied
// restatement of it that could silently drift.
func ExtractCatalogYAML(t testing.TB) string {
	t.Helper()
	root := RepoRoot(t)
	configMapPath := filepath.Join(root, "kubernetes", "apps", "ai", "agent-router-catalog", "app", "catalog-configmap.yaml")
	raw, err := os.ReadFile(configMapPath)
	if err != nil {
		t.Fatalf("testutil: reading catalog ConfigMap: %v", err)
	}

	var doc struct {
		Data struct {
			Catalog string `yaml:"catalog.yaml"`
		} `yaml:"data"`
	}
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("testutil: parsing catalog ConfigMap: %v", err)
	}
	if doc.Data.Catalog == "" {
		t.Fatal("testutil: catalog ConfigMap has no data[\"catalog.yaml\"]")
	}

	out := filepath.Join(t.TempDir(), "catalog.yaml")
	if err := os.WriteFile(out, []byte(doc.Data.Catalog), 0o600); err != nil {
		t.Fatalf("testutil: writing extracted catalog: %v", err)
	}
	return out
}

// CompileSchema compiles a committed JSON Schema by filename (e.g.
// "status.schema.json") from contracts/agent-router/schemas/, resolving its
// relative $refs against that same directory.
func CompileSchema(t testing.TB, filename string) *jsonschema.Schema {
	t.Helper()
	root := RepoRoot(t)
	path := filepath.Join(root, "contracts", "agent-router", "schemas", filename)
	compiler := jsonschema.NewCompiler()
	schema, err := compiler.Compile(path)
	if err != nil {
		t.Fatalf("testutil: compiling schema %q: %v", filename, err)
	}
	return schema
}

// ValidateJSON validates raw JSON bytes against schema, failing the test
// with the validation error on mismatch.
func ValidateJSON(t testing.TB, schema *jsonschema.Schema, raw []byte) {
	t.Helper()
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("testutil: decoding JSON under validation: %v", err)
	}
	if err := schema.Validate(v); err != nil {
		t.Fatalf("testutil: schema validation failed: %v", err)
	}
}
