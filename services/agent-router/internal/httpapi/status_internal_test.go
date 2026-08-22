package httpapi

import (
	"testing"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
)

// TestReasonCodeFor_HeartbeatingButNotSelectable covers the case a real
// catalog fixture cannot easily construct: an edge placement whose node is
// alive and heartbeating AVAILABLE (or SERVING), but whose profile physical
// candidate is ineligible only because the placement itself is not
// selectable in the catalog. reasonCodeFor must not call that "offline" -
// the node is not offline, it is not selectable.
func TestReasonCodeFor_HeartbeatingButNotSelectable(t *testing.T) {
	for _, state := range []string{heartbeat.StateAvailable, heartbeat.StateServing} {
		res := placementResolution{source: "heartbeat", state: state}
		if got := reasonCodeFor(res); got != "not_selectable" {
			t.Errorf("reasonCodeFor(%+v) = %q, want not_selectable", res, got)
		}
	}
}

func TestReasonCodeFor_OtherCases(t *testing.T) {
	cases := []struct {
		res  placementResolution
		want string
	}{
		{placementResolution{source: "unseen"}, "not_yet_observed"},
		{placementResolution{source: "silence"}, "offline"},
		{placementResolution{source: "heartbeat", state: heartbeat.StateInteractive}, "withdrawn_interactive"},
		{placementResolution{source: "heartbeat", state: heartbeat.StateDraining}, "withdrawn_draining"},
		{placementResolution{source: "heartbeat", state: heartbeat.StateOffline}, "offline"},
		{placementResolution{source: "static"}, "not_selectable"},
	}
	for _, c := range cases {
		if got := reasonCodeFor(c.res); got != c.want {
			t.Errorf("reasonCodeFor(%+v) = %q, want %q", c.res, got, c.want)
		}
	}
}

// An alive node that reports it cannot reach the cluster is ineligible, but
// it is not "not_selectable" - the catalog does select it. Reporting the
// wrong code here would send an operator looking at catalog configuration
// for a problem that is entirely on the node side.
func TestReasonCodeFor_UnreachableIsOfflineNotNotSelectable(t *testing.T) {
	unreachable := &heartbeat.Heartbeat{State: heartbeat.StateAvailable, ClusterReachable: false}
	got := reasonCodeFor(placementResolution{
		eligible:  false,
		source:    "heartbeat",
		state:     heartbeat.StateAvailable,
		heartbeat: unreachable,
	})
	if got != "offline" {
		t.Errorf("reasonCodeFor(unreachable) = %q, want offline", got)
	}

	// Contrast: an alive, reachable node that is ineligible really is a
	// catalog-selectability problem, and must still say so.
	reachable := &heartbeat.Heartbeat{State: heartbeat.StateAvailable, ClusterReachable: true}
	if got := reasonCodeFor(placementResolution{
		eligible:  false,
		source:    "heartbeat",
		state:     heartbeat.StateAvailable,
		heartbeat: reachable,
	}); got != "not_selectable" {
		t.Errorf("reasonCodeFor(reachable but ineligible) = %q, want not_selectable", got)
	}
}
