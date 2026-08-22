// Command agent-router is the agent-router HTTP service: GET /v1/status and
// POST /v1/capacity/heartbeat in this story's scope (35.9a). See
// contracts/agent-router/openapi.yaml for the frozen API this conforms to.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/auth"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/capacity"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/catalog"
	"github.com/jlengelbrecht/prox-ops/services/agent-router/internal/httpapi"
)

// version and commit are set at build time via
// -ldflags "-X main.version=... -X main.commit=..." (see
// .github/workflows/build-agent-router.yaml). The defaults below are for
// local `go run` only.
var (
	version = "dev"
	commit  = "unknown"
)

const (
	defaultListenAddr               = ":8080"
	defaultHeartbeatIntervalSeconds = 30
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(logger); err != nil {
		logger.Error("agent-router exited with error", "error", err)
		os.Exit(1)
	}
}

func run(logger *slog.Logger) error {
	catalogPath := os.Getenv("AGENT_ROUTER_CATALOG_PATH")
	if catalogPath == "" {
		return errors.New("AGENT_ROUTER_CATALOG_PATH is required")
	}

	listenAddr := envOrDefault("AGENT_ROUTER_LISTEN_ADDR", defaultListenAddr)

	intervalSeconds := defaultHeartbeatIntervalSeconds
	if v := os.Getenv("AGENT_ROUTER_HEARTBEAT_INTERVAL_SECONDS"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 {
			return fmt.Errorf("AGENT_ROUTER_HEARTBEAT_INTERVAL_SECONDS must be a positive integer, got %q", v)
		}
		intervalSeconds = n
	}
	interval := time.Duration(intervalSeconds) * time.Second
	offlineAfter := 3 * interval

	buildVersion := version
	if commit != "" && commit != "unknown" {
		buildVersion = fmt.Sprintf("%s+%s", version, commit)
	}

	cat, digest, loadErr := catalog.Load(catalogPath)
	catalogState := httpapi.CatalogState{Catalog: cat, Digest: digest, Err: loadErr}
	if loadErr != nil {
		logger.Error("catalog load failed; serving degraded until this is fixed", "error", loadErr, "path", catalogPath)
	} else {
		logger.Info("catalog loaded", "digest", digest, "document_version", cat.DocumentVersion, "schema_version", cat.SchemaVersion)
	}

	callerAuth := auth.NewCallerAuth(loadCallerTokens())
	nodeCreds, err := loadNodeCredentials()
	if err != nil {
		return fmt.Errorf("loading node credentials: %w", err)
	}
	nodeAuth := auth.NewNodeAuth(nodeCreds)

	store := capacity.NewStore(nil)

	cfg := httpapi.Config{Version: buildVersion, HeartbeatInterval: interval, OfflineAfter: offlineAfter}
	srv := httpapi.NewServer(cfg, catalogState, store, callerAuth, nodeAuth, logger)

	httpServer := &http.Server{
		Addr:              listenAddr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		logger.Info("agent-router listening", "addr", listenAddr, "version", buildVersion)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
			return
		}
		errCh <- nil
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
	}

	logger.Info("shutting down: signal received")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		return fmt.Errorf("graceful shutdown: %w", err)
	}
	return <-errCh
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func loadCallerTokens() []string {
	var tokens []string
	if v := os.Getenv("AGENT_ROUTER_CALLER_TOKENS"); v != "" {
		for _, t := range strings.Split(v, ",") {
			t = strings.TrimSpace(t)
			if t != "" {
				tokens = append(tokens, t)
			}
		}
	}
	if path := os.Getenv("AGENT_ROUTER_CALLER_TOKEN_FILE"); path != "" {
		raw, err := os.ReadFile(path)
		if err == nil {
			for _, line := range strings.Split(string(raw), "\n") {
				line = strings.TrimSpace(line)
				if line != "" {
					tokens = append(tokens, line)
				}
			}
		}
	}
	return tokens
}

// loadNodeCredentials reads AGENT_ROUTER_NODE_CREDENTIALS_DIR, a mounted
// secret volume with one file per edge node: the filename is the node
// identifier and the file content is that node's bearer token. This is the
// shape a Kubernetes Secret volume mount produces directly, so 35.9b needs
// no translation layer between an ExternalSecret and this router. Entries
// whose name starts with ".." are the atomic-writer bookkeeping symlinks
// kubelet creates alongside the real keys and are skipped.
func loadNodeCredentials() (map[string]string, error) {
	dir := os.Getenv("AGENT_ROUTER_NODE_CREDENTIALS_DIR")
	creds := map[string]string{}
	if dir == "" {
		return creds, nil
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("reading %q: %w", dir, err)
	}
	for _, e := range entries {
		if e.IsDir() || strings.HasPrefix(e.Name(), "..") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("reading credential file %q: %w", e.Name(), err)
		}
		token := strings.TrimSpace(string(raw))
		if token != "" {
			creds[token] = e.Name()
		}
	}
	return creds, nil
}
