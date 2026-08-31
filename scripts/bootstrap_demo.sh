#!/usr/bin/env bash
# Run migration and deterministic bootstrap as explicit one-shot jobs.
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${COMPOSE_PROJECT_NAME:-control-tower-local}"
COMPOSE=(docker compose --project-name "$PROJECT" --file "${ROOT_DIR}/docker-compose.yml")

command -v docker >/dev/null 2>&1 || { printf 'docker is required\n' >&2; exit 1; }
"${COMPOSE[@]}" version >/dev/null 2>&1 || { printf 'Docker Compose is required\n' >&2; exit 1; }
[[ -n "${POSTGRES_PASSWORD:-}" ]] || {
    printf 'POSTGRES_PASSWORD must be exported explicitly\n' >&2
    exit 1
}

"${COMPOSE[@]}" up -d postgres
"${COMPOSE[@]}" run --rm migrate
"${COMPOSE[@]}" run --rm bootstrap
