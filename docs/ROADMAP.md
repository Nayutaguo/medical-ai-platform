# Roadmap

## Phase 1 - Structured Query MVP

Goal: run a safe, testable data-query chain.

- Define `QuerySpec` JSON schema with Pydantic.
- Validate tables, fields, operators, aggregations, aliases, and limits.
- Compile safe parameterized SQL with SQLAlchemy Core.
- Execute against MySQL through a `QueryExecutor` interface.
- Expose MCP tools:
  - `get_database_schema`
  - `get_distinct_values`
  - `query_medical_data`
- Provide synthetic development data and sample queries.
- Provide scripts for cleaning and loading a SPARCS development subset.
- Completed local full-data acceptance: 2,101,588 SPARCS 2021 rows were
  stream-cleaned with zero rejects, manifest/hash reconciliation, staging load,
  atomic MySQL publish, and a retained rollback table. See ADR 0004.
- Keep database integration tests optional.

## Phase 1.5 - Minimal LLM Planner

- Add an OpenAI-compatible LLM client configured through `.env`.
- Convert natural-language questions to `QuerySpec`, not free-form SQL.
- Normalize common LLM JSON shape variants before strict validation.
- Keep the raw LLM response available for debugging.
- Add minimal demos that can run without a frontend.

## Phase 2 - Product HTTP Backend and Existing Frontend

- Completed: Flask application factory and `/api/v1` Blueprint.
- Completed: repository/service/API dependency direction.
- Completed: stable `success/data/meta/error` contract, request IDs, size limits, and exception mapping.
- Completed: aggregate-only public QuerySpec endpoint and LLM-not-configured degradation.
- Completed: existing React + Vite + MUI + ECharts workbench migrated through a minimal API-client change.
- Completed training slice: membership-scoped history/favorites/QuerySpec reuse
  and browser-local CSV, chart PNG, and HTML report generation. Local exports
  are not the production server-authorized/audited export design.
- Add deployment configuration around the Gunicorn WSGI entry point.
- Identity, authorization, audit, and privacy work is tracked as the dedicated Phase 2.5 product release gate.

## Phase 2.5 - Identity, Governance, and Operational Control Plane

Goal: turn the analytical workbench into an authenticated, tenant-aware, auditable product without rewriting the existing frontend or splitting premature microservices. The accepted direction is documented in [`docs/adr/0001-identity-governance-control-plane.md`](adr/0001-identity-governance-control-plane.md).

### Identity and persistence foundation

- Completed: Alembic baseline plus additive migrations through
  `007_demo_completion`, including facilities, cross-tenant integrity,
  tenant-bound one-time invitations, membership-scoped analysis history, and
  the password-reset token purpose.
- Separate the logical control and analytics schemas and use least-privilege credentials for application control writes, analytics reads, audit appends, and ingestion.
- Completed foundation: organizations, users, memberships, roles, permissions,
  grants, facilities, scopes, sessions, tenant-bound invitation tokens, audit
  ledger, background jobs, and dataset versions. Idempotency/outbox behavior
  remains.
- Completed: one-time administrator bootstrap with no default password.

### Authentication and authorization

- Completed: Argon2id, opaque server-side sessions, secure-cookie posture, CSRF,
  frontend login/invited-registration/session/logout/change-password/reset,
  identity-version-bound single-use reset tokens, and Redis source/account
  authentication throttling. Production email/SMS reset delivery, broader
  account lifecycle policy, and privileged MFA remain.
- Completed: authenticated immutable `AccessContext` with user, organization,
  membership, permissions, versions, and fail-closed facility scope.
- Completed for analytics services: stable exact permission codes; frontend
  visibility is not an authorization boundary.
- Completed for authenticated HTTP: RBAC plus organization/facility data scopes
  and a trusted predicate outside QuerySpec/model input.
- Completed first administration slice: tenant-bound member, role, and facility
  catalogs; one-time invitation issuance; optimistic role/scope replacement;
  membership lifecycle updates; and facility-catalog synchronization. Writes
  require CSRF and exact `users.manage`, `roles.assign`, or `imports.create`
  permissions by operation.
- Completed custom-role slice: stable permission catalog plus tenant role
  creation, full permission replacement, optimistic editing, and deletion of
  unused roles. System roles are immutable and affected authorization versions
  are invalidated.
