#!/usr/bin/env bash
set -euo pipefail

image="${1:?image reference required}"
token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker run -d --name broker -p 127.0.0.1:8000:8000 \
  -e OP_CONNECT_HOST=https://connect.invalid \
  -e OP_CONNECT_TOKEN=unused \
  -e OP_BOUND_VAULT=AI-Access \
  -e MCP_CALLER_TOKEN="$token" \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  "$image"
trap 'docker rm -f broker >/dev/null' EXIT
for _ in $(seq 30); do
  curl -fsS http://127.0.0.1:8000/health && break
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health
status="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"
test "$status" = 401
curl -fsS -X POST http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | grep -q create_generated_item
