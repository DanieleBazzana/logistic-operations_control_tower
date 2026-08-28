#!/usr/bin/env bash
# Verify the complete local M06 release flow against an isolated Compose PostgreSQL.
# This script never reads .env implicitly: export synthetic/local values explicitly.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT_DIR}/.venv/bin/python"
COMPOSE_PROJECT_NAME="control-tower-m06-release-${$}"
COMPOSE=(docker compose --project-name "$COMPOSE_PROJECT_NAME" --file "${ROOT_DIR}/docker-compose.yml")
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/control-tower-m06.XXXXXX")"
API_PID=""
DASHBOARD_PID=""

fail() {
    printf 'M06 release verification failed: %s\n' "$1" >&2
    exit 1
}

cleanup() {
    set +e
    if [[ -n "$API_PID" ]]; then
        kill "$API_PID" >/dev/null 2>&1
        wait "$API_PID" >/dev/null 2>&1
    fi
    if [[ -n "$DASHBOARD_PID" ]]; then
        kill "$DASHBOARD_PID" >/dev/null 2>&1
        wait "$DASHBOARD_PID" >/dev/null 2>&1
    fi
    "${COMPOSE[@]}" down --volumes >/dev/null 2>&1
    rm -rf "$RUN_DIR"
}
trap cleanup EXIT INT TERM

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

[[ -x "$PYTHON" ]] || fail "repository virtualenv is missing: .venv/bin/python"
require_command docker
require_command curl
"${COMPOSE[@]}" version >/dev/null 2>&1 || fail "Docker Compose is unavailable"

[[ -n "${TEST_DATABASE_URL:-}" ]] || fail "TEST_DATABASE_URL must be set explicitly"
[[ "${ALLOW_DESTRUCTIVE_TEST_DB:-}" == "1" ]] || fail \
    "set ALLOW_DESTRUCTIVE_TEST_DB=1 for an isolated disposable database"
[[ -n "${POSTGRES_PASSWORD:-}" ]] || fail "POSTGRES_PASSWORD must be set explicitly"

# Validate the same effective target constraints as the integration reset fixture.
# This command intentionally prints only a non-sensitive database/user/port tuple.
CONNECTION_TARGET="$("$PYTHON" -c '
import os
import sys
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

try:
    parsed = make_url(os.environ["TEST_DATABASE_URL"])
except (ArgumentError, ValueError, KeyError):
    sys.exit("TEST_DATABASE_URL must be a valid SQLAlchemy PostgreSQL URL")
if parsed.query:
    sys.exit("TEST_DATABASE_URL must not include query parameters")
if parsed.get_backend_name() != "postgresql":
    sys.exit("TEST_DATABASE_URL must be a PostgreSQL URL")
