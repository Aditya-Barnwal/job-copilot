#!/usr/bin/env bash
# Build the public demo image locally, run it the way Cloud Run does (PORT=8080,
# demo mode baked in), smoke-test it, then clean up. Exit 0 only if every check
# passes. Uses podman or docker, whichever is available (podman first).
#
#   scripts/demo_check.sh            # or: make demo-check
set -euo pipefail
cd "$(dirname "$0")/.."

ENGINE="${CONTAINER_ENGINE:-}"
if [[ -z "$ENGINE" ]]; then
  if command -v podman >/dev/null 2>&1; then ENGINE=podman
  elif command -v docker >/dev/null 2>&1; then ENGINE=docker
  else echo "demo-check: need podman or docker" >&2; exit 2; fi
fi
if [[ "$ENGINE" == podman ]] && ! podman info >/dev/null 2>&1; then
  echo "demo-check: starting the podman machine..."; podman machine start >/dev/null
fi

IMAGE="job-copilot-demo:check"
NAME="jc-demo-check"
HOST_PORT="${DEMO_CHECK_PORT:-8011}"
BASE="http://127.0.0.1:${HOST_PORT}"
fail=0
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; fail=1; }
cleanup() { "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "demo-check: building $IMAGE with ${ENGINE}..."
"$ENGINE" build -q --build-arg REQS=requirements-demo.txt --build-arg DEMO=1 \
  --build-arg TORCH_INDEX= -t "$IMAGE" . >/dev/null

size=$("$ENGINE" image inspect "$IMAGE" --format '{{.Size}}')
mb=$(( size / 1000000 ))
(( mb < 400 )) && pass "image size ${mb} MB (< 400 MB)" || bad "image size ${mb} MB (>= 400 MB)"

leaks=$("$ENGINE" run --rm --entrypoint sh "$IMAGE" -c \
  'cd /app; for p in profile .env data/jobcopilot.db; do [ -e "$p" ] && echo "$p"; done;
   find / -xdev \( -name "*.db" -o -name "*.pdf" -o -name ".env" \) -not -path "/proc/*" 2>/dev/null | head -3;
   python -c "import importlib.util as u, sys; sys.stdout.write(\"torch\" if u.find_spec(\"torch\") else \"\")"' || true)
[[ -z "$leaks" ]] && pass "no personal files, databases or torch in the image" || bad "image contains: $leaks"

cleanup
"$ENGINE" run -d --name "$NAME" -p "127.0.0.1:${HOST_PORT}:8080" -e PORT=8080 "$IMAGE" >/dev/null
for _ in $(seq 1 60); do curl -fs -o /dev/null "$BASE/api/meta" && break; sleep 1; done

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
expect() {  # expect <code> <label> <curl args...>
  local want="$1" label="$2"; shift 2
  local got; got=$(code "$@")
  [[ "$got" == "$want" ]] && pass "$label -> $got" || bad "$label -> $got (wanted $want)"
}
expect 200 "GET /" "$BASE/"
curl -s "$BASE/" | grep -q 'Demo — sample data' && pass "demo banner shown" || bad "demo banner missing"
expect 200 "GET /api/meta" "$BASE/api/meta"
expect 200 "GET /api/queue" "$BASE/api/queue?lane=main"
expect 200 "GET /api/matches" "$BASE/api/matches?limit=5"
expect 200 "GET /api/excluded" "$BASE/api/excluded?limit=5"
expect 200 "GET /api/tracker" "$BASE/api/tracker"
expect 200 "GET /api/runs" "$BASE/api/runs"
expect 403 "POST /api/ingest (blocked in demo)" -X POST -H 'Content-Type: application/json' -d '{}' "$BASE/api/ingest"
n=$(curl -s "$BASE/api/queue?lane=main" | python3 -I -c \
  'import json,sys; d=json.load(sys.stdin); print(len(d.get("items", d) if isinstance(d, dict) else d))' 2>/dev/null || echo 0)
(( n > 0 )) && pass "queue has $n sample jobs" || bad "queue is empty"
prov=$(curl -s "$BASE/api/llm/health" | python3 -I -c 'import json,sys; print(json.load(sys.stdin).get("provider"))' 2>/dev/null || echo "?")
[[ "$prov" == "demo" ]] && pass "LLM disabled (provider=demo)" || bad "LLM provider is '$prov', expected demo"

if (( fail )); then echo "demo-check: FAILED"; exit 1; fi
echo "demo-check: all checks passed"
