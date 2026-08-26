# Organization administration API

Base path: `/api/v1/admin`.

This API administers exactly the organization selected by the caller's active
membership. It is not a cross-tenant platform-administrator interface. Every
route is unavailable when authentication enforcement is disabled, and every
repository query or mutation is bound to the trusted `organization_id` in the
server-side `AccessContext`; organization identifiers are never accepted from
request JSON.

The invited-registration and browser-session contract is documented separately
in [`API_AUTH.md`](API_AUTH.md).

## Shared HTTP contract

- Every request requires the opaque `HttpOnly` session cookie.
- Every state-changing request also requires the active session's CSRF proof in
  `X-CSRF-Token`.
- Write requests use `Content-Type: application/json`. Unknown JSON fields are
  rejected instead of ignored.
- Every response uses the `success/data/meta/error` envelope, includes the same
  request identifier in `meta.request_id` and `X-Request-Id`, and has
  `Cache-Control: no-store`.
- Identifiers in path or JSON inputs are non-empty strings of at most 36
  characters. They are always resolved inside the active organization.
- Before every mutation, the repository locks the organization row and then
  revalidates the exact requesting session, active/non-deleted user and
  membership, identity and authorization versions, and current effective
  permission. A permission removed while a request waits for the lock cannot
  authorize the write.

Successful responses have this shape:

```json
{
  "success": true,
  "data": {},
  "meta": {
    "request_id": "request-id",
    "query_time_ms": 4.2,
    "total_time_ms": 4.2,
    "dimensions": [],
    "metrics": []
  },
  "error": null
}
```

Failures set `data` to `null` and return a stable `error.code`. Stack traces,
SQL, cross-tenant object details, passwords, token digests, and database
diagnostics are not returned.

## Exact permission matrix

Permissions are additive, exact codes. A governance permission never implies
analytics access.

| Endpoint | Required permission | CSRF |
| --- | --- | --- |
| `GET /members` | `users.manage` **or** `roles.assign` | no |
| `GET /roles` | `roles.assign` | no |
| `GET /facilities` | `roles.assign` **or** `imports.create` | no |
| `POST /invitations` | `users.manage` | yes |
| `PUT /members/{membership_id}/roles` | `roles.assign` | yes |
| `PUT /members/{membership_id}/facility-scope` | `roles.assign` | yes |
| `PATCH /members/{membership_id}/status` | `users.manage` | yes |
| `POST /facilities/sync` | `imports.create` **or** `roles.assign` | yes |

The stable permission-code registry currently contains:

- `analytics.query.execute`
- `analytics.agent.execute`
- `analytics.schema.read`
- `analytics.distinct.read`
- `users.manage`
- `roles.assign`
- `audit.read`
- `imports.create`

The one-time bootstrap creates an `organization_admin` role with the four
governance permissions (`users.manage`, `roles.assign`, `audit.read`, and
`imports.create`) and an unassigned `data_analyst` role with the four analytics
permissions. The bootstrap administrator therefore has no medical-data access
until an analytics role and explicit facility scope are assigned.

For a caller changing their own roles or facility scope, the successful write
rebases only the exact requesting session to the new authorization version so a
follow-up management request can continue. Every other session for that
membership remains on the old version and fails closed. Changes to another
member invalidate all of that member's old-version sessions.

## Pagination

The three catalog endpoints use bounded keyset pagination:

```text
?cursor=<last-returned-id>&limit=50
```

- `limit` defaults to 50 and must be between 1 and 100.
- `cursor` is optional, non-empty, and at most 36 characters.
- `data.next_cursor` is `null` when the page is complete.
- Members are ordered by `membership_id`; roles and facilities are ordered by
  their `id`.

## List organization members

`GET /api/v1/admin/members`