- Require explicit, time-bounded, and fully audited emergency access instead of granting medical-data access implicitly to platform administrators.

### Privacy, audit, and transport parity

- Completed: configurable minimum groups enforced with SQL `HAVING` before
  `LIMIT`, defensive filtering, and indistinguishable empty/suppressed output.
- Completed first field-policy slice: governed distinct/dimension/aggregation/
  filter capabilities across all 34 inpatient fields.
- Persist sanitized append-only audit events for authentication, administration, queries, Agent calls, imports, exports, and job lifecycle changes.
- Completed for successful administration mutations: the audit fact shares the
  control-plane transaction. Attempt/failure coverage, other request paths, and
  transaction/outbox integration remain.
- Completed audit-read slice: `audit.read` exposes a redacted, tenant-bound,
  filterable keyset page and the frontend provides its query screen. Ledger
  population remains partial; this does not imply complete audit coverage.
- Completed: authenticated product analytics routes and non-sensitive health.
- Completed fail-closed product posture: unscoped MCP is disabled. An
  authenticated MCP principal adapter remains.

### Product UI and operations

- Completed first UI slices: existing workbench; login, invitation-only
  registration, permission-pending state, session restore, current organization,
  logout, password change, forgot/reset password; plus a permission-aware
  organization management workspace for invitations, member status, role
  assignment/definition, facility scopes, catalog sync, and audit browsing.
- Completed training history/export slice: successful authenticated QuerySpec
  and Agent runs persist safe metadata without result rows or original Agent
  questions; users can favorite/delete/reuse entries and locally download the
  currently displayed authorized aggregate as CSV/PNG/HTML.
- Administration still needs real-MySQL mutation coverage, authenticated browser
  E2E, complete audit/outbox behavior, and an operational runbook before
  release.
- Completed first Redis slice: production login throttling. Query/Agent limits,
  session/authorization caches, and invalidation remain.
- Move long-running Agent, import, quality, and production export/report work to
  durable background jobs; expose bounded status polling and cancellation APIs.
  The current browser-local export remains explicitly outside that production
  workflow.
- Connect the verified full-data importer to `background_jobs` and
  `dataset_versions`, including heartbeat, resume/checkpoint, idempotent
  activation, cancellation, and recovery drills. The local CLI is complete for
  a controlled single-host acceptance run but is not the final job product.
- Use transactions, unique constraints, idempotency keys, and optimistic locking for business correctness; reserve distributed locks for singleton imports, schedulers, and duplicate job submission.

### Release acceptance

- Verify 401, 403, 409, and 429 response contracts and preserve the existing response envelope and request IDs.
- Test cross-organization and cross-facility isolation, session fixation and expiry, brute-force protection, permission-cache invalidation, minimum-group suppression, and audit completeness.
- Test Redis and worker outages, retry idempotency, cancellation, recovery, and full-data query plans before production exposure.
- Once authentication is enabled in production, all failure and rollback paths must fail closed rather than restore anonymous analytical access.

## Phase 3 - Data Agent Workflow

- First slice completed:
  - LLM-first `AgentPlan`
  - intent and route fields
  - tool selection among schema, distinct values, and structured query
  - backend `ChartSpec`
  - execution steps
  - post-query LLM `AgentInsight`
- Add planner validation and retry loops around schema/tool feedback.
- Completed transport-resilience slice: bounded retries, empty/non-JSON response validation, 502/504 mapping, and post-query insight degradation.
- Move long-running Agent requests from synchronous WSGI workers to background jobs with task status polling.
- Keep human-readable execution trace.
- Inspect DeepAnalyze and LAMBDA before implementing agent state, artifact tracking, and tool-calling workflow.

## Phase 4 - Python Analysis Sandbox

- Add controlled Python analysis for statistics, regression, correlation, clustering, anomaly detection, and complex plotting.
- Required controls before implementation:
  - sandbox isolation
  - CPU limit
  - memory limit
  - timeout
  - filesystem isolation
  - network isolation
  - package allowlist
- Never execute raw LLM output with `exec`.

## Phase 5 - Spark/Hive Scale-Out

- Preserve the executor interface:
  - `QueryExecutor`
  - `MySQLExecutor`
  - future `SparkExecutor`
  - future `HiveExecutor`
- Prefer PySpark local mode for early validation.
- Do not deploy HDFS/YARN until the MySQL MVP and product workflow are stable.
