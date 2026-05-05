# D2C Platform

Multi-tenant marketing data API. D2C brands connect their Shopify store and Meta Ads account, sync data via background jobs, and query spend-vs-revenue analytics through a scoped API.

---

## Quick Start

```bash
cp .env.example .env
docker compose up          # postgres + mock servers + app + migrations + seed
open http://localhost:8000/docs
```

**Seeded credentials**

| Email | Brand domain |
|-------|-------------|
| `alice@alpha.com` | `brand-alpha.myshopify.com` |
| `bob@beta.com` | `brand-beta.myshopify.com` |

```bash
# Get a token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@alpha.com","brand_domain":"brand-alpha.myshopify.com"}'

# Trigger a sync
curl -X POST http://localhost:8000/api/v1/integrations/shopify/sync \
  -H "Authorization: Bearer <token>"

# Query analytics
curl "http://localhost:8000/api/v1/insights/spend-vs-revenue?from=2024-01-01&to=2024-01-31" \
  -H "Authorization: Bearer <token>"
```

---

## Project Layout

```
app/
  core/           config, JWT, encryption, logging
  db/             engine (NullPool/QueuePool), TenantSession, RLS context
  models/         ORM models — every tenant table carries brand_id
  middleware/     JWT auth dependency, request logging
  integrations/
    base_client.py          retry + exponential backoff with jitter
    circuit_breaker.py      per-provider open/half-open/closed state
    token_lifecycle.py      expiry detection, EXPIRING warnings
    shopify/  urls / serializers / service
    meta/     urls / serializers / service
  api/v1/endpoints/
    {feature}_{urls,serializers,logic}.py   one triplet per feature

infra/
  Dockerfile, Dockerfile.mock
  cloudrun-service.yaml     Cloud Run service definition
  cloudbuild.yaml           CI/CD pipeline (test -> build -> migrate -> deploy)
  setup_gcp.sh              one-shot GCP provisioning script

migrations/
  versions/0001_initial.py  full schema
  versions/0002_rls.py      PostgreSQL Row-Level Security policies

tests/
  unit/test_core.py                       JWT, encryption, HMAC, serializers
  integration/test_tenant_isolation.py    5 proofs of cross-tenant blocking
```

---

## Architecture Decisions

### Multi-Tenancy: Shared Schema + Three-Layer Isolation

**Approach:** one set of tables, `brand_id` on every row. Chosen over separate-schema and separate-database tenancy.

| Option | Why rejected |
|--------|-------------|
| Separate DB per tenant | Migrations x N tenants; N connection strings; impractical at hundreds of brands |
| Schema per tenant (search_path) | DDL required for each new tenant; Alembic cannot autogenerate across schemas; catalog bloat at scale |
| **Shared schema** (chosen) | Instant new tenants, one migration, one backup, simple operations |

The tradeoff is a "noisy neighbour" risk and the possibility of a query accidentally omitting the `brand_id` filter. We address both with three explicit layers:

**Layer 1 — JWT (middleware/auth.py)**
`brand_id` is baked into the signed token at login. No forged brand context without the HS256 key. Validated on every protected request before any handler runs.

**Layer 2 — TenantSession (db/tenant.py)**
All application queries go through `TenantSession`, which unconditionally appends `WHERE brand_id = :brand_id` to every SELECT, INSERT, UPDATE, DELETE. The raw escape hatch (`session.execute()`) is replaced with `scoped_execute(model, stmt)` which requires a model class and enforces scoping. An unscoped attempt raises `TenantScopeError` immediately — it cannot silently succeed.

**Layer 3 — PostgreSQL Row-Level Security (migrations/0002_rls.py)**
Even if layers 1 and 2 are bypassed — a direct psql session, a future microservice that shares the DB, an ORM bug — the database itself enforces `brand_id`. Before each query, `TenantSession.activate()` sets `SET LOCAL app.current_brand_id = '<uuid>'`. The RLS policy on every tenant table reads that variable. `SET LOCAL` is transaction-scoped; it resets on commit or rollback, so pooled connections cannot carry stale context between requests.

The `brands` table is intentionally RLS-exempt: the login endpoint needs to read a brand record to validate the request before `brand_id` is known.

### Phase 2: Parent Company with Subsidiary Brands

No schema rewrites required. The migration path is purely additive:

1. `Brand.settings` JSONB already stores `parent_company_id: null`. Promote to a real FK column in one `ALTER TABLE ... ADD COLUMN` migration.
2. Add `parent_companies` table (id, name, settings JSONB).
3. Add `ParentUser` model for cross-brand admin users.
4. JWT gains an optional `parent_company_id` claim for parent admins, who can request brand-scoped tokens via a token-exchange endpoint (audited).
5. TenantSession and all existing brand-scoped API contracts are unchanged.

---

### Authentication