if (parsed.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
    sys.exit("TEST_DATABASE_URL must target a loopback host")
if parsed.database != "control_tower_m04" and not (parsed.database or "").endswith("_test"):
    sys.exit("TEST_DATABASE_URL database must be control_tower_m04 or end with _test")
if not parsed.username or parsed.password is None:
    sys.exit("TEST_DATABASE_URL must contain the Compose username and password")
if parsed.password != os.environ["POSTGRES_PASSWORD"]:
    sys.exit("TEST_DATABASE_URL password must match POSTGRES_PASSWORD")
print("{}\t{}\t{}".format(parsed.database, parsed.username, parsed.port or 5432))
' 2>"$RUN_DIR/url-check.error")" || fail "$(tr '\n' ' ' < "$RUN_DIR/url-check.error")"
IFS=$'\t' read -r TEST_DB TEST_USER TEST_PORT <<< "$CONNECTION_TARGET"

if [[ -n "${POSTGRES_USER:-}" && "$POSTGRES_USER" != "$TEST_USER" ]]; then
    fail "POSTGRES_USER must match the TEST_DATABASE_URL username"
fi
export POSTGRES_USER="$TEST_USER"
export POSTGRES_DB="$TEST_DB"
export POSTGRES_PORT="$TEST_PORT"
export DATABASE_URL="$TEST_DATABASE_URL"

printf 'M06 release verification: starting isolated PostgreSQL (%s)\n' "$COMPOSE_PROJECT_NAME"
"${COMPOSE[@]}" up -d postgres >"$RUN_DIR/compose-up.log" 2>&1 || fail "Docker Compose PostgreSQL startup failed"

for ((attempt = 1; attempt <= 60; attempt++)); do
    if "${COMPOSE[@]}" exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        >"$RUN_DIR/pg-health.log" 2>&1; then
        break
    fi
    sleep 1
done
"${COMPOSE[@]}" exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    >"$RUN_DIR/pg-health-final.log" 2>&1 || fail "PostgreSQL did not become healthy"

"$PYTHON" -m alembic upgrade head >"$RUN_DIR/migration.log" 2>&1 \
    || fail "Alembic migration to head failed"

BUNDLE_ONE="$RUN_DIR/bundle-one"
BUNDLE_TWO="$RUN_DIR/bundle-two"
AS_OF="2025-01-15T12:00:00Z"
SEED="20250301"
"$PYTHON" -m control_tower.synthetic generate --output-dir "$BUNDLE_ONE" --seed "$SEED" \
    --as-of "$AS_OF" >"$RUN_DIR/generate-one.log" 2>&1 \
    || fail "deterministic fixture generation failed"
"$PYTHON" -m control_tower.synthetic generate --output-dir "$BUNDLE_TWO" --seed "$SEED" \
    --as-of "$AS_OF" >"$RUN_DIR/generate-two.log" 2>&1 \
    || fail "repeat fixture generation failed"

cmp -s "$BUNDLE_ONE/manifest.json" "$BUNDLE_TWO/manifest.json" \
    || fail "manifest generation is not byte-for-byte deterministic"
while IFS= read -r artifact; do
    cmp -s "$BUNDLE_ONE/$artifact" "$BUNDLE_TWO/$artifact" \
        || fail "artifact generation is not byte-for-byte deterministic: $artifact"
done < <(cd "$BUNDLE_ONE" && find . -type f -name '*.csv' | sort)

"$PYTHON" -m control_tower.ingestion ingest --input-dir "$BUNDLE_ONE" \
    >"$RUN_DIR/ingest-one.json" 2>&1 || fail "initial ingestion failed"
"$PYTHON" -m control_tower.ingestion ingest --input-dir "$BUNDLE_ONE" \
    >"$RUN_DIR/ingest-two.json" 2>&1 || fail "idempotent re-ingestion failed"

"$PYTHON" -c '
import json
import sys
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("committed") is not True:
        raise SystemExit(f"ingestion did not commit: {path}")
' "$RUN_DIR/ingest-one.json" "$RUN_DIR/ingest-two.json" \
    || fail "ingestion result did not confirm a committed transaction"

"$PYTHON" -m control_tower.exceptions --as-of "$AS_OF" \
    >"$RUN_DIR/detection-one.json" 2>&1 || fail "initial exception detection failed"
"$PYTHON" -m control_tower.exceptions --as-of "$AS_OF" \
    >"$RUN_DIR/detection-two.json" 2>&1 || fail "idempotent exception detection failed"

"$PYTHON" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    first = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    second = json.load(handle)
if first["detections"] < 6 or first["created"] != first["detections"]:
    raise SystemExit("initial detection did not create all six rule findings")
if second["created"] != 0 or second["updated"] != first["detections"]:
    raise SystemExit("repeat detection was not active-row idempotent")
' "$RUN_DIR/detection-one.json" "$RUN_DIR/detection-two.json" \
    || fail "exception detection deduplication check failed"

API_PORT="${API_PORT:-8000}"
export API_PORT
"$PYTHON" -c '
import os
import socket

try:
    port = int(os.environ["API_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
except (OSError, ValueError):
    raise SystemExit("API_PORT is unavailable")
' >"$RUN_DIR/api-port-check.log" 2>&1 \
    || fail "API_PORT is unavailable; choose a free loopback port"
API_LOG="$RUN_DIR/api.log"
"$PYTHON" -m uvicorn control_tower.api.app:app --host 127.0.0.1 --port "$API_PORT" \
    --log-level warning >"$API_LOG" 2>&1 &
API_PID=$!
API_BASE="http://127.0.0.1:${API_PORT}/api/v1"
export API_BASE
for ((attempt = 1; attempt <= 60; attempt++)); do
    if curl --silent --show-error --fail "$API_BASE/health" >"$RUN_DIR/health.json" 2>/dev/null; then
        break
    fi
    sleep 1
done
curl --silent --show-error --fail "$API_BASE/health" >"$RUN_DIR/health-final.json" \
    || fail "API health check failed"
curl --silent --show-error --fail "$API_BASE/kpis/summary?as_of=$AS_OF" \
    >"$RUN_DIR/kpi.json" || fail "API KPI check failed"
curl --silent --show-error --fail "$API_BASE/exceptions?page=1&page_size=1" \
    >"$RUN_DIR/queue.json" || fail "API exception queue check failed"

EXCEPTION_ID="$($PYTHON -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    items = json.load(handle).get("items", [])
if not items or items[0].get("id") is None:
    raise SystemExit("exception queue is empty")
print(items[0]["id"])
' "$RUN_DIR/queue.json")" || fail "API queue did not return an exception"
curl --silent --show-error --fail "$API_BASE/exceptions/$EXCEPTION_ID" \
    >"$RUN_DIR/detail.json" || fail "API exception detail check failed"

EXCEPTION_TYPE="$("$PYTHON" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    detail = json.load(handle)
required = {
    "exception_type", "severity", "status", "business_impact", "revenue_at_risk",
    "orders_affected", "root_cause", "recommended_action", "confidence", "detected_at",
    "history",
}
missing = required.difference(detail)
if missing:
    raise SystemExit(f"exception detail is missing fields: {sorted(missing)}")
if not isinstance(detail["history"], list) or not detail["history"]:
    raise SystemExit("exception detail history is missing or empty")
print(detail["exception_type"])
' "$RUN_DIR/detail.json")" || fail "API exception detail response contract failed"
export EXCEPTION_TYPE
curl --silent --show-error --fail --get \
    --data-urlencode "exception_type=$EXCEPTION_TYPE" \
    "$API_BASE/exceptions?page=1&page_size=100" \
    >"$RUN_DIR/filtered-queue.json" || fail "API exception type filter check failed"
"$PYTHON" -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    body = json.load(handle)
items = body.get("items", [])
expected_type = sys.argv[2]
if not items:
    raise SystemExit("exception type filter returned no items")
if any(item.get("exception_type") != expected_type for item in items):
    raise SystemExit("exception type filter returned a mismatched item")
' "$RUN_DIR/filtered-queue.json" "$EXCEPTION_TYPE" \
    || fail "API exception type filter response contract failed"

"$PYTHON" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    health = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    kpi = json.load(handle)
if health != {"status": "ok"}:
    raise SystemExit("unexpected health response")
required = {
    "orders_processed", "sla_performance_pct", "open_exceptions", "critical_exceptions",
    "revenue_at_risk", "stockout_risks", "supplier_delays", "shipment_delays",
}
if not required.issubset(kpi):
    raise SystemExit("KPI response is missing a required field")
' "$RUN_DIR/health-final.json" "$RUN_DIR/kpi.json" \
    || fail "API health/KPI response contract failed"

DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
export DASHBOARD_PORT
"$PYTHON" -c '
import os
import socket

try:
    port = int(os.environ["DASHBOARD_PORT"])
    if not 1 <= port <= 65535:
        raise ValueError
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
except (OSError, ValueError):
    raise SystemExit("DASHBOARD_PORT is unavailable")
' >"$RUN_DIR/dashboard-port-check.log" 2>&1 \
    || fail "DASHBOARD_PORT is unavailable; choose a free loopback port"
DASHBOARD_LOG="$RUN_DIR/dashboard.log"
API_BASE_URL="$API_BASE" "$PYTHON" -m streamlit run "${ROOT_DIR}/src/control_tower/dashboard/app.py" \
    --server.headless true --server.address 127.0.0.1 --server.port "$DASHBOARD_PORT" \
    --browser.gatherUsageStats false >"$DASHBOARD_LOG" 2>&1 &
DASHBOARD_PID=$!
DASHBOARD_BASE="http://127.0.0.1:${DASHBOARD_PORT}"
for ((attempt = 1; attempt <= 60; attempt++)); do
    if curl --silent --show-error --fail "$DASHBOARD_BASE/_stcore/health" \
        >"$RUN_DIR/dashboard-health.json" 2>/dev/null; then
        break
    fi
    sleep 1
done
curl --silent --show-error --fail "$DASHBOARD_BASE/_stcore/health" \
    >"$RUN_DIR/dashboard-health-final.json" \
    || fail "Streamlit health check failed"
curl --silent --show-error --fail "$DASHBOARD_BASE/" \
    >"$RUN_DIR/dashboard-root.html" \
    || fail "Streamlit root HTTP check failed"
"$PYTHON" -c '
from pathlib import Path
import sys

health = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
root = Path(sys.argv[2]).read_text(encoding="utf-8")
if health != "ok":
    raise SystemExit("unexpected Streamlit health response")
if "<html" not in root.lower():
    raise SystemExit("Streamlit root response was not HTML")
' "$RUN_DIR/dashboard-health-final.json" "$RUN_DIR/dashboard-root.html" \
    || fail "Streamlit health/root response contract failed"

printf '%s' '{"status":"ACKNOWLEDGED","actor":"m06-release-check"}' \
    >"$RUN_DIR/ack.json"
printf '%s' '{"status":"IN_PROGRESS","actor":"m06-release-check"}' \
    >"$RUN_DIR/progress.json"
printf '%s' '{"status":"RESOLVED","actor":"m06-release-check","reason":"release smoke test"}' \
    >"$RUN_DIR/resolve.json"
curl --silent --show-error --fail -X PATCH -H 'content-type: application/json' \
    --data-binary @"$RUN_DIR/ack.json" "$API_BASE/exceptions/$EXCEPTION_ID/status" \
    >"$RUN_DIR/ack-response.json" || fail "API acknowledgement lifecycle check failed"
curl --silent --show-error --fail -X PATCH -H 'content-type: application/json' \
    --data-binary @"$RUN_DIR/progress.json" "$API_BASE/exceptions/$EXCEPTION_ID/status" \
    >"$RUN_DIR/progress-response.json" || fail "API in-progress lifecycle check failed"
curl --silent --show-error --fail -X PATCH -H 'content-type: application/json' \
    --data-binary @"$RUN_DIR/resolve.json" "$API_BASE/exceptions/$EXCEPTION_ID/status" \
    >"$RUN_DIR/resolve-response.json" || fail "API resolution lifecycle check failed"

"$PYTHON" -c '
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    detail = json.load(handle)
if detail.get("status") != "RESOLVED":
    raise SystemExit("lifecycle smoke test did not finish RESOLVED")
if len(detail.get("history") or []) < 4:
    raise SystemExit("lifecycle smoke test did not append expected history")
if detail["history"][-1].get("to_status") != "RESOLVED":
    raise SystemExit("lifecycle smoke test history did not record RESOLVED")
' "$RUN_DIR/resolve-response.json" || fail "API lifecycle response contract failed"

"$PYTHON" -c '
import csv
import io
import os

from control_tower.dashboard.client import DashboardClient
from control_tower.dashboard.ui import exceptions_to_csv

with DashboardClient(os.environ["API_BASE"]) as client:
    rows = client.get_all_exceptions({"exception_type": [os.environ["EXCEPTION_TYPE"]]})
csv_text = exceptions_to_csv(rows)
csv_rows = list(csv.reader(io.StringIO(csv_text)))
if not rows:
    raise SystemExit("DashboardClient export returned no exceptions")
if (
    len(csv_rows) < 2
    or csv_rows[0][:2] != ["id", "exception_type"]
    or not csv_rows[1][0]
    or csv_rows[1][1] != os.environ["EXCEPTION_TYPE"]
):
    raise SystemExit("exception export is missing a non-empty header and row")
' \
    || fail "live DashboardClient exception export contract failed"

"$PYTHON" -m pytest tests/integration -q >"$RUN_DIR/integration.log" 2>&1 \
    || fail "PostgreSQL integration suite failed"

printf 'Release verification passed: deterministic bundle, ingestion idempotency, detection deduplication, API queue/detail/filter, Streamlit health/root, lifecycle history, and export.\n'