Returns memberships only from the active organization, together with their
current roles and explicit facility grants.

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "membership_id": "membership-uuid",
        "user_id": "user-uuid",
        "email": "analyst@example.com",
        "display_name": "Analyst",
        "user_status": "active",
        "membership_status": "active",
        "authorization_version": 3,
        "version": 4,
        "roles": [
          {
            "id": "role-uuid",
            "role_key": "data_analyst",
            "name": "Data Analyst",
            "description": "Read-only governed analytics",
            "permissions": [
              "analytics.agent.execute",
              "analytics.distinct.read",
              "analytics.query.execute",
              "analytics.schema.read"
            ],
            "version": 1
          }
        ],
        "facilities": [
          {
            "id": "facility-uuid",
            "facility_key": "1001",
            "display_name": "Example Medical Center",
            "status": "active",
            "version": 1
          }
        ]
      }
    ],
    "next_cursor": null
  },
  "meta": {
    "request_id": "request-id",
    "query_time_ms": 4.2,
    "total_time_ms": 4.2,
    "dimensions": [],
    "metrics": []
  },
  "error": null
}
```

`version` is the optimistic-write version expected by the mutation endpoints.
`authorization_version` changes whenever the member's effective role, facility
scope, or membership status changes; existing sessions bound to the older
authorization version then fail closed.

## List assignable roles

`GET /api/v1/admin/roles`

Returns only roles owned by the active organization.

```json
{
  "items": [
    {
      "id": "role-uuid",
      "role_key": "data_analyst",
      "name": "Data Analyst",
      "description": "Read-only governed analytics",
      "permissions": [
        "analytics.agent.execute",
        "analytics.distinct.read",
        "analytics.query.execute",
        "analytics.schema.read"
      ],
      "version": 1
    }
  ],
  "next_cursor": null
}
```

This slice lists and assigns existing roles. It does not create, rename, delete,
or edit role permission definitions.

## List organization facilities

`GET /api/v1/admin/facilities`

Returns the canonical facilities currently owned by the active organization.

```json
{
  "items": [
    {
      "id": "facility-uuid",
      "facility_key": "1001",
      "display_name": "Example Medical Center",
      "status": "active",
      "version": 1
    }
  ],
  "next_cursor": null
}
```

`facility_key` corresponds to the governed analytics value derived from
`inpatient.PermanentFacilityId`. Clients must submit the internal facility
`id`, not `facility_key`, when replacing a member's scope.

## Issue an invitation

`POST /api/v1/admin/invitations`

```json
{
  "email": "new.analyst@example.com",
  "lifetime_hours": 24
}
```

- The first release accepts ASCII email identifiers only. They are trimmed,
  case-folded for identity matching, and limited to 320 bytes. Internationalized
  email addresses (EAI) are rejected until the identity column uses a separately
  reviewed binary normalization and collation policy.
- `lifetime_hours` is an integer from 1 through 168.
- A new email creates one global `invited` identity and an `invited` membership.
  An existing healthy `invited` or `active` global identity may receive a new
  `invited` membership in another organization. Both cases return the same
  HTTP 201 shape, preventing another organization from probing whether the
  email already exists.
- While a membership remains `invited`, member-list and mutation responses use
  the normalized invitation email plus fixed `Invited user` / `invited`
  placeholders. They never reveal an existing global identity's display name
  or activation status to the inviting organization.
- Reissuing an invitation for the same organization's `invited` membership
  invalidates its previous unconsumed token. A same-organization `active`,
  `suspended`, or `removed` membership returns `409 INVITATION_CONFLICT` and is
  never silently replaced.
- Invitations never grant a role or facility scope. An already-active identity
  must prove control with its existing password when accepting; acceptance
  cannot change its password, display name, identity version, or sessions.

Success is HTTP 201. The bearer token is returned only in this response:

```json
{
  "success": true,
  "data": {
    "token": "single-use-secret",
    "expires_at": "2026-08-26T09:00:00+00:00",
    "membership_id": "membership-uuid"
  },
  "meta": {
    "request_id": "request-id",
    "query_time_ms": 4.2,
    "total_time_ms": 4.2,
    "dimensions": [],
    "metrics": []
  },
  "error": null
}
```

Only the token digest is persisted. The raw token must be transferred through
an approved secret channel and must not be placed in Git, tickets, logs,
analytics, browser storage, or environment examples. The invitee completes
`POST /api/v1/auth/registrations`; wrong, expired, replayed, stale, or
email-mismatched tokens all return the same public registration error.

## Mutation audit facts

Every successful management mutation appends its business change and audit row
in the same database transaction. Role replacement records sorted previous and
new role keys. Membership status records previous and new status. Facility
scope replacement records previous/new counts plus SHA-256 digests of the
sorted internal ID sets; raw facility IDs are not copied into audit details.
Failed validation, authorization, optimistic-version, or last-manager guards do
not append a success audit row.

## Replace member roles

`PUT /api/v1/admin/members/{membership_id}/roles`

This is replacement, not merge, semantics.

```json
{
  "role_ids": ["role-uuid"],
  "expected_version": 4
}
```

- `role_ids` must be an array containing at most 16 unique role IDs.
- Every role must belong to the active organization.
- An empty array removes every role. If the caller is changing their own
  membership, the replacement must retain at least one role containing both
  `users.manage` and `roles.assign`; otherwise the request fails closed.
- `expected_version` must be a positive integer and must equal the current
  membership `version`.

Success returns the complete refreshed member representation, including the new
versions and current roles/facilities. For example:

```json
{
  "membership_id": "membership-uuid",
  "user_id": "user-uuid",
  "email": "analyst@example.com",
  "display_name": "Analyst",
  "user_status": "active",
  "membership_status": "active",
  "authorization_version": 4,
  "version": 5,
  "roles": [
    {
      "id": "role-uuid",
      "role_key": "data_analyst",
      "name": "Data Analyst",
      "description": "Read-only governed analytics",
      "permissions": [
        "analytics.agent.execute",
        "analytics.distinct.read",
        "analytics.query.execute",
        "analytics.schema.read"
      ],
      "version": 1
    }
  ],
  "facilities": []
}
```

## Replace member facility scope

`PUT /api/v1/admin/members/{membership_id}/facility-scope`

This is also replacement semantics.

```json
{
  "facility_ids": ["facility-uuid"],
  "expected_version": 5
}
```

- `facility_ids` must be an array containing at most 1,000 unique IDs.
- Every selected facility must be active and owned by the current organization.
- An empty array is valid and means deny-all: the member cannot query any
  governed medical data.
- A role grant alone is insufficient for analytics access; an explicit non-empty
  facility scope is also required.

Success returns the complete refreshed member representation. If a client
performs role and facility updates sequentially, it must pass the `version`
returned by the first request as `expected_version` in the second.

## Change membership status

`PATCH /api/v1/admin/members/{membership_id}/status`

```json
{
  "status": "suspended",
  "expected_version": 4
}
```

Accepted status values are `active` and `suspended`. This changes the
organization membership; it does not delete the user identity. Suspending a
membership increments its authorization version, so existing sessions for that
membership become invalid. A caller cannot suspend their own active membership
through this endpoint. Success returns the complete refreshed member
representation.

## Synchronize the facility catalog

`POST /api/v1/admin/facilities/sync`

The request body must be an empty JSON object:

```json
{}
```

The current single-database implementation derives a bounded catalog from
distinct, non-empty `inpatient.PermanentFacilityId` values and their facility
names, then creates or reuses canonical `facilities` rows and binds them to the
active organization in one transaction.

The imported dataset must first be bound to exactly one organization through
`INPATIENT_DATASET_OWNER_ORGANIZATION_ID`. The configured value must equal the
active session's trusted organization ID. An empty value or a different tenant
fails closed before the analytics table is scanned; this prevents another
organization from claiming a shared facility catalog on a first-writer basis.

```json
{
  "created_count": 2,
  "existing_count": 315
}
```

The operation is idempotent for an unchanged catalog. It rejects more than
10,000 source facilities, keys longer than 128 characters, names longer than
200 characters, and any attempt to claim a canonical facility already owned by
another organization. A conflict rolls back the whole synchronization. Catalog
synchronization does not grant any facility to a member; scopes remain explicit.

If analytics and control storage are separated later, this operation requires a
durable staged handoff. The current transaction must not be described as an
atomic cross-database operation.

## Optimistic concurrency and tenant safety

Every membership mutation locks the organization-bound membership and compares
`expected_version` before replacing grants or status. A stale value returns:

```json
{
  "success": false,
  "data": null,
  "meta": {
    "request_id": "request-id",
    "query_time_ms": 4.2,
    "total_time_ms": 4.2,
    "dimensions": [],
    "metrics": []
  },
  "error": {
    "code": "ADMIN_VERSION_CONFLICT",
    "message": "管理对象已被其他请求修改，请刷新后重试"
  }
}
```

The client must refresh the member and let the operator reconcile the new
state; it must not blindly replay with a guessed version. Cross-organization
membership IDs return the same not-found response as unknown IDs. Role and
facility selections outside the active organization return a scope conflict
without revealing the other tenant's state.

Successful invitation issuance, role replacement, facility-scope replacement,
membership-status changes, and facility synchronization append sanitized audit
facts in the same MySQL transaction as their control-plane mutation. Complete
attempt/failure audit coverage and a transactional outbox remain release work.

## Stable errors

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `ADMIN_INVALID_REQUEST` | Missing, malformed, duplicate, oversized, or unsupported input |
| 401 | `AUTHENTICATION_REQUIRED` | Missing, expired, revoked, or version-stale session |
| 403 | `CSRF_VALIDATION_FAILED` | Missing or incorrect CSRF proof on a write |
| 403 | `PERMISSION_DENIED` | Active membership lacks the exact required permission |
| 404 | `AUTHENTICATION_DISABLED` | Administration is unavailable in anonymous development mode |
| 404 | `ADMIN_RESOURCE_NOT_FOUND` | Object is absent or outside the active organization |
| 409 | `INVITATION_CONFLICT` | Invitation cannot be created for the requested identity state |
| 409 | `ADMIN_VERSION_CONFLICT` | `expected_version` is stale |
| 409 | `ADMIN_SCOPE_CONFLICT` | Selected role or facility is outside the active tenant/scope |
| 409 | `ADMIN_SELF_LOCKOUT` | Mutation would remove the caller's active management path |
| 409 | `ADMIN_LAST_MANAGER` | Mutation would remove or suspend the organization's final active full manager |
| 409 | `ADMIN_STATUS_CONFLICT` | An invited or removed membership cannot be activated through the status toggle |
| 409 | `FACILITY_OWNERSHIP_CONFLICT` | A canonical facility belongs to another organization |
| 409 | `FACILITY_CATALOG_SCOPE_UNAVAILABLE` | The inpatient dataset is not explicitly bound to the active organization |
| 409 | `FACILITY_CATALOG_INVALID` | Analytics facility source violates catalog limits |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | A write did not use `application/json` |
| 503 | `DATABASE_UNAVAILABLE` | Control-plane persistence is unavailable |

## Browser management workspace

The React workspace displays the **管理** view only when the current session has
`users.manage`, `roles.assign`, or `imports.create`. Controls are hidden by
permission for usability, but the backend permission checks remain the security
boundary.

The current workspace supports:

- paged member, role, and facility catalogs;
- one-time invitation issuance without local/session-storage persistence;
- role and facility-scope replacement using the returned optimistic version;
- member suspension and reactivation;
- explicit facility-catalog synchronization.

The UI caps automatic catalog traversal at 20 pages and asks the operator to
narrow or refresh rather than issuing an unbounded request.

## Current release limits

This management slice is not a production deployment. The following remain:

- privileged-account MFA or step-up authentication;
- password reset and broader account-recovery/lifecycle policy;
- role-definition creation/editing and an audit-ledger read API/UI;
- production TLS/proxy, secrets, least-privilege database identities,
  monitoring, backup/restore drills, and authenticated browser E2E acceptance;
- complete attempt/failure audit and durable outbox integration;
- cross-tenant and real-MySQL integration coverage for every administration
  mutation and outage path.

The repository currently has unit-contract coverage in
`tests/unit/test_admin_api.py` and
`tests/unit/test_governance_administration.py`. Invitation single-use,
tenant-binding, digest-only persistence, and unprivileged activation also have
an opt-in real-MySQL integration test in
`tests/integration/test_invitation_registration_mysql.py`.
