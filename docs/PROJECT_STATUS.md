# Project status

Last updated: 2026-08-25.

## Delivery position

The repository is in Phase 2.5 product governance and early Phase 3 Agent
workflow. The aggregate analysis loop works, and the first authenticated,
tenant-aware privacy boundary is implemented. It is still **not a production
release**: the local full dataset and first organization-administration slice
are implemented, but administration integration acceptance, durable audit/job
integration, production data operations, and release testing remain open.

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
- The tenant-bound administration API and first management workspace cover
  member/role/facility catalogs, one-time invitations, role and facility-scope
  replacement, membership suspension/reactivation, and facility synchronization.
  Every write requires CSRF and exact permissions; membership writes also use
  optimistic `expected_version` checks.

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
- Successful invitation, membership-role, facility-scope, membership-status,
  and facility-catalog mutations append sanitized audit facts in the same MySQL
  transaction as the control-plane write. Attempt/failure and outbox coverage
  remain incomplete.
- Facility-catalog synchronization requires an explicit
  `INPATIENT_DATASET_OWNER_ORGANIZATION_ID` match and cannot bind the global
  inpatient catalog to an arbitrary first caller.
- The `inpatient` table has the mandatory facility/year index used by trusted
  scope queries.
- The local SPARCS 2021 dataset has completed streaming clean, zero-reject
  manifest/hash reconciliation, staging validation, atomic MySQL publish, and
  rollback-table retention for all 2,101,588 rows. Import publication and
  recovery semantics are recorded in ADR 0004.
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
- The frontend management view is permission-aware, keeps raw invitation tokens
  out of browser storage, chains optimistic versions across role/scope writes,
  treats an empty facility scope as deny-all, and prevents self-status changes
  in the normal UI. Backend authorization remains the enforcement boundary.

## Verified state

- Python/unit-backed suite: **428 passed**.
- Normal full suite: **428 passed, 9 external integrations skipped**.
- Real local MySQL integration suite: **9 passed** with `RUN_MYSQL_TESTS=1`.
- Alembic: `006_invitation_registration (head)` and
  `No new upgrade operations detected`.
- Frontend: **12 Vitest/Testing Library checks passed**; the TypeScript check and
  production Vite build pass. The only build note is the existing bundle-size
  warning.
- Python compileall and `git diff --check` pass.
- `redis` 6.4.0 is installed in the current Conda environment.

The local analytics database contains all 2,101,588 accepted SPARCS 2021 rows.
The prior 1,000-row table remains available as
`inpatient_backup_876dcce267b14044bfe85e5270b6207f`; it has not been approved
for deletion. The full clean produced zero rejected rows and no non-newborn
record with a non-null birth weight. There are 10,642 rows without a facility
ID; governed facility-scoped queries exclude them until an approved mapping
policy exists.

## Release blockers

### P0: required before a product release

1. Productize the verified 2.1M-row import path as durable background work:
   connect `background_jobs` and `dataset_versions`, add checkpoint/resume,
   idempotent activation, rejected-row artifacts, cancellation, interrupted-DDL
   reconciliation, backup/restore drills, and production-like load acceptance.
   The local controlled CLI acceptance is complete, but it is not yet an
   operator-facing import product.
2. Complete administration release acceptance. The first backend/API/UI slice
   now provisions invited members with roles and explicit facility scopes, but
   real-MySQL mutation tests, cross-tenant/outage tests, authenticated browser
   E2E, role-definition administration, and operational runbooks remain.
3. Connect audit attempt/success/failure events to login, logout, denied access,
   query, Agent, import, and remaining administration paths. Successful
   administration mutations now write transactionally, but failures and
   cross-service operations still require a transaction-aware writer or outbox.
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

The active branch is `feat/full-data-pipeline`, based on the latest
`origin/main`. The full-data pipeline changes are not yet committed. Before
synchronization, keep them as focused Conventional Commits for cleaning rules,
safe import state, tests, and documentation. Do not commit `.env`, raw/cleaned
data, profiles, manifests, load audits, database exports, retained MySQL backup
contents, logs, `frontend/dist`, or generated caches.

## Next implementation order

1. Administration real-MySQL/browser acceptance, role-definition operations,
   and complete attempt/failure audit/outbox integration.
2. Connect the accepted full-data pipeline to durable job/dataset-version state,
   checkpoint/recovery, and facility mapping governance.
3. Redis query/Agent limits and durable Celery jobs.
4. Administration and analytics authenticated E2E tests plus operations UI
   hardening.
5. Production credentials, deployment, monitoring, backup, and load acceptance.

The accepted design is documented in ADR 0001 (identity/governance), ADR 0002
(Redis/jobs/locking), ADR 0003 (governed analytics/privacy), and ADR 0004
(recoverable full-data publish).
