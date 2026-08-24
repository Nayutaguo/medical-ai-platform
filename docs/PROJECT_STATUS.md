# Project status

Last updated: 2026-08-24.

## Delivery position

The repository is in Phase 2.5 product governance and early Phase 3 Agent
workflow. The aggregate analysis loop works, and the first authenticated,
tenant-aware privacy boundary is implemented. It is still **not a production
release**: full data, administration workflows, durable audit integration,
background jobs, operations, and release testing remain open.

Estimated position against the documented end state:

- first releasable product, excluding Python/Spark: about 60% complete;
- all roadmap phases, including Python sandbox and Spark/Hive: about 45% complete.

These percentages are risk-weighted, not based on lines of code. Security,
privacy, data acceptance, deployment, and recovery are release gates.

## Current runtime

```text
React/MUI/ECharts SPA
  -> explicit anonymous loopback mode, or opaque browser session + CSRF
  -> Flask /api/v1
  -> AccessContext (user, organization, membership, permissions, versions)
  -> trusted facility scope + governed field capability policy
  -> QuerySpec validation + parameterized SQLAlchemy Core compiler
  -> HAVING COUNT(*) >= privacy threshold before LIMIT
  -> MySQL aggregate result
  -> optional LLM Agent insight
  -> sanitized chart/table/summary response
```

Production startup is fail-closed. `medical_ai.api.wsgi:app` requires
authentication, secure cookies, Redis-backed login protection, and disabled
unscoped MCP. `medical_ai.api.dev_wsgi:app` is the explicit anonymous loopback
entry point.

## Completed in the current productization slice

### Identity, authorization, and browser flow

- Alembic migrations cover tenant-bound invitations through
  `006_invitation_registration`; local application and drift verification are
  part of the current registration slice.
- Fifteen control-plane tables cover users, organizations, memberships, roles,
  permissions, role grants, opaque sessions, one-time token placeholders,
  append-only audit storage, jobs, datasets, facilities, organization ownership,
  and membership facility scopes.
- Passwords use Argon2id. Session and CSRF values use random opaque tokens; only
  fixed-width digests are persisted.
- Login, invitation-only registration, current-session, and CSRF-protected logout
  APIs are implemented.
- The existing frontend now has login/register tabs, session/exit controls, and
  a permission-pending state. The three-column workbench layout is unchanged.
- `AccessContext` is immutable and carries exact permission and facility facts.
  Governance roles do not implicitly grant analytics access.
- The Agent entry requires Agent, Schema, Distinct, and Query permissions, so a
  single Agent permission cannot bypass downstream tool authorization.
- Session idle extension is monotonic under out-of-order concurrent requests.
- A one-time interactive administrator bootstrap exists with no default password.

### Tenant and database integrity

- Facilities have one owning organization in the first product version;
  `organization_facilities.facility_id` is unique.
- Membership facility grants use composite organization foreign keys and empty
  scope means deny-all.
- Dataset versions cannot reference an import job from another organization.
- User audit actors must match a real user/membership/organization tuple; system
  actors cannot carry user identity.
- Audit identity references use retention-safe semantics rather than cascade
  deletion.
- The `inpatient` table has the mandatory facility/year index used by trusted
  scope queries.
- Migration 005 reconciles SQLAlchemy/MySQL comments explicitly and has reviewed
  upgrade, downgrade, partial-recovery, and cross-tenant tests.

### Query, privacy, and Agent safety

- Product queries are aggregate-only, use strict QuerySpec validation, and bind
  every SQL value.
- Trusted facility predicates are supplied outside QuerySpec and cannot be
  widened by JSON, prompts, model output, frontend state, or MCP arguments.
- Minimum-group privacy defaults to 5. It is enforced with parameterized
  `HAVING COUNT(*) >= k` before ordering/limiting, followed by an application
  defense-in-depth filter.
- Empty results and results containing only suppressed groups have the same
  public and LLM-visible shape. Suppressed counts, internal aliases, unsafe
  truncation metadata, compiled SQL, and trusted facility IDs are not exposed.
- A central capability matrix covers all 34 inpatient fields. Governed paths
  reject unsafe distinct fields, `MIN/MAX`, high-risk dimensions, sensitive
  numeric filters, wildcard diagnosis searches, more than three dimensions, and
  more than six filters.
- Filter values are type checked, bounded, finite, non-nested, and parameterized.
- Diagnosis/procedure classifications may be exact cohort filters but cannot be
  returned as high-cardinality dimensions.
- LLM empty/non-JSON/disconnected/429/5xx responses have bounded retries and
  stable 502/504 mapping. A failed second insight call does not discard a
  completed aggregate result.

### API, frontend, and operational safeguards

- `/api/v1` uses one `success/data/meta/error` envelope and request ID.
- Public liveness is dependency-free; readiness uses a bounded `SELECT 1` and
  does not expose database names, row counts, or model configuration.
