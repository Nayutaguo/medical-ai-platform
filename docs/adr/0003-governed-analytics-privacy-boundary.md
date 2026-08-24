# ADR 0003: Governed analytics and privacy boundary

- Status: Accepted and implemented for authenticated HTTP analytics
- Date: 2026-08-24
- Owners: platform backend, data governance, and security

## Context

The product analyses hospital discharge records. Ordinary RBAC is necessary but
not sufficient: an analyst may be allowed to run an aggregate query while still
being restricted to particular facilities, and a syntactically safe aggregate
can disclose a person through a small group, an extreme value, a high-cardinality
dimension, or repeated overlapping queries.

The existing `QuerySpec` validator and parameterized SQL compiler prevent raw SQL
execution. This ADR defines the additional policy applied to authenticated
product traffic. Anonymous legacy methods remain available only in the explicit
loopback development entry point and are forbidden by production startup
validation.

## Decision

### 1. Authorization and data scope are server-owned inputs

Every authenticated analytics request resolves an `AccessContext`. The service
checks an exact permission and intersects the requested scope with the active
membership's facility allowlist. An empty intersection is a denial, never an
unrestricted query.

Facility identifiers are passed to `compile_scoped_query` as a trusted argument.
They cannot be supplied, removed, or broadened by HTTP JSON, QuerySpec, prompts,
model output, frontend state, or MCP arguments. SQL values remain bound
parameters.

### 2. Minimum groups are enforced in SQL before ordering and limiting

Authenticated queries use `PRIVACY_MIN_GROUP_SIZE`, initially `5`. The compiler
adds an internal `COUNT(*)` and a parameterized `HAVING COUNT(*) >= k` before
`ORDER BY` and `LIMIT`. This prevents suppressed groups from consuming the result
limit and avoids revealing how many small groups existed.

The application applies a second defensive filter and always removes internal
privacy columns. Public metadata contains only allowlisted configuration facts;
it does not expose suppressed-group counts, raw executor row counts, unsafe
`truncated` values, compiled SQL, or trusted facility parameters. A query with no
matching rows and one whose only groups are below `k` have the same public shape
and the same model-insight input.

### 3. A central field capability matrix constrains product queries

All 34 inpatient fields have one governed capability entry containing:

- sensitivity and expected cardinality;
- whether the field may be enumerated with `distinct`;
- whether it may appear as a selected/grouped dimension;
- allowed aggregate functions;
- allowed filter operators.

The initial conservative policy permits common categorical dimensions such as
age group, gender, admission type, payment type, and discharge year. It permits
`COUNT`, and `SUM`/`AVG` for approved numeric measures. It rejects `MIN` and
`MAX`, direct enumeration of charges, costs, birth weight, ZIP prefixes,
diagnosis/procedure descriptions, and facility identifiers, and grouping by
high-cardinality or re-identifying fields.

Diagnosis/procedure classifications may be used only as exact cohort filters;
they cannot be returned or used with wildcard search. User facility filters are
not accepted because the trusted facility predicate is authoritative. A single
governed query is limited to three dimensions and six filter clauses. Filter
values are type checked, bounded, finite, non-nested, and parameterized.

### 4. Agent access cannot bypass tool permissions or field policy

The first product policy treats the Agent as a composite capability. Entry
requires all four explicit permissions:

- `analytics.agent.execute`;
- `analytics.schema.read`;
- `analytics.distinct.read`;
- `analytics.query.execute`.

The model may choose a tool only after those checks. Every scoped Agent query is
then validated against the same capability matrix, facility predicate, and
minimum-group policy used by the HTTP query service. Results below the privacy
threshold are removed before chart construction or a second LLM call.

### 5. Transport responses do not become a disclosure channel

All `/api/v1` responses, including errors, use `Cache-Control: no-store` and do
not return SQL, database diagnostics, credentials, raw model responses, or
patient-level rows. Errors use stable categories and do not echo rejected filter
values. Product MCP starts and executes fail-closed until an authenticated
principal adapter can supply the same `AccessContext`.

## Consequences

This policy makes the authenticated aggregate path materially safer and keeps
HTTP and Agent behavior consistent. Some exploratory queries that are valid SQL
are intentionally unavailable until data governance approves a narrower
capability.

The policy does **not** claim formal differential privacy. Repeated overlapping
queries can still support differencing attacks, and averages or sums can be
combined with outside knowledge. Before production release, the platform still
requires durable query audit, per-user/organization query budgets, shared rate
limits, cache keys containing tenant/scope/version context, and monitoring for
repeated cohort refinement. If stronger guarantees are required, rounding,
minimum cohort deltas, contribution bounds, and formal privacy accounting will
be introduced as a separate versioned policy.

## Verification requirements

Tests must cover empty and cross-organization scopes, parameter binding, groups
on both sides of the threshold, `HAVING` placement before `LIMIT`,
indistinguishable empty/suppressed responses, field capability coverage, unsafe
distinct and extreme aggregates, nested or oversized filter values, Agent
permission bypass,
and the absence of trusted scope or internal privacy aliases from public and LLM
payloads.