Mock Google OAuth: `POST /auth/login` accepts `{email, brand_domain}` and issues a JWT. In production this endpoint accepts a Google ID token and calls Google's tokeninfo endpoint to extract the email before proceeding.

JWT payload: `{sub: user_id, brand_id, email, exp, iat, jti}`. The `jti` claim supports a future token revocation blocklist (Redis). HS256 is correct for a single-issuer system; upgrade to RS256 when downstream services need to verify tokens without holding the signing key.

One DB query per request validates the user still exists and their `brand_id` matches the JWT. This catches deactivated users and suspended brands.

---

### Token Lifecycle (integrations/token_lifecycle.py)

Tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256). The `ENCRYPTION_KEY` comes from Secret Manager in production.

States: `VALID` -> `EXPIRING` (within 5-day buffer) -> `EXPIRED` -> `MISSING`. The 5-day buffer gives 5 missed sync cycles to recover before a token becomes unusable. Shopify tokens don't expire; Meta user tokens expire in 60 days. `EXPIRING` emits a warning log and continues the sync. `EXPIRED` or `MISSING` raises `IntegrationNotConnectedError`.

Key rotation: `ENCRYPTION_KEY` is a comma-separated list. `MultiFernet` encrypts with the first key, decrypts with any. Rotate by prepending a new key — old tokens still decrypt.

---

### Retry and Circuit Breaker

**Retry (base_client.py):** Exponential backoff with full jitter: `wait = random(0, min(60, base x 2^attempt))`. Full jitter avoids thundering herd — if 100 brands all hit a 429, they don't all retry at the same moment. Respects `Retry-After` headers. 3 retries by default.

**Circuit Breaker (circuit_breaker.py):** Per-provider, per-brand state machine (CLOSED -> OPEN -> HALF-OPEN). After 5 consecutive failures the circuit opens; sync jobs fail fast without making HTTP calls. After 60 seconds a single test request is allowed through.

Current limitation: circuit state is in-process. In a multi-instance Cloud Run deployment each instance has independent state. Phase 2: back the state with Cloud Memorystore (Redis) for shared circuit state.

---

### Idempotency

Every sync uses PostgreSQL `INSERT ... ON CONFLICT (brand_id, external_id) DO UPDATE`. Running the same sync twice produces identical DB state. Correct for retries after a mid-sync crash, Shopify/Meta webhooks (at-least-once delivery), and manual re-syncs.

Webhook processing uses a persist-first pattern: the raw event is written to `webhook_events` with `processed=false` before business logic runs. If processing crashes, the event is not lost and can be replayed by querying `WHERE processed = false`.

---

### Schema Design

**JSONB vs normalised columns**

`Brand.settings` is JSONB with a GIN index. Config fields (`timezone`, `currency`, `feature_flags`) vary by brand and change shape over time. Adding a new setting requires no migration. The GIN index makes `WHERE settings @> '{"timezone": "UTC"}'` efficient.

Rule for promotion to a normalised column: when a field appears in a `GROUP BY`, `JOIN`, or `ORDER BY` in production queries. None of the current settings fields do.

**Date column type**

`ad_spend_daily.date` is a proper PostgreSQL `DATE` column, not `VARCHAR`. This enables native range operators, correct index range scans, and timezone-aware date arithmetic without per-query casts.

**Index strategy**

| Index | Type | Purpose |
|-------|------|---------|
| `(brand_id, date)` on `ad_spend_daily` | B-tree composite | Primary analytics query: equality on brand + range on date |
| `(brand_id, date) INCLUDE (spend, revenue, impressions, clicks)` | Covering | Index-only scan for spend-vs-revenue — no heap access at scale |
| `settings` on `brands` | GIN | JSONB containment queries |
| `(domain) WHERE is_active = true` on `brands` | Partial B-tree | Login lookup: smaller, faster than a full index |
| `(brand_id, external_id)` unique constraints | B-tree | Idempotency enforcement on products, orders, ad_spend_daily |

---

### GCP Deployment

**Cloud Run + Cloud SQL private IP + Secret Manager**

Cloud Run is stateless and scales to zero. This affects two things:

**Connection pooling:** The app uses `NullPool` when `DB_POOL_SIZE=0` (set in `cloudrun-service.yaml`). Each request opens and closes a connection via the Cloud SQL Auth Proxy Unix socket (`host=/cloudsql/PROJECT:REGION:INSTANCE`). The Proxy handles pooling at the socket layer. This is the documented GCP recommendation — a persistent pool in a scale-to-zero service wastes connections and exhausts Cloud SQL's limit across many dormant instances.

**RLS session variables:** `SET LOCAL app.current_brand_id` is transaction-scoped and resets on commit/rollback. Safe with NullPool (fresh connection per request) and also safe with QueuePool (resets before the connection returns to the pool).

**Cloud SQL is private-IP only.** Cloud Run reaches it through a Serverless VPC Access connector. DB traffic never touches the public internet. 

