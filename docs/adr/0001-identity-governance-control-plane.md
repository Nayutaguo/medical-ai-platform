# ADR 0001: Identity and Governance Control Plane

- Status: Accepted; foundation implemented, release controls still pending
- Date: 2026-08-24
- Owners: platform backend and security

## Context

At decision time, the modular Flask application had a React workbench, safe
`QuerySpec` analysis, MySQL medical data, and optional MCP, but no identity or
tenant boundary. The foundation described here is now implemented for browser
HTTP traffic; durable audit integration, administration workflows, authenticated
MCP, MFA, credential separation, and background jobs remain release work.

Authentication and authorization are not isolated UI features. They must constrain every schema lookup, distinct-value request, structured query, Agent request, export, import, administrative action, and background job. Medical-data access also requires a data-scope decision in addition to ordinary role permissions.

The immediate product does not require independent identity microservices, but it does require boundaries that can be extracted later without changing the public API or weakening the analysis safety model.

## Decision

### 1. Use a modular monolith with explicit control-plane modules

Identity and governance will be implemented inside the existing deployment as a modular monolith. The intended dependency direction is:

```text
HTTP or authenticated MCP transport
  -> authentication and request principal
  -> authorization and data-scope policy
  -> application service
  -> control-plane or analytics repository
  -> separately privileged MySQL schemas
```

Target module responsibilities are:

- `medical_ai.identity`: users, organizations, credentials, sessions, password reset, and privileged-account MFA.
- `medical_ai.authorization`: permission checks, role resolution, organization selection, facility scope, and privacy policy inputs.
- `medical_ai.audit`: append-only security, administration, query, import, and Agent audit events.
- `medical_ai.jobs`: durable job state, idempotency, queue submission, progress, cancellation, and retry policy.
- `medical_ai.services`: transport-neutral use cases that require an authenticated `AccessContext`.
- `medical_ai.repositories`: separate control-plane and read-only analytics persistence adapters.
- `medical_ai.api` and `medical_ai.mcp_server`: transport adapters only; they must not implement independent authorization rules.

This keeps deployment simple while preventing route handlers, MCP tools, the frontend, or LLM output from becoming authorization authorities.

### 2. Separate control and analytics data

The target MySQL layout uses two logical schemas, which may initially share one MySQL instance:

- `medical_ai_control`: identity, authorization, sessions, audit, one-time tokens, jobs, and idempotency metadata.
- `medical_ai_analytics`: the `inpatient` fact table and future curated analytical dimensions or aggregates.

Database credentials are separated by responsibility:

- the analytics reader receives `SELECT` only on curated analytics objects;
- the control-plane application account receives required CRUD rights only on control tables;
- the audit writer receives append-only rights on audit and outbox tables where operationally possible;
- the loader account owns controlled ingestion and schema migration privileges and is not used by web workers.

Authorization is resolved before an analytics query is compiled. The application passes a trusted access scope to the analytics layer; user or LLM input cannot create, remove, or broaden that scope. Control and analytics data are not joined through arbitrary user-generated queries.

### 3. Use RBAC plus organization and facility data scopes

RBAC answers what a caller may do. A separate data-scope policy answers which organizations and facilities the action may cover.

Initial built-in roles are `platform_admin`, `organization_admin`, `data_analyst`, `data_engineer`, `auditor`, and `viewer`. Role names are conveniences; stable permission codes such as `analytics.query.execute`, `analytics.agent.execute`, `analytics.result.export`, `users.manage`, `roles.assign`, `audit.read`, and `imports.create` are the enforcement contract.

Roles are assigned to an organization membership, not directly to a global user. A session is bound to one active organization. The effective facility scope is the intersection of:

1. facilities owned or delegated to that organization;
2. facilities granted to the active membership;
3. any narrower filter requested by the user.

The trusted facility predicate is injected after `QuerySpec` validation and
before SQL compilation. It cannot be overridden by a prompt, QuerySpec, request
header, frontend state, or MCP argument. Minimum groups are enforced in SQL with
`HAVING` before `LIMIT`, followed by a serialization-time defensive filter.
Platform administration does not implicitly grant medical-data access;
emergency access must be explicit, time-bounded, and fully audited.

### 4. Use opaque server-side browser sessions

The browser uses a cryptographically random opaque session token in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie. Only a SHA-256 digest of the token is stored. Session state includes the user, active organization membership, idle and absolute expiry, revocation state, and identity/authorization version values.

