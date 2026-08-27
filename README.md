# Supply Chain Operations Control Tower

## Local bootstrap

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
# Replace the local password placeholder in both POSTGRES_PASSWORD and DATABASE_URL.
docker compose up -d postgres
.venv/bin/python -m pytest tests/unit/test_config.py -q
.venv/bin/ruff check .
```

`.env` is untracked local configuration. Before starting PostgreSQL, copy
`.env.example` to `.env` and replace `replace-with-a-local-password` with a
local-only password in both `POSTGRES_PASSWORD` and `DATABASE_URL`. If the
password contains URL-reserved characters, URL-encode it in `DATABASE_URL`.
Alternatively, pass `POSTGRES_PASSWORD` explicitly when invoking Compose, for
example `POSTGRES_PASSWORD=local-test-password docker compose up -d postgres`,
and provide a matching `DATABASE_URL` to the native Python application. The
Python application runs natively; Docker Compose provides PostgreSQL only.

## Database migration and health gate

With the local PostgreSQL service running, apply the M01 schema migration:

```bash
.venv/bin/python3 -m alembic upgrade head
```

The PostgreSQL migration/health gate uses an explicit `TEST_DATABASE_URL` and
checks both migration success and database health. After replacing the local
password placeholder in `.env`, load that configuration and run the gate
against an isolated, disposable database:

```bash
set -a
. ./.env
set +a
TEST_DATABASE_URL="$DATABASE_URL" \
  .venv/bin/python3 -m pytest tests/integration/test_postgres.py -q
```
