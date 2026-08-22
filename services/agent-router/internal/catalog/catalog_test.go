package catalog_test

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/testutil"
)

// TestLoad_RealCatalog proves the router conforms to the committed catalog
// (contracts/agent-router/README.md documents its real digest as
// sha256:2fd681e0...60e229), not a hand-copied restatement of it.
func TestLoad_RealCatalog(t *testing.T) {
	path := testutil.ExtractCatalogYAML(t)

	cat, digest, err := catalog.Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	const wantDigest = "sha256:2fd681e0e988bf6be94b8923b1485a0aa174a4e66aec580a2d383c764b60e229"
	if digest != wantDigest {
		t.Errorf("digest = %q, want %q (contracts/agent-router/README.md digest table)", digest, wantDigest)
	}
	if cat.DocumentVersion != "1.3.0" {
		t.Errorf("DocumentVersion = %q, want 1.3.0", cat.DocumentVersion)
	}
	if cat.SchemaVersion != 1 {
		t.Errorf("SchemaVersion = %d, want 1", cat.SchemaVersion)
	}

	// Order preservation: harnesses/placements/pools/profiles/policies must
	// render in the catalog document's own table order.
	wantHarnesses := []string{"claude", "codex", "devin", "local-agent"}
	if got := names(cat.Harnesses); !equal(got, wantHarnesses) {
		t.Errorf("harness order = %v, want %v", got, wantHarnesses)
	}
	wantPlacements := []string{"kserve-a5000", "cachyos-7900xtx", "bazzite-5090", "laptop-rtx5000"}
	if got := names(cat.Placements); !equal(got, wantPlacements) {
		t.Errorf("placement order = %v, want %v", got, wantPlacements)
	}
}

// TestLoad_NonSelectableProfiles is the "non-selectable-profile asymmetry"
// conformance case the story calls out by name: local-code-fast and
// local-unrestricted must render selectable: false with zero candidates,
// empty capabilities and a null min_context - visibly non-selectable, not
// silently omitted and not indistinguishable from a selectable profile.
func TestLoad_NonSelectableProfiles(t *testing.T) {
	path := testutil.ExtractCatalogYAML(t)
	cat, _, err := catalog.Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	for _, name := range []string{"local-code-fast", "local-unrestricted"} {
		p, ok := findProfile(cat, name)
		if !ok {
			t.Fatalf("profile %q not found in catalog", name)
		}
		if p.Selectable {
			t.Errorf("profile %q: Selectable = true, want false", name)
		}
		if len(p.Physical) != 0 {
			t.Errorf("profile %q: Physical = %v, want empty", name, p.Physical)
		}
		if len(p.Capabilities) != 0 {
			t.Errorf("profile %q: Capabilities = %v, want empty", name, p.Capabilities)
		}
		if p.MinContext != nil {
			t.Errorf("profile %q: MinContext = %v, want nil", name, *p.MinContext)
		}
	}

	// local-code-standard remains selectable with candidates, so the
	// asymmetry is between profiles, not a blanket property of the catalog.
	standard, ok := findProfile(cat, "local-code-standard")
	if !ok {
		t.Fatal("profile local-code-standard not found")
	}
	if !standard.Selectable {
		t.Error("profile local-code-standard: Selectable = false, want true")
	}
	if len(standard.Physical) == 0 {
		t.Error("profile local-code-standard: Physical is empty, want candidates")
	}
}

func TestLoad_ModelAuthorizedOnPlacement(t *testing.T) {
	path := testutil.ExtractCatalogYAML(t)
	cat, _, err := catalog.Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	if !cat.ModelAuthorizedOnPlacement("qwen36-27b", "cachyos-7900xtx") {
		t.Error("qwen36-27b should be authorized on cachyos-7900xtx per the committed catalog")
	}
	if cat.ModelAuthorizedOnPlacement("qwen36-27b", "bazzite-5090") {
		t.Error("qwen36-27b should not be authorized on bazzite-5090")
	}
	if cat.ModelAuthorizedOnPlacement("not-a-real-model", "cachyos-7900xtx") {
		t.Error("an unknown model must never be authorized anywhere")
	}
}

func TestLoad_ParseFailure(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "catalog.yaml")
	if err := os.WriteFile(path, []byte("not: [valid: yaml"), 0o600); err != nil {
		t.Fatal(err)
	}

	_, _, err := catalog.Load(path)
	if err == nil {
		t.Fatal("Load: expected an error for malformed YAML, got nil")
	}
	var unsupported *catalog.UnsupportedSchemaError
	if errors.As(err, &unsupported) {
		t.Fatalf("Load: got UnsupportedSchemaError for malformed YAML, want a plain parse error: %v", err)
	}
}

func TestLoad_MissingFile(t *testing.T) {
	_, _, err := catalog.Load(filepath.Join(t.TempDir(), "does-not-exist.yaml"))
	if err == nil {
		t.Fatal("Load: expected an error for a missing file, got nil")
	}
}

func TestLoad_UnsupportedSchemaVersion(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "catalog.yaml")
	doc := "version: \"9.9.9\"\nschema_version: 2\ndefaults:\n  placement_policy: prefer-warm-local\nharnesses: {}\nplacements: {}\nmodels: {}\nentitlement_pools: {}\nprofiles: {}\npolicies:\n  placement_policy: {}\n"
	if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
		t.Fatal(err)
	}

	_, _, err := catalog.Load(path)
	if err == nil {
		t.Fatal("Load: expected an error for an unsupported schema_version, got nil")
	}
	var unsupported *catalog.UnsupportedSchemaError
	if !errors.As(err, &unsupported) {
		t.Fatalf("Load: got %v (%T), want *catalog.UnsupportedSchemaError", err, err)
	}
	if unsupported.Received != 2 || unsupported.Supported != 1 {
		t.Errorf("UnsupportedSchemaError = %+v, want Received=2 Supported=1", unsupported)
	}
}

func names[V any](entries []catalog.Entry[V]) []string {
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = e.Name
	}
	return out
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func findProfile(cat *catalog.Catalog, name string) (catalog.Profile, bool) {
	for _, e := range cat.Profiles {
		if e.Name == name {
			return e.Value, true
		}
	}
	return catalog.Profile{}, false
}