**Secret Manager** stores JWT key, encryption key, DB password, and OAuth secrets. The Cloud Run service account has `secretmanager.secretAccessor` on exactly those 6 secrets — not a project-wide role.

**Deployment pipeline (cloudbuild.yaml):**
1. Unit tests
2. Build + push image to Artifact Registry  
3. Run `alembic upgrade head` as a Cloud Run Job (same network/secret access as the app)
4. Deploy new revision with `--no-traffic` (zero users hit it yet)
5. Shift 100% traffic to the new revision

Migrations run before the new code is live. All migrations are additive to stay backward-compatible during the deploy window. Destructive changes (DROP COLUMN) happen in a separate PR after the old code is fully retired.

---

## What Was Cut and Why

**Celery / background task queue**
Sync jobs run synchronously in the HTTP request. The correct implementation is Cloud Tasks: the endpoint enqueues a task, returns `202 Accepted` immediately, and the task runs asynchronously. Cut because it requires a broker, adds operational complexity, and does not affect correctness for the mock. The sync endpoint is the right place to add Cloud Tasks enqueue calls in Phase 2.

**Token refresh**
`token_lifecycle.py` detects expiring tokens and logs a warning. The actual Meta refresh flow (`/oauth/access_token?grant_type=fb_exchange_token`) is not implemented. Cut because the mock server doesn't expire tokens and implementing refresh without real credentials is untestable.

**JWT revocation**
JWTs are valid until expiry (60 minutes). A Redis-backed blocklist would allow immediate revocation on logout or brand suspension. Cut because it requires Redis infrastructure and adds a network hop per request. The `jti` claim is present so the blocklist can be added without a token format change.

**Per-instance circuit breaker state**
The circuit breaker state is in-process. Multiple Cloud Run instances have independent state. Cut in favour of correctness clarity — the in-process implementation is correct for a single instance and the Cloud Memorystore path is documented.

**Hourly ad spend granularity**
`ad_spend_daily` stores one row per (brand, platform, campaign, day). If hourly ROAS is ever needed, a migration and historical re-pull are required. Cut: daily is the right starting point for D2C dashboards and matches native Shopify/Meta API granularity.

---

## What I Would Do Differently

**pgBouncer in transaction mode instead of NullPool**
NullPool means a TCP handshake on every request. The Cloud SQL Auth Proxy helps but doesn't eliminate this. pgBouncer in transaction mode multiplexes thousands of app connections onto a small real pool. The tradeoff is another component to operate.

**Async background tasks from day 1**
Sync jobs blocking HTTP responses is the most significant gap. I would start with Cloud Tasks: same GCP project, no broker, built-in retries and dead-letter queues. The sync endpoint becomes: validate -> enqueue task -> `202 Accepted`. This also makes it straightforward to schedule per-brand syncs at different intervals.

**Separate migration user from app user in Alembic config**
`setup_gcp.sh` creates `d2c_app` (DML) and `d2c_migrations` (DDL). `alembic.ini` should hard-wire `DATABASE_SYNC_URL` to use `d2c_migrations`, never `d2c_app`. Currently both exist but the separation isn't enforced in config, meaning a misconfigured deploy could run DDL via the restricted app user (and fail, but it could also run DDL with the wrong role if wired incorrectly).

**Structured error envelope**
Errors return FastAPI's default `{"detail": "..."}`. A production API needs `{error: {code, message, request_id}}` so clients can switch on `code` and logs correlate on `request_id`.

**Connection health endpoint**
There's no surface for "Brand X's Shopify token expires in 3 days" or "Brand Y's last sync failed". A scheduled check writing to a `connection_health` table (or a simple admin endpoint) would surface stale-data issues before brands notice them.

---

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/unit/ -v                  # no DB required

docker compose up postgres -d
pytest tests/integration/ -v           # requires Postgres
```

The integration test suite in `test_tenant_isolation.py` contains five explicit proofs:
1. `get_by_id` with a foreign brand's UUID returns `None`
2. `all()` returns only the current tenant's rows
3. Writing with a mismatched `brand_id` raises `TenantScopeError`
4. `brand_id=None` on a new object is auto-filled to the session brand
5. HTTP request with a JWT whose `brand_id` mismatches the DB user returns `401`

---

## GCP Deployment

```bash
# One-shot infrastructure provisioning
./infra/setup_gcp.sh YOUR_PROJECT_ID us-central1

# CI/CD does this automatically on push to main:
gcloud builds submit --config=infra/cloudbuild.yaml

# Or deploy the service definition directly
# (edit infra/cloudrun-service.yaml to set PROJECT_ID first)
gcloud run services replace infra/cloudrun-service.yaml --region=us-central1
```

Minimum IAM for the service account: `roles/cloudsql.client` on the project, `roles/secretmanager.secretAccessor` on exactly the 6 application secrets.
