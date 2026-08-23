package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"regexp"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/stampvalidate"
)

var catalogVersionPattern = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Frozen CLI exit semantics (amendment 7):
const (
	exitValid         = 0 // valid; Verdict JSON on stdout
	exitPolicyInvalid = 1 // well-formed but policy-invalid; Verdict JSON on stdout
	exitToolFailure   = 2 // malformed input/catalog/tool failure; no execution
)

// AGENT_STAMP_VALIDATE_CATALOG_PATH names the catalog file this CLI trusts.
// Owner clarification 2 (2026-08-23 dispatch): the catalog is trusted policy
// material. It is ALWAYS installer/launcher configuration - an env var here,
// or the --catalog flag Orca's launcher is configured with - and is NEVER
// accepted from the stamp file or from stdin. A workload cannot choose its
// own policy authority.
const catalogPathEnv = "AGENT_STAMP_VALIDATE_CATALOG_PATH"

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, time.Now().UTC()))
}

// run is the CLI's whole orchestration, factored out of main so tests can
// drive it without os/exec (forbidden in this package - see the boundary
// grep this story's gate 5 runs) and without depending on the real
// wall-clock. now is the ONLY place this binary reads a clock; ValidateFinal
// itself never does (STORY-035-12 "Determinism and purity").
func run(args []string, stdout, stderr io.Writer, now time.Time) int {
	fs := flag.NewFlagSet("agent-stamp-validate", flag.ContinueOnError)
	fs.SetOutput(stderr)
	catalogPath := fs.String("catalog", "", "path to the trusted catalog document (defaults to "+catalogPathEnv+")")
	stampPath := fs.String("stamp", "", "path to the execution-stamp JSON document (required)")
	evidencePath := fs.String("evidence", "", "path to the optional placement-evidence JSON document")
	if err := fs.Parse(args); err != nil {
		// flag already printed usage/error to stderr.
		return exitToolFailure
	}

	if *catalogPath == "" {
		*catalogPath = os.Getenv(catalogPathEnv)
	}
	if *catalogPath == "" {
		fmt.Fprintln(stderr, "agent-stamp-validate: --catalog is required (or set "+catalogPathEnv+"); it is never taken from the stamp or from stdin")
		return exitToolFailure
	}
	if *stampPath == "" {
		fmt.Fprintln(stderr, "agent-stamp-validate: --stamp is required")
		return exitToolFailure
	}

	cat, _, err := catalog.Load(*catalogPath)
	if err != nil {
		fmt.Fprintf(stderr, "agent-stamp-validate: loading catalog %q: %v\n", *catalogPath, err)
		return exitToolFailure
	}

	stampRaw, err := os.ReadFile(*stampPath)
	if err != nil {
		fmt.Fprintf(stderr, "agent-stamp-validate: reading stamp %q: %v\n", *stampPath, err)
		return exitToolFailure
	}
	stamp, ierr := parseStamp(stampRaw)
	if ierr != nil {
		fmt.Fprintf(stderr, "agent-stamp-validate: malformed stamp: %v\n", ierr)
		return exitToolFailure
	}

	vctx := newValidationContext()
	if *evidencePath != "" {
		evidenceRaw, err := os.ReadFile(*evidencePath)
		if err != nil {
			fmt.Fprintf(stderr, "agent-stamp-validate: reading evidence %q: %v\n", *evidencePath, err)
			return exitToolFailure
		}
		evidence, ierr := parseEvidence(evidenceRaw)
		if ierr != nil {
			fmt.Fprintf(stderr, "agent-stamp-validate: malformed placement evidence: %v\n", ierr)
			return exitToolFailure
		}
		vctx.PlacementEvidence = evidence
	}

	verdict := stampvalidate.ValidateFinal(cat, stamp, vctx, now)

	enc := json.NewEncoder(stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(toVerdictWire(verdict)); err != nil {
		fmt.Fprintf(stderr, "agent-stamp-validate: encoding verdict: %v\n", err)
		return exitToolFailure
	}

	if !verdict.Valid {
		return exitPolicyInvalid
	}
	return exitValid
}

// newValidationContext builds the ValidationContext this CLI supplies to
// ValidateFinal. It is a fixed, zero-elevation construction: there is no
// flag, no environment variable, and no code path anywhere in this binary
// that can set MeteredSpendAuthorized to true (amendment 3 / owner-frozen
// MVP scope: "the CLI has no --allow-metered, --authorized, or any
// caller-settable elevation flag"). Every metered stamp therefore fails
// closed under this CLI, by construction - not by a check that could be
// disabled, but because nothing in this function's signature or body can
// produce a true value. A later identity story may add a trusted adapter
// that constructs an authorized ValidationContext some other way; it must
// not do so by adding a flag here.
func newValidationContext() stampvalidate.ValidationContext {
	return stampvalidate.ValidationContext{}
}
