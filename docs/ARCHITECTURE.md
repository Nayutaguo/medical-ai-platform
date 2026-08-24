# Architecture

## Final Direction

```text
Natural language question
  -> LLM / Data Agent
  -> analysis plan
  -> data analysis tools
  -> SQL / Spark execution
  -> structured data
  -> optional Python secondary analysis
  -> charts + tables + written insights
```

## Phase 1 Runtime

```text
QuerySpec JSON
  -> Pydantic model
  -> QueryValidator
  -> QueryCompiler
  -> ExecutableQuery
  -> QueryExecutor
  -> MySQLExecutor
  -> QueryResult JSON
```

Current natural-language path:

```text
User question
  -> MedicalDataAgent
  -> OpenAI-compatible LLM planning call
  -> AgentPlan
       - intent
       - tool_name
       - QuerySpec
       - ChartSpec
       - execution_steps
  -> AgentPlan normalization
  -> QuerySpec validation
  -> QueryCompiler
  -> MySQLExecutor
  -> structured QueryResult
  -> OpenAI-compatible LLM result-interpretation call
  -> AgentInsight or INSIGHT_UNAVAILABLE warning
       - summary
       - chart_reading
       - observations
       - limitations
       - follow_up_questions
```

Current web application path:

```text
React/Vite/MUI workbench
  -> explicit anonymous loopback mode, or opaque session + CSRF
  -> Vite proxy /api/v1 or same-origin product request
  -> Flask API blueprint
  -> AccessContext + exact permission + trusted facility scope
  -> AnalyticsService
  -> governed field capability + minimum-group policy
  -> MedicalDataRepository
  -> MedicalDataAgent or MySQLExecutor
  -> ECharts rendering from Agent chart_spec
```

## Product control plane

The product remains a modular monolith for the current delivery stage. Identity and governance are explicit modules and persistence boundaries inside the same deployment, not logic duplicated across routes or transports. See [`docs/adr/0001-identity-governance-control-plane.md`](adr/0001-identity-governance-control-plane.md) for the accepted decision and alternatives.

```text
React browser                          authenticated MCP client
  -> secure session cookie               -> service/delegated identity
  -> CSRF validation                      -> MCP authentication
                 \                       /
                  AuthenticationService
                    -> RequestPrincipal / AccessContext
                    -> AuthorizationService
                         - active organization
                         - stable permissions
                         - organization facilities
                         - membership facility scope
                         - identity/authz versions
                    -> application service
                         - trusted scope predicate
                         - QuerySpec safety validation
                         - minimum-group privacy policy
                    -> analytics read repository
                    -> append-only audit / durable job state
```

Authorization is enforced before analytical execution and checked again at the application-service boundary. The frontend may hide unavailable actions, but only backend policy is authoritative. QuerySpec, LLM output, request headers, and MCP arguments cannot create or widen a trusted data scope.

### Logical persistence separation

```text
medical_ai_control
  -> organizations, users, memberships
  -> roles, permissions, role and scope grants
  -> sessions and one-time token hashes
  -> audit events, idempotency keys, outbox, jobs

medical_ai_analytics
  -> inpatient fact table
  -> curated dimensions and future aggregates
```

This is the target persistence split. The local implementation still shares one
database and credential and therefore is not production accepted. Product
deployment must use separate least-privilege credentials for control-plane
read/write, analytics read-only, audit append, and ingestion/migration. Web
workers must not receive ingestion or DDL privileges. Medical-data access is
scoped in application policy and expressed as a trusted analytics predicate
rather than an arbitrary cross-schema join.

MySQL remains the system of record. Redis is an acceleration and coordination
layer, never the correctness authority. The first implemented use is production
login protection with atomic HMAC-keyed source/account buckets. Session and
authorization caches plus job delivery remain planned. Durable Agent, import,
quality, export, and report work will be represented by a MySQL job row and
delivered through a queue by job ID. Distributed locks are limited to
non-critical singleton coordination; transactions, foreign keys, unique
constraints, idempotency, and optimistic locking remain the correctness
mechanisms.

## Boundaries

- `medical_ai.data` owns raw-to-canonical data cleaning helpers for development datasets.
- `medical_ai.query` owns structured query models, validation, and SQL compilation.
- `medical_ai.db` owns schema allowlists and execution.
- `medical_ai.mcp_server` owns MCP tool registration and thin transport wrappers; it must call the same authorization-aware services as HTTP and may not instantiate an executor to bypass policy.
- `medical_ai.agent` owns provider-neutral LLM configuration, LLM-first planning, chart recommendation, and result interpretation.
- `medical_ai.identity` owns organizations, users, credentials, opaque sessions,
  memberships, roles, and facility-scope persistence. Password recovery and MFA
  remain disabled or pending.
- `medical_ai.authorization` owns permission resolution, organization selection,
  facility scopes, and privacy-policy inputs.
- `medical_ai.audit` owns sanitized append-only event validation and storage;
  transactional integration into every business path remains pending.
