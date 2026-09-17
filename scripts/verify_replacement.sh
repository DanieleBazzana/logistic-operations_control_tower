#!/usr/bin/env bash
# Verify the replacement service against an explicitly supplied disposable PostgreSQL.
# The script never sources .env and never invents or prints database credentials.

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

: "${TEST_DATABASE_URL:?TEST_DATABASE_URL must be set explicitly to a disposable PostgreSQL URL}"

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    PYTHON="${ROOT_DIR}/.venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
    [[ -n "$PYTHON" ]] || {
        printf 'replacement verification unavailable: python3 is missing\n' >&2
        exit 1
    }
fi

if [[ -x "${ROOT_DIR}/.venv/bin/ruff" ]]; then
    RUFF=("${ROOT_DIR}/.venv/bin/ruff")
elif command -v ruff >/dev/null 2>&1; then
    RUFF=("$(command -v ruff)")
else
    printf 'replacement verification unavailable: ruff is missing\n' >&2
    exit 1
fi

run_gate() {
    local name="$1"
    shift
    printf 'replacement verification: %s\n' "$name"
    "$@"
    printf 'replacement verification: PASS: %s\n' "$name"
}

run_gate "Alembic upgrade head" env DATABASE_URL="$TEST_DATABASE_URL" "$PYTHON" -m alembic upgrade head
run_gate "replacement PostgreSQL integration tests" \
    env -u DATABASE_URL "$PYTHON" -m pytest tests/integration/test_replacement_postgres.py -q
run_gate "dataset-version focused unit tests" \
    env -u DATABASE_URL "$PYTHON" -m pytest tests/unit/test_dataset_versioning.py -q
run_gate "Ruff replacement scope" \
    "${RUFF[@]}" check src/control_tower/replacement tests/integration/test_replacement_postgres.py tests/unit/test_dataset_versioning.py
run_gate "Python compileall replacement scope" \
    env -u DATABASE_URL "$PYTHON" -m compileall -q src/control_tower/replacement tests/integration/test_replacement_postgres.py tests/unit/test_dataset_versioning.py
run_gate "bash syntax" bash -n scripts/verify_replacement.sh
run_gate "git diff check" git diff --check
run_gate "untracked-file whitespace check" env -u DATABASE_URL "$PYTHON" - <<'PY'
from pathlib import Path

paths = (
    Path("tests/integration/test_replacement_postgres.py"),
    Path("scripts/verify_replacement.sh"),
)
for path in paths:
    data = path.read_bytes()
    if not data.endswith(b"\n"):
        raise SystemExit(f"{path} must end with a newline")
    for number, line in enumerate(data.splitlines(), 1):
        if line.rstrip(b" \t") != line:
            raise SystemExit(f"trailing whitespace in {path}:{number}")
PY

printf 'replacement verification passed: migration, PostgreSQL replacement semantics, focused units, Ruff, compileall, shell syntax, and diff checks.\n'