Passwords are stored only as Argon2id hashes. Login and reset responses do not disclose whether an identifier exists. Login, privilege changes, password changes, organization switching, and MFA elevation rotate or revoke sessions as appropriate. State-changing browser requests use CSRF protection, and production startup requires HTTPS-compatible cookie configuration.

JWT claims stored in browser storage are not the default because permission and data-scope changes must take effect promptly. API keys or OAuth-style credentials for service accounts may be added for non-browser clients; raw credentials are never stored.

### 5. Add normalized control-plane tables

The first control-plane migration creates:

| Table | Purpose and primary constraints |
| --- | --- |
| `organizations` | Tenant or hospital organization; unique code and explicit lifecycle state. |
| `users` | Global identity; unique normalized identifiers, Argon2id hash, lock state, and `auth_version`. |
| `organization_memberships` | User-to-organization relationship; unique `(organization_id, user_id)`, membership state, and `authz_version`. |
| `roles` | Initially built-in role definitions with stable unique codes. |
| `permissions` | Stable unique permission codes. |
| `role_permissions` | Composite key `(role_id, permission_id)`. |
| `membership_roles` | Composite key `(membership_id, role_id)`. |
| `facilities` | Canonical facility identity mapped to analytics `PermanentFacilityId`. |
| `organization_facilities` | Single organization ownership in v1; unique `facility_id`. Future sharing requires an explicit governed share grant. |
| `membership_facility_scopes` | Explicit restricted facility grants for a membership. |
| `auth_sessions` | Unique token digest, expiry, revocation, and session version metadata. |
| `one_time_tokens` | Hashed invitation, verification, and password-reset tokens with purpose, expiry, and single-use state. |
| `audit_events` | Append-only sanitized event facts, request ID, actor, organization, outcome, query fingerprint, dimensions, metrics, row count, and timing. |
| `analysis_jobs` | Durable state for Agent, import, quality, export, and report jobs. |
| `idempotency_keys` | Unique organization/caller/key combinations for retried mutation requests. |
| `outbox_events` | Transactional handoff for security-critical audit or background events when asynchronous delivery is used. |

Control tables use internal numeric primary keys plus non-enumerable public IDs, UTC timestamps, foreign keys, unique constraints, and optimistic version columns where concurrent administration is possible. Users and memberships are disabled or soft-deleted rather than physically removed. Audit rows are never cascade-deleted. One-time and session tokens are stored only as hashes.

The analytics schema adds indexes that support mandatory scope filtering, starting with `PermanentFacilityId` plus common time dimensions. Exact composite indexes are selected from full-data query plans rather than added speculatively.

### 6. Keep API and service boundaries explicit

The authentication API owns login, logout, current-session inspection, organization switching, password changes, reset, and MFA verification. Administrative APIs own membership lifecycle, role assignment, facility-scope assignment, and audit search. Job APIs own submission, status, cancellation, and artifact metadata.

All endpoints retain the existing `success/data/meta/error` envelope and request ID. Authentication failures use 401, authorization or data-scope failures use 403, optimistic-lock or idempotency conflicts use 409, and rate limits use 429. Lists use bounded cursor pagination.

`/api/v1/health` becomes a non-sensitive liveness check. Database and queue readiness move to an internal or administrative endpoint; public health responses do not expose database names, row counts, or model configuration.

The existing React workbench is changed incrementally: add a login route, authenticated application shell, current-user provider, permission-aware navigation, organization switcher, and focused user/role/scope/audit pages. Hiding a control in the frontend is only a usability measure; backend authorization remains mandatory.

### 7. Apply one policy to HTTP and MCP

HTTP and MCP must call the same authorization-aware application services. MCP tools may not instantiate an executor and bypass aggregate, scope, privacy, rate-limit, or audit policies.

Unauthenticated external MCP transport is disabled in product environments. When MCP exposure is required, it uses an authenticated service account or delegated user principal with the same permission and facility-scope model. Machine credentials are hashed, rotatable, expiring where possible, and independently auditable. Until that path exists, MCP is restricted to trusted local development.

### 8. Introduce Redis, queues, and locks only for defined responsibilities

MySQL remains the source of truth. Redis may cache sessions and resolved authorization, provide shared rate limits, and expose short-lived job progress. Cache keys include identity and authorization versions so user, role, or scope changes invalidate effective access. Redis failure must never broaden access, and patient-level data is not cached.

Agent calls, full-data imports, quality reports, exports, and report generation become durable background jobs. An initial Celery-compatible queue may use Redis for operational simplicity, while a higher-availability deployment can use RabbitMQ without changing the job API. Queue messages carry job IDs rather than medical results; workers load state from MySQL and implement idempotent transitions.

