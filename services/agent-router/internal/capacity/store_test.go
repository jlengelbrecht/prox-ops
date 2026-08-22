package capacity_test

import (
	"testing"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
)

func TestResolve_Unseen(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })

	res := store.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Source != capacity.SourceUnseen {
		t.Errorf("Source = %q, want %q", res.Source, capacity.SourceUnseen)
	}
	if res.State != heartbeat.StateOffline {
		t.Errorf("State = %q, want %q", res.State, heartbeat.StateOffline)
	}
	if res.Eligible {
		t.Error("Eligible = true, want false for an unseen node")
	}
	if res.Heartbeat != nil || res.LastHeartbeat != nil {
		t.Error("Heartbeat/LastHeartbeat must both be nil for an unseen node")
	}
}

func TestResolve_HeartbeatAvailable(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })

	store.Record(heartbeat.Heartbeat{Node: "cachyos-7900xtx", State: heartbeat.StateAvailable, ClusterReachable: true})

	res := store.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Source != capacity.SourceHeartbeat {
		t.Errorf("Source = %q, want %q", res.Source, capacity.SourceHeartbeat)
	}
	if !res.Eligible {
		t.Error("Eligible = false, want true for an AVAILABLE node within the interval")
	}
	if res.Heartbeat == nil {
		t.Fatal("Heartbeat is nil, want the recorded heartbeat")
	}
}

// TestResolve_StaleGoesOffline is the "stale -> OFFLINE at 3x interval"
// acceptance case: a node that reported once and then went silent for the
// offline threshold must resolve to OFFLINE/silence/ineligible, while
// retaining the prior heartbeat and its receipt time.
func TestResolve_StaleGoesOffline(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })
	offlineAfter := 90 * time.Second

	store.Record(heartbeat.Heartbeat{Node: "cachyos-7900xtx", State: heartbeat.StateAvailable, ClusterReachable: true})

	// Just under the threshold: still heartbeat-sourced.
	clock = clock.Add(89 * time.Second)
	res := store.Resolve("cachyos-7900xtx", offlineAfter)
	if res.Source != capacity.SourceHeartbeat {
		t.Errorf("at 89s: Source = %q, want %q", res.Source, capacity.SourceHeartbeat)
	}

	// At/over the threshold: silence.
	clock = clock.Add(2 * time.Second) // total 91s
	res = store.Resolve("cachyos-7900xtx", offlineAfter)
	if res.Source != capacity.SourceSilence {
		t.Errorf("at 91s: Source = %q, want %q", res.Source, capacity.SourceSilence)
	}
	if res.State != heartbeat.StateOffline {
		t.Errorf("at 91s: State = %q, want %q", res.State, heartbeat.StateOffline)
	}
	if res.Eligible {
		t.Error("at 91s: Eligible = true, want false")
	}
	if res.Heartbeat == nil || res.LastHeartbeat == nil {
		t.Error("silence must retain the prior heartbeat and its receipt time")
	}
}

// TestRestart_ReLearns is the "restart re-learns" acceptance case: a fresh
// Store (representing a router restart) has no memory of a node a prior
// process instance recorded, and correctly reports it unseen rather than
// carrying stale state forward.
func TestRestart_ReLearns(t *testing.T) {
	clock := time.Now()
	before := capacity.NewStore(func() time.Time { return clock })
	before.Record(heartbeat.Heartbeat{Node: "cachyos-7900xtx", State: heartbeat.StateAvailable, ClusterReachable: true})
	if res := before.Resolve("cachyos-7900xtx", 90*time.Second); res.Source != capacity.SourceHeartbeat {
		t.Fatalf("pre-restart Source = %q, want %q", res.Source, capacity.SourceHeartbeat)
	}

	after := capacity.NewStore(func() time.Time { return clock })
	res := after.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Source != capacity.SourceUnseen {
		t.Errorf("post-restart Source = %q, want %q (in-memory state must not survive a restart)", res.Source, capacity.SourceUnseen)
	}
	if after.Uptime() != 0 {
		t.Errorf("post-restart Uptime = %v, want 0 immediately after construction", after.Uptime())
	}
}

func TestResolve_InteractiveIsIneligible(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })
	store.Record(heartbeat.Heartbeat{Node: "cachyos-7900xtx", State: heartbeat.StateInteractive, Interactive: true, ClusterReachable: true})

	res := store.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Eligible {
		t.Error("Eligible = true, want false for an INTERACTIVE node")
	}
	if res.State != heartbeat.StateInteractive {
		t.Errorf("State = %q, want %q", res.State, heartbeat.StateInteractive)
	}
}

func TestResolve_DrainingIsIneligible(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })
	store.Record(heartbeat.Heartbeat{Node: "cachyos-7900xtx", State: heartbeat.StateDraining, ClusterReachable: true})

	res := store.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Eligible {
		t.Error("Eligible = true, want false for a DRAINING node")
	}
}

// A node that reports it cannot reach the cluster has narrowed itself, and
// the router must honour that - observations subtract. Without this the node
// stays advertised as eligible while being unable to serve, which is the
// failure mode the intersect rule exists to prevent.
//
// The contrast case matters: the SAME node, same AVAILABLE state, differing
// only in cluster_reachable, must be eligible. Otherwise this test would
// still pass if eligibility were broken for an unrelated reason.
func TestResolve_UnreachableIsIneligible(t *testing.T) {
	clock := time.Now()
	store := capacity.NewStore(func() time.Time { return clock })

	store.Record(heartbeat.Heartbeat{
		Node:             "cachyos-7900xtx",
		State:            heartbeat.StateAvailable,
		ClusterReachable: false,
	})
	res := store.Resolve("cachyos-7900xtx", 90*time.Second)
	if res.Eligible {
		t.Error("Eligible = true for a node reporting cluster_reachable:false; want false")
	}
	if res.State != heartbeat.StateAvailable {
		t.Errorf("State = %q, want the reported AVAILABLE preserved", res.State)
	}

	store.Record(heartbeat.Heartbeat{
		Node:             "cachyos-7900xtx",
		State:            heartbeat.StateAvailable,
		ClusterReachable: true,
	})
	if res := store.Resolve("cachyos-7900xtx", 90*time.Second); !res.Eligible {
		t.Error("Eligible = false once the node reports cluster_reachable:true again; want true")
	}
}
