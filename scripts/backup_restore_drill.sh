#!/usr/bin/env bash
# Exercise backup and restore only against an explicitly disposable local database.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="control-tower-backup-${$}"
COMPOSE=(docker compose --project-name "$PROJECT" --file "${ROOT_DIR}/docker-compose.yml")
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/control-tower-backup.XXXXXX")"
TEST_DB=""
TEST_USER=""
TEST_PORT=""

fail() { printf 'backup/restore drill failed: %s\n' "$1" >&2; exit 1; }
cleanup() {
    set +e
    "${COMPOSE[@]}" exec -T postgres dropdb -U "$TEST_USER" --if-exists "${TEST_DB}_restore" >/dev/null 2>&1
    "${COMPOSE[@]}" down --volumes >/dev/null 2>&1
    rm -rf "$RUN_DIR"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || fail "docker is required"
"${COMPOSE[@]}" version >/dev/null 2>&1 || fail "Docker Compose is required"
[[ -n "${TEST_DATABASE_URL:-}" ]] || fail "TEST_DATABASE_URL must be exported explicitly"
[[ "${ALLOW_DESTRUCTIVE_TEST_DB:-}" == "1" ]] || fail "ALLOW_DESTRUCTIVE_TEST_DB=1 is required"
[[ -n "${POSTGRES_PASSWORD:-}" ]] || fail "POSTGRES_PASSWORD must be exported explicitly"

TARGET="$(${ROOT_DIR}/.venv/bin/python -c '
import os, sys
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
try:
    url = make_url(os.environ["TEST_DATABASE_URL"])
except (ArgumentError, ValueError, KeyError):
    sys.exit("invalid TEST_DATABASE_URL")
if url.get_backend_name() != "postgresql" or url.query:
    sys.exit("TEST_DATABASE_URL must be query-free PostgreSQL")
if (url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
    sys.exit("TEST_DATABASE_URL must target loopback")
if url.database != "control_tower_m04" and not (url.database or "").endswith("_test"):
    sys.exit("TEST_DATABASE_URL must name a disposable test database")
if url.password != os.environ["POSTGRES_PASSWORD"]:
    sys.exit("TEST_DATABASE_URL password must match POSTGRES_PASSWORD")
print("\t".join((url.database or "", url.username or "", str(url.port or 5432))))
' 2>"$RUN_DIR/url-check")" || fail "$(tr '\n' ' ' < "$RUN_DIR/url-check")"
IFS=$'\t' read -r TEST_DB TEST_USER TEST_PORT <<< "$TARGET"
export POSTGRES_DB="$TEST_DB" POSTGRES_USER="$TEST_USER" POSTGRES_PORT="${BACKUP_POSTGRES_PORT:-55432}"

"${COMPOSE[@]}" up -d postgres >"$RUN_DIR/up.log" 2>&1 || fail "PostgreSQL startup failed"
for ((attempt=1; attempt<=60; attempt++)); do
    if "${COMPOSE[@]}" exec -T postgres pg_isready -U "$TEST_USER" -d "$TEST_DB" >"$RUN_DIR/ready.log" 2>&1; then break; fi
    sleep 1
done
"${COMPOSE[@]}" exec -T postgres pg_isready -U "$TEST_USER" -d "$TEST_DB" >/dev/null 2>&1 \
    || fail "PostgreSQL did not become ready"

"${COMPOSE[@]}" run --rm --build migrate >"$RUN_DIR/migrate.log" 2>&1 \
    || fail "database migration failed"
"${COMPOSE[@]}" run --rm --build bootstrap >"$RUN_DIR/bootstrap.log" 2>&1 \
    || fail "database bootstrap failed"

"${COMPOSE[@]}" exec -T postgres pg_dump -Fc -U "$TEST_USER" -d "$TEST_DB" >"$RUN_DIR/backup.dump" \
    || fail "pg_dump failed"
[[ -s "$RUN_DIR/backup.dump" ]] || fail "backup artifact is empty"
"${COMPOSE[@]}" exec -T postgres createdb -U "$TEST_USER" "${TEST_DB}_restore" \
    || fail "restore database creation failed"
"${COMPOSE[@]}" exec -T postgres pg_restore -U "$TEST_USER" -d "${TEST_DB}_restore" --no-owner <"$RUN_DIR/backup.dump" \
    || fail "pg_restore failed"
TABLE_COUNT="$(${COMPOSE[@]} exec -T postgres psql -At -U "$TEST_USER" -d "${TEST_DB}_restore" -c \
    'select count(*) from information_schema.tables where table_schema = '\''public'\''' 2>"$RUN_DIR/count-error")" \
    || fail "restore verification query failed"
[[ "$TABLE_COUNT" =~ ^[1-9][0-9]*$ ]] || fail "restored database has no public tables"
ORDER_COUNT="$(${COMPOSE[@]} exec -T postgres psql -At -U "$TEST_USER" -d "${TEST_DB}_restore" -c \
    'select count(*) from public.orders' 2>"$RUN_DIR/order-count-error")" \
    || fail "restored bootstrap data verification query failed"
[[ "$ORDER_COUNT" =~ ^[1-9][0-9]*$ ]] || fail "restored database has no bootstrap orders"
KNOWN_ORDER_COUNT="$(${COMPOSE[@]} exec -T postgres psql -At -U "$TEST_USER" -d "${TEST_DB}_restore" -c \
    "select count(*) from public.orders where source_order_id = 'O000001'" 2>"$RUN_DIR/known-order-error")" \
    || fail "restored deterministic row verification query failed"
[[ "$KNOWN_ORDER_COUNT" == "1" ]] || fail "restored deterministic bootstrap row is missing"
printf 'Backup/restore drill passed: disposable PostgreSQL dump restored with schema and deterministic bootstrap data verified.\n'