Distributed locks are reserved for singleton imports, schedulers, and duplicate job submissions. User and role writes rely on transactions, foreign keys, unique constraints, idempotency keys, and optimistic locking. A Redis lock, if used, requires a unique owner token, bounded TTL, compare-and-delete release, and fencing or equivalent stale-owner protection; it is never the sole correctness mechanism.

## Implementation status on 2026-08-24

Implemented: migrations through 006, opaque login/invited-registration/session/
logout, tenant-bound single-use invitation tokens, CSRF, explicit permissions,
organization membership and single-owner facility scope, trusted query
predicates, field capabilities, minimum groups, production fail-closed startup,
a small frontend authentication shell, and Redis-backed authentication
throttling.

Partially implemented: append-only audit validation/storage and durable job
tables exist, but audit calls and queue workers are not connected to request
flows. The current MySQL development instance still uses one broad credential.

Not enabled: password reset, privileged MFA, administrative user/role/scope
APIs, authenticated MCP, Redis caches, Celery workers, and production deployment.
These remain release gates and are tracked in `PROJECT_STATUS.md`.

## Phased Implementation

1. Record the permission matrix, threat model, data-scope rules, retention rules, and this ADR.
2. Introduce Alembic, baseline the existing analytics schema, and add control-plane tables through additive migrations.
3. Create least-privilege database accounts and an explicit one-time administrator bootstrap command without a default password.
4. Implement password hashing, opaque sessions, CSRF, login throttling, session invalidation, privileged-account MFA, and authentication APIs.
5. Introduce `AccessContext`, authorization services, organization/facility scopes, trusted query predicates, and minimum-group privacy enforcement.
6. Protect all HTTP endpoints; disable or authenticate MCP and route it through the same services.
7. Persist append-only audits for authentication, administration, query, Agent, import, export, and job actions.
8. Add the minimal login and administration frontend without redesigning the existing workbench.
9. Add Redis-backed shared caching and rate limiting, then move Agent and data operations to durable jobs.
10. Verify cross-organization isolation, privilege changes, session expiry, brute-force protection, privacy thresholds, audit completeness, worker recovery, cache invalidation, and full-data load behavior before production release.

Identity tables and enforcement may be deployed additively behind a pre-production rollout flag. Once authentication is enabled in a production environment, rollback must fail closed and must not reopen anonymous analytical access.

## Consequences

### Positive

- HTTP, MCP, Agent, import, and future Spark paths share one security policy.
- Roles and data scopes can change without redeploying the application.
- The system can scale from one deployment to multiple web and worker processes without changing its API contract.
- Analysis credentials remain read-only, and control-plane writes cannot mutate the medical fact table.
- Audit records identify the actor, organization, allowed scope, action, outcome, and request without storing raw medical results or complete natural-language prompts.

### Costs and risks

- The product gains schema migrations, credential separation, session lifecycle, audit retention, Redis, and worker operations that require monitoring and recovery procedures.
- Mandatory scope predicates and privacy suppression require new query-plan and cross-tenant tests.
- Authorization caches introduce invalidation complexity and must fail closed.
- A phased rollout must avoid temporary anonymous bypasses and must bootstrap the first administrator securely.

## Alternatives Considered

### Independent identity microservice now

Rejected for the current stage. It adds network, deployment, tracing, and distributed-transaction complexity before team or traffic boundaries justify extraction. The modular boundary preserves a future extraction path.

### Browser-stored long-lived JWT permissions

Rejected as the default. Embedded roles and facility scopes become stale after administrative changes, and browser storage increases token-theft impact. Short-lived delegated tokens may still be added for external APIs.

### RBAC without data scopes

Rejected. A medical analyst role alone cannot express organization or facility isolation, and would allow cross-tenant data access.

### A single schema and single database account

Rejected for product operation. It would allow a compromised web or identity path to write analytical data and would prevent meaningful least-privilege controls.

### Frontend-only authorization

Rejected. Browser state is attacker-controlled and cannot protect HTTP, MCP, background workers, or direct API clients.

### Redis as the sole session, job, or lock authority

Rejected. Redis is an acceleration and coordination layer; durable state and correctness constraints remain in MySQL.

### External IAM as the only first implementation

Deferred. OIDC or enterprise SSO is desirable later, but it does not replace organization membership, medical-data scopes, audit policy, minimum-group protection, or service-account governance inside this platform.
