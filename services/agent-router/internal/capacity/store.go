// Package capacity is the router's in-memory capacity table: the most
// recent heartbeat observed from each edge node, and how to interpret it
// against the router's heartbeat policy. No database, no Redis, no
// persistence, no CRD - a restart re-learns capacity within one heartbeat
// interval, and that is the intended behaviour (EPIC-035 section 4).
package capacity

import (
	"sync"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/heartbeat"
)

// Record is one node's most recently observed heartbeat, plus the router's
// own receipt time for it - distinct from the heartbeat's self-reported
// last_heartbeat, which is stamped by the edge host, not the router
// (heartbeat.schema.json).
type Record struct {
	Heartbeat  heartbeat.Heartbeat
	ReceivedAt time.Time
}

// Store is the in-memory node id -> most recent heartbeat table.
type Store struct {
	mu      sync.RWMutex
	records map[string]*Record
	started time.Time
	now     func() time.Time
}

// NewStore creates an empty capacity store. now defaults to time.Now when
// nil; tests supply an injectable clock so stale/restart behaviour can be
// exercised without sleeping.
func NewStore(now func() time.Time) *Store {
	if now == nil {
		now = time.Now
	}
	return &Store{
		records: make(map[string]*Record),
		started: now(),
		now:     now,
	}
}

// Record stores node's heartbeat, stamped with the router's receipt time.
// A later heartbeat from the same node replaces the earlier one; there is
// no history, only the most recent observation.
func (s *Store) Record(hb heartbeat.Heartbeat) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.records[hb.Node] = &Record{Heartbeat: hb, ReceivedAt: s.now()}
}

// Get returns the most recent record for node, if any has been observed in
// this store's lifetime.
func (s *Store) Get(node string) (Record, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	r, ok := s.records[node]
	if !ok {
		return Record{}, false
	}
	return *r, true
}

// Uptime is how long this store (and therefore this router process) has
// been running. Drives router.capacity_state: learning for less than one
// heartbeat interval, steady afterwards.
func (s *Store) Uptime() time.Duration {
	return s.now().Sub(s.started)
}

// Now returns the store's current time, so callers building a response
// alongside store data use the same clock (real or injected).
func (s *Store) Now() time.Time {
	return s.now()
}

// Source categorizes where a placement's rendered state comes from
// (contracts/agent-router/schemas/status.schema.json placements[].state_source).
type Source string

const (
	// SourceHeartbeat: the current state came from an observed heartbeat.
	SourceHeartbeat Source = "heartbeat"
	// SourceSilence: the node previously reported and then exceeded the
	// offline threshold. The prior heartbeat is retained.
	SourceSilence Source = "silence"
	// SourceUnseen: a heartbeat channel exists and none has been observed
	// yet in this router process's lifetime.
	SourceUnseen Source = "unseen"
)

// Resolved is the router's live interpretation of one enrolled edge node's
// state, derived from the capacity store.
type Resolved struct {
	Source        Source
	State         string // AVAILABLE|SERVING|DRAINING|INTERACTIVE|OFFLINE
	Eligible      bool
	Heartbeat     *heartbeat.Heartbeat
	LastHeartbeat *time.Time
}

// Resolve interprets the capacity store's record for an enrolled edge node
// (catalog kind: edge, status: available) against offlineAfter, the router's
// silence threshold (3x the heartbeat interval).
//
// This is the narrowing half of "a heartbeat may narrow static authority;
// it may never expand it": the catalog says the placement exists and is
// selectable, and only a live, recent, non-withdrawn heartbeat can make it
// eligible. INTERACTIVE and DRAINING narrow eligibility to false
// immediately on receipt, with no timeout (README "Withdrawal semantics"
// mechanism 1 of 3); silence narrows it after offlineAfter (mechanism 2).
func (s *Store) Resolve(node string, offlineAfter time.Duration) Resolved {
	rec, ok := s.Get(node)
	if !ok {
		return Resolved{Source: SourceUnseen, State: heartbeat.StateOffline, Eligible: false}
	}
	hb := rec.Heartbeat
	receivedAt := rec.ReceivedAt
	if s.now().Sub(receivedAt) >= offlineAfter {
		return Resolved{
			Source:        SourceSilence,
			State:         heartbeat.StateOffline,
			Eligible:      false,
			Heartbeat:     &hb,
			LastHeartbeat: &receivedAt,
		}
	}
	eligible := hb.State == heartbeat.StateAvailable || hb.State == heartbeat.StateServing
	return Resolved{
		Source:        SourceHeartbeat,
		State:         hb.State,
		Eligible:      eligible,
		Heartbeat:     &hb,
		LastHeartbeat: &receivedAt,
	}
}