- `medical_ai.jobs` will own durable job lifecycle, idempotency, queue delivery, retry, progress, and cancellation.
- `medical_ai.repositories` owns persistence adapters, separates control-plane writes from analytics reads, and contains no HTTP or business logic.
- `medical_ai.services` owns transport-neutral orchestration, requires an authenticated `AccessContext` for protected use cases, and applies aggregate, scope, and privacy rules.
- `medical_ai.api` owns the Flask application factory, `/api/v1` routes, request validation, response envelopes, request IDs, and exception mapping.
- `scripts/run_demo_server.py` is retained only as a local Flask runner; `medical_ai.api.wsgi:app` is the production WSGI entry point.
- `frontend/` owns the React + Vite + MUI + ECharts workbench, login and administrative user experience, and permission-aware presentation; it is never an authorization boundary and is changed incrementally.
- `third_party/` is for reference repositories only, not core source.

## Product Identity and Governance Invariants

- Protected requests resolve an authenticated principal, active organization membership, permission set, and trusted facility scope before invoking a business service.
- Browser authentication uses opaque server-side sessions in secure cookies; raw session, reset, invitation, API, and service-account tokens are never stored.
- Passwords use Argon2id, state-changing browser requests use CSRF protection, and privileged accounts require MFA before production release.
- Roles are assigned to organization memberships. Platform administration does not implicitly grant access to medical data.
- Permission or scope changes increment identity or authorization versions so cached decisions and active sessions can be invalidated promptly.
- Product analytical endpoints are aggregate-only and enforce configurable
  minimum groups with SQL `HAVING` before `LIMIT`, plus a serialization-time
  defense-in-depth filter.
- Authentication, authorization, administration, query, Agent, import, export,
  and job actions must create sanitized audit events without raw medical
  results, full prompts, secrets, or SQL text. The storage primitive exists;
  complete request-path integration is still a release blocker.
- Public liveness responses expose no database name, medical row count, user data, model configuration, or dependency details.
- External MCP access is disabled until it has an authenticated user or service principal and policy parity with HTTP.
- Redis or queue failure never broadens access. Once product authentication is enabled, failure and rollback paths fail closed.

## Current governed request flow

```text
session cookie
  -> active user/membership/version checks
  -> exact route permission
  -> non-empty membership facility allowlist
  -> QuerySpec syntax and aggregate-only validation
  -> 34-field governed capability matrix
  -> trusted facility predicate outside QuerySpec
  -> parameterized HAVING COUNT(*) >= k
  -> MySQL aggregate execution
  -> remove internal privacy fields and unsafe metadata
  -> optional LLM interpretation of safe rows
  -> no-store JSON response
```

The Agent entry requires Agent, Schema, Distinct, and Query permissions because
the model selects its downstream tool after entry. Authenticated MCP remains
disabled until it can supply this same context and policy path.

## Security Model for Phase 1

- No arbitrary SQL input.
- LLM output is normalized only into structured `AgentPlan` and `QuerySpec`; it is never executed as SQL.
- No write operation tools.
- Static table allowlist.
- Static field allowlist.
- Operator allowlist.
- Aggregation allowlist.
- Alias pattern allowlist.
- Parameter binding through SQLAlchemy Core.
- Maximum result limit.
- Distinct-value limit.
- MySQL query timeout setting where supported by the server.
- Public HTTP queries require aggregate metrics; patient-level selections are rejected.
- API errors never return stack traces or database configuration.
- Every HTTP response carries a validated or generated request ID.
- LLM transport validates content type, non-empty JSON envelopes, and message content before any model output is used.
- Transient LLM transport failures use bounded exponential-backoff retries; stable 4xx failures are not retried.
- A second-call interpretation failure does not discard an already completed aggregate query.

## Data Model Scope

The `inpatient` table is aligned with NY SPARCS 2021 de-identified hospital inpatient discharge fields:

- `HospitalServiceArea`
- `HospitalCounty`
- `OperatingCertificateNumber`
- `PermanentFacilityId`
- `FacilityName`
- `AgeGroup`
- `ZipCode3Digits`
- `Gender`
- `Race`
- `Ethnicity`
- `RaceEthnicity`
- `LengthOfStay`
- `AdmissionType`
- `PatientDisposition`
- `DischargeYear`
- `CCSRDiagnosisCode`
- `CCSRDiagnosisDescription`
- `CCSRProcedureCode`
- `CCSRProcedureDescription`
- `APRDRGCode`
- `APRDRGDescription`
- `APRMDCCode`
- `APRMDCDescription`
- `APRSeverityOfIllnessCode`
- `APRSeverityOfIllnessDescription`
- `APRRiskOfMortality`
- `APRMedicalSurgicalDescription`
- `PaymentTypology1`
- `PaymentTypology2`
- `PaymentTypology3`
- `BirthWeight`
- `EmergencyDepartmentIndicator`
- `TotalCharges`
- `TotalCosts`

`data/sample/inpatient_sample.csv` is synthetic development data only. Cleaned SPARCS development files under `data/processed/` are generated locally and ignored by Git.
