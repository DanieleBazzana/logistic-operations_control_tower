# Deployment architecture

This is the deployment record for the current product boundary. The public demo is
served by the existing Cloud Run deployment, with FastAPI, Streamlit, and Neon
PostgreSQL; the repository retains the local topology below as its reproducible
verification path. The deployment work did not change the business or API contracts.

```text
Local PostgreSQL volume / deployed Neon PostgreSQL
       ^
       | explicit PostgreSQL fields -> safe SQLAlchemy URL
       |
  migrate job ----> bootstrap job
       |                 |
       +-----------------+----> FastAPI service (8080, non-root)
                                      ^
                                      | HTTP API_BASE_URL
                                      |
                              Streamlit service (8501, non-root)
```

## Explicit operational boundaries

- `Dockerfile.api` and `Dockerfile.dashboard` build independent minimal Python
  images and run as UID/GID 10001. Neither image contains `.env`, generated data,
  caches, or a writable application data volume.
- `migrate` runs `alembic upgrade head` as a one-shot job. `bootstrap` runs
  deterministic generation, idempotent ingestion, and explicit detection as a
  separate one-shot job. FastAPI and Streamlit never perform either operation at
  startup.
- The containers honor `PORT`; production commands bind all interfaces and do not
  enable reload or debug. Compose maps them to loopback-only host ports for local
  use. Existing Cloud Run services supply the provider `PORT`.
- `/api/v1/livez` is process-only; `/api/v1/readyz` checks PostgreSQL. The existing
  `/api/v1/health` remains a compatibility database readiness endpoint.
- Request middleware returns a safe `X-Request-ID` and emits structured JSON with
  method, path, status, duration, and correlation ID only. It never records query
  strings, request bodies, headers, SQL, or credentials.

## Public demo boundary

`PUBLIC_DEMO_READ_ONLY` is explicit configuration and defaults to `false` for local
development. When true, FastAPI rejects the lifecycle PATCH route server-side with
403 before calling the lifecycle service; the response does not change exception
state or append history. Streamlit reads the same environment boundary and hides
its lifecycle form. Existing GET routes remain anonymous read-only operational
surfaces. Authentication, OAuth/SSO, user management, and enterprise RBAC are not
provided by the current product.

## Current provider mapping

The existing deployment maps the FastAPI API and Streamlit dashboard to Cloud Run and
uses Neon PostgreSQL for persistence. The public service entry points are documented
in the repository README; IAM, secrets, Neon connection details, revision identifiers,
and deployment manifests are external to this repository. No provider resource is
provisioned or changed here. Alembic migration and deterministic
bootstrap remain explicit release jobs rather than web startup behavior. The public
demo is read-only through the server-side `PUBLIC_DEMO_READ_ONLY` boundary.

## CI and evidence boundary

GitHub Actions in `.github/workflows/ci.yml` runs Ruff, unit tests, shell syntax,
Compose validation, container builds/hardening, and a PostgreSQL integration job.
The local deployment scripts provide the same delivery checks with disposable resources;
Docker-dependent and database-dependent checks are not implied by AppTest or unit
results when their prerequisites are unavailable.

## Conservative cost and pricing posture

This is an architecture guardrail, not a quote. Provider prices, quotas, and free-tier
limits change; no dynamic price or quota has been verified for this repository. Any
future cost estimate must label unverified values `PRICE_NOT_VERIFIED`, include the
verification date, and link to the current official pricing page.

- **Cloud Run:** a Google Cloud billing account/project is required before changing
  Cloud Run services; see the [billing-account troubleshooting guidance](https://cloud.google.com/run/docs/troubleshooting#billing-account).
  The intended baseline is `min instances=0` for both API and dashboard, allowing
  scale-to-zero when idle; this reduces idle compute but does not guarantee a $0 bill.
  Requests, CPU/memory while handling requests, networking/egress, logs, and other
  Google Cloud services can still incur charges. Consult [Cloud Run pricing](https://cloud.google.com/run/pricing)
  and [instance autoscaling](https://cloud.google.com/run/docs/about-instance-autoscaling)
  before any deployment.
- **Neon:** the local design assumes a Neon Free plan only for evaluation, with
  `PRICE_NOT_VERIFIED` pricing and limits. Free-plan compute, storage, retention,
  branching, connection, and egress limits are dynamic and must be checked on the
  [official Neon pricing page](https://neon.tech/pricing); overages or a plan change
  can create charges or interrupt the workload.
- **Network and overage risk:** place Cloud Run and Neon in compatible regions where
  practical, minimize cross-region traffic, and treat database and public API egress
  as a budget risk. This project makes no guarantee of $0 spend and makes no paid
  commitment on behalf of an operator.
- **Budget controls:** configure a billing budget and alert thresholds before
  provisioning; [Google Cloud budgets](https://cloud.google.com/billing/docs/how-to/budgets)
  alert on forecast/actual spend but do not cap or automatically stop all usage.
  Record the selected plans, thresholds, and a dated price verification in the
  deployment change review. No cloud resources, billing account, budget, or paid
  commitment is created by this repository.