- All API responses, including errors, are `no-store` and omit SQL, secrets,
  provider bodies, database diagnostics, and patient-level rows.
- The SPA restores the CSRF proof from a same-origin readable cookie into memory;
  it never stores it in local/session storage. Authentication errors fail closed.
- Login has Redis-backed, atomic IP and normalized-account buckets before Argon2.
  Redis keys are HMAC digests. Exceeded limits return 429 plus `Retry-After`;
  Redis failure returns 503 and does not allow password verification to proceed.
- Product MCP refuses to start or execute unscoped tools. Local MCP remains a
  development-only path until it has an authenticated principal adapter.
- The frontend clears stale results before new queries, detects empty/non-JSON
  HTTP responses, displays request IDs, shows the medical disclaimer, disposes
  charts, and throttles resize through `requestAnimationFrame`.

## Verified state

- Python unit suite: **291 passed**.
- Normal full suite: **291 passed, 6 external integrations skipped**.
- Real local MySQL integration suite: **6 passed** with `RUN_MYSQL_TESTS=1`.
- Alembic: `006_invitation_registration (head)` and
  `No new upgrade operations detected`.
- Frontend: TypeScript check and production Vite build pass. The only build note
  is the existing bundle-size warning.
- Python compileall and `git diff --check` pass.
- `redis` 6.4.0 is installed in the current Conda environment.

The local analytics database still contains only the 1,000-row cleaned SPARCS
development subset. The source file contains about 2.1 million data rows and has
not completed full-load acceptance.

## Release blockers

### P0: required before a product release

1. Run the full 2.1M-row streaming clean/import through staging, checkpointing,
   rejection reporting, source hash, row/quality reconciliation, atomic publish,
   and query-plan benchmarks. The current loader is not yet a recoverable import
   product.
2. Implement user/membership lifecycle, role assignment, and facility-scope
   administration services/APIs, then add a small permission-aware administration
   UI. Login and invited registration alone cannot provision an analyst.
3. Connect audit attempt/success/failure events to login, logout, denied access,
   query, Agent, import, and administration paths. Security writes must use a
   transaction-aware writer or outbox; the current audit primitive is not yet a
   complete audit trail.
4. Add privileged-account MFA/step-up. Invitation activation now uses tenant,
   membership, purpose, identity-version, expiry, and atomic single-use binding;
   password reset remains disabled until it satisfies the same standard.
5. Separate production database credentials for analytics read-only, control
   read/write, audit insert, and migration/ingestion. Verify grants against real
   MySQL.
6. Add query/Agent shared rate limits, per-user/organization privacy budgets,
   rounding or minimum cohort-delta policy, and repeated-overlap detection.
   Minimum-group protection alone is not formal differential privacy.
7. Move Agent, import, export, and report work out of synchronous Gunicorn into
   durable MySQL job state plus Celery/Redis delivery, idempotent workers,
   bounded retry, heartbeat, cancellation, and recovery.
8. Deliver a production deployment and recovery chain: TLS reverse proxy,
   secrets management, process supervision/containers, CI/CD, backups and restore
   drill, structured metrics/logs, dashboards, alerts, load tests, and rollback.

### P1: product quality after the security gates

- Add Agent planner self-correction using safe validation/tool feedback.
- Add frontend unit/component tests and authenticated browser E2E coverage.
- Add query caching only after permission/scope/privacy checks; cache keys must
  include tenant, scope, permission version, and dataset version.
- Add complete public API schemas, pagination/export contracts, and operational
  runbooks.
- Add provider governance for sending aggregate rows to an external LLM,
  including a data-processing agreement, field policy, cost/SLA monitoring, and
  an option to disable second-call interpretation.

### P2: later roadmap capabilities

- Controlled Python analysis sandbox with CPU, memory, time, filesystem,
  network, and package limits.
- Spark/Hive/HDFS executors and full-data scale-out only after the MySQL product
  path has measured limits that justify them.

## Working tree and handoff

The active branch is `feat/platform-development`. The repository still has only
the initial commit; the productization work is present as a large uncommitted and
untracked working tree. This is a delivery risk. Before synchronization, split
the changes into small Conventional Commits (migrations, identity/auth, governed
query, rate limit, frontend, docs) and add CI. Do not commit `.env`, local data,
database exports, logs, `frontend/dist`, or generated caches.

## Next implementation order

1. Administration write services/APIs plus transactional audit/outbox.
2. Full-data recoverable import and quality acceptance.
3. Redis query/Agent limits and durable Celery jobs.
4. Minimal administration UI and authenticated E2E tests.
5. Production credentials, deployment, monitoring, backup, and load acceptance.

The accepted design is documented in ADR 0001 (identity/governance), ADR 0002
(Redis/jobs/locking), and ADR 0003 (governed analytics/privacy).
