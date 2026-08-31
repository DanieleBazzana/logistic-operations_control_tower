#!/usr/bin/env bash
# Full local M07 gate: build, migrate, bootstrap twice, smoke both services,
# verify the public mutation boundary, development lifecycle behavior, persistence,
# and export. It is intentionally local and disposable.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="control-tower-m07-${$}"
COMPOSE=(docker compose --project-name "$PROJECT" --file "${ROOT_DIR}/docker-compose.yml")
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/control-tower-m07.XXXXXX")"
API_PID=""

fail() { printf 'M07 production gate failed: %s\n' "$1" >&2; exit 1; }
cleanup() {
    set +e
    "${COMPOSE[@]}" down --volumes >/dev/null 2>&1
    rm -rf "$RUN_DIR"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || fail "docker is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
"${COMPOSE[@]}" version >/dev/null 2>&1 || fail "Docker Compose is unavailable"
[[ -x "$ROOT_DIR/.venv/bin/python" ]] || fail ".venv/bin/python is missing"
[[ -n "${POSTGRES_PASSWORD:-}" ]] || fail "POSTGRES_PASSWORD must be exported explicitly"
[[ "${ALLOW_DESTRUCTIVE_TEST_DB:-}" == "1" ]] || fail "ALLOW_DESTRUCTIVE_TEST_DB=1 is required"
[[ -n "${TEST_DATABASE_URL:-}" ]] || fail "TEST_DATABASE_URL must be exported explicitly"

TARGET="$($ROOT_DIR/.venv/bin/python -c '
import os, sys
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
try: url = make_url(os.environ["TEST_DATABASE_URL"])
except (ArgumentError, ValueError, KeyError): sys.exit("invalid TEST_DATABASE_URL")
if url.get_backend_name() != "postgresql" or url.query: sys.exit("TEST_DATABASE_URL must be query-free PostgreSQL")
if (url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}: sys.exit("TEST_DATABASE_URL must target loopback")
if url.database != "control_tower_m04" and not (url.database or "").endswith("_test"): sys.exit("TEST_DATABASE_URL must name a disposable database")
if url.password != os.environ["POSTGRES_PASSWORD"]: sys.exit("TEST_DATABASE_URL password mismatch")
print("\t".join((url.database or "", url.username or "", str(url.port or 5432))))
' 2>"$RUN_DIR/url-error")" || fail "$(tr '\n' ' ' < "$RUN_DIR/url-error")"
IFS=$'\t' read -r TEST_DB TEST_USER TEST_PORT <<< "$TARGET"
export POSTGRES_DB="$TEST_DB" POSTGRES_USER="$TEST_USER" POSTGRES_PORT="${POSTGRES_PORT:-$TEST_PORT}"
export API_PORT="${API_PORT:-8000}" DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"

"${COMPOSE[@]}" build api dashboard >"$RUN_DIR/build.log" 2>&1 || fail "production image build failed"
"${COMPOSE[@]}" up -d postgres >"$RUN_DIR/postgres.log" 2>&1 || fail "PostgreSQL startup failed"
"${COMPOSE[@]}" run --rm migrate >"$RUN_DIR/migrate.log" 2>&1 || fail "migration failed"
"${COMPOSE[@]}" run --rm bootstrap >"$RUN_DIR/bootstrap-one.log" 2>&1 || fail "bootstrap failed"
"${COMPOSE[@]}" run --rm bootstrap >"$RUN_DIR/bootstrap-two.log" 2>&1 || fail "repeat bootstrap failed"

export PUBLIC_DEMO_READ_ONLY=true
"${COMPOSE[@]}" up -d api dashboard >"$RUN_DIR/services.log" 2>&1 || fail "API/dashboard startup failed"
API_BASE="http://127.0.0.1:${API_PORT}/api/v1"
DASHBOARD_BASE="http://127.0.0.1:${DASHBOARD_PORT}"
for ((attempt=1; attempt<=60; attempt++)); do
    curl --silent --show-error --fail "$API_BASE/readyz" >"$RUN_DIR/ready.json" 2>/dev/null && break
    sleep 1
done
curl --silent --show-error --fail "$API_BASE/livez" >"$RUN_DIR/live.json" || fail "API liveness failed"
curl --silent --show-error --fail "$API_BASE/readyz" >"$RUN_DIR/ready-final.json" || fail "API readiness failed"
curl --silent --show-error --fail "$API_BASE/kpis/summary" >"$RUN_DIR/kpi.json" || fail "KPI retrieval failed"
curl --silent --show-error --fail "$API_BASE/exceptions?page_size=1" >"$RUN_DIR/queue.json" || fail "exception queue failed"
EXCEPTION_ID="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; items=json.load(open(sys.argv[1]))["items"]; print(items[0]["id"] if items else "")' "$RUN_DIR/queue.json")"
[[ -n "$EXCEPTION_ID" ]] || fail "exception queue was empty"
curl --silent --show-error --fail "$API_BASE/exceptions/$EXCEPTION_ID" >"$RUN_DIR/before.json" || fail "exception detail failed"
cp "$RUN_DIR/before.json" "$RUN_DIR/before-copy.json"
printf '%s' '{"status":"ACKNOWLEDGED","actor":"public-m07"}' >"$RUN_DIR/public-patch.json"
PUBLIC_STATUS="$(curl --silent --output "$RUN_DIR/public-response.json" --write-out '%{http_code}' -X PATCH -H 'content-type: application/json' --data-binary @"$RUN_DIR/public-patch.json" "$API_BASE/exceptions/$EXCEPTION_ID/status")"
[[ "$PUBLIC_STATUS" == "403" ]] || fail "public lifecycle mutation returned $PUBLIC_STATUS"
curl --silent --show-error --fail "$API_BASE/exceptions/$EXCEPTION_ID" >"$RUN_DIR/after-public.json" || fail "post-rejection detail failed"
"$ROOT_DIR/.venv/bin/python" - "$RUN_DIR/before.json" "$RUN_DIR/after-public.json" <<'PY' || fail "public rejection changed exception state/history"
import json, sys
before, after = (json.load(open(path)) for path in sys.argv[1:])
if before.get("status") != after.get("status") or before.get("history") != after.get("history"):
    raise SystemExit(1)
PY

export PUBLIC_DEMO_READ_ONLY=false
"${COMPOSE[@]}" up -d --force-recreate api >"$RUN_DIR/dev-api.log" 2>&1 || fail "development API restart failed"
for ((attempt=1; attempt<=60; attempt++)); do curl --silent --show-error --fail "$API_BASE/readyz" >/dev/null 2>&1 && break; sleep 1; done
DEV_STATUS="$(curl --silent --output "$RUN_DIR/dev-response.json" --write-out '%{http_code}' -X PATCH -H 'content-type: application/json' --data-binary @"$RUN_DIR/public-patch.json" "$API_BASE/exceptions/$EXCEPTION_ID/status")"
[[ "$DEV_STATUS" == "200" ]] || fail "development lifecycle mutation returned $DEV_STATUS"
"${COMPOSE[@]}" restart api >"$RUN_DIR/restart.log" 2>&1 || fail "API restart failed"
for ((attempt=1; attempt<=60; attempt++)); do curl --silent --show-error --fail "$API_BASE/readyz" >/dev/null 2>&1 && break; sleep 1; done
curl --silent --show-error --fail "$API_BASE/exceptions/$EXCEPTION_ID" >"$RUN_DIR/persisted.json" || fail "persisted detail failed after restart"
"$ROOT_DIR/.venv/bin/python" - "$RUN_DIR/persisted.json" <<'PY' || fail "PostgreSQL state did not persist after API restart"
import json, sys
if json.load(open(sys.argv[1])).get("status") != "ACKNOWLEDGED": raise SystemExit(1)
PY
for ((attempt=1; attempt<=60; attempt++)); do curl --silent --show-error --fail "$DASHBOARD_BASE/_stcore/health" >"$RUN_DIR/dashboard-health" 2>/dev/null && break; sleep 1; done
curl --silent --show-error --fail "$DASHBOARD_BASE/" >"$RUN_DIR/dashboard-root.html" || fail "dashboard root failed"
API_BASE_URL="$API_BASE" "$ROOT_DIR/.venv/bin/python" - <<'PY' || fail "CSV export failed"
import csv, io, os
from control_tower.dashboard.client import DashboardClient
from control_tower.dashboard.ui import exceptions_to_csv
with DashboardClient(os.environ["API_BASE_URL"]) as client:
    rows = client.get_all_exceptions()
parsed = list(csv.reader(io.StringIO(exceptions_to_csv(rows))))
if len(parsed) < 2 or parsed[0][0] != "id" or not parsed[1][0]: raise SystemExit(1)
PY

printf 'M07 production gate passed: images, migration, repeat bootstrap, API readiness/liveness, public rejection with unchanged state/history, development lifecycle, restart persistence, dashboard smoke, and CSV export.\n'
