# Authentication and governed analytics API

Base path: `/api/v1`.

All responses use the `success/data/meta/error` envelope, carry an
`X-Request-Id`, and use `Cache-Control: no-store`. Production startup refuses an
anonymous configuration. The anonymous development WSGI entry point is
restricted to loopback; `auth_dev_wsgi` enables this authentication contract
over loopback HTTP for local verification.

## Browser credentials

- The authentication credential is an opaque random value in an `HttpOnly`,
  `Secure`, `SameSite=Lax` cookie with `Path=/api/v1`.
- Only the token digest is stored in MySQL.
- A separate non-HttpOnly CSRF proof cookie has `Path=/` so the same-origin SPA
  can restore it into module memory after refresh. It is not stored in web
  storage and cannot authenticate without the HttpOnly session cookie.
- State-changing governed requests copy that proof to `X-CSRF-Token`; the server
  compares its digest with the active session.
- Default idle expiry is 30 minutes and absolute expiry is 12 hours. User or
  membership authorization-version changes invalidate old sessions.

## Complete invited registration

`POST /api/v1/auth/registrations`

Registration is invitation-only. The server never provides open self-signup and
never assigns an analytics role or facility scope during activation. A global
email identity may belong to multiple organizations:

The first release accepts ASCII email identifiers only. It trims and
case-folds them and rejects internationalized email addresses (EAI) until a
binary-normalized database identity policy is introduced. Registration maps an
unsupported email to the same public invalid-invitation response.

- On its first acceptance, an `invited` identity sets its Argon2id password and
  display name, activates the identity, and activates only the token-bound
  membership.
- When the identity is already `active`, the submitted password must verify the
  existing credential. Acceptance activates only the new membership and never
  replaces the password, display name, `auth_version`, or existing sessions.

```json
{
  "invitation_token": "single-use bearer invitation",
  "email": "analyst@example.com",
  "display_name": "Analyst",
  "password": "user supplied passphrase"
}
```

The invitation is bound to one user, organization, membership, purpose,
identity version, expiry, and email. MySQL stores only its digest. Consumption,
the required identity or membership activation, and the success audit row occur
in one transaction. When the first password is established, other unconsumed
and unexpired invitations for that identity are transactionally rebased to the
new identity version; accepting them then requires the existing password.
Wrong passwords, expired or replayed tokens, cross-email use, stale snapshots,
and failed compare-and-swap writes share the same public error. Password policy
is checked before token lookup so invalid short passwords cannot be used as a
token-validity oracle.

Success is HTTP 201:

```json
{
  "success": true,
  "data": {
    "registered": true,
    "organization_id": "token-bound-organization-uuid"
  },
  "meta": {"request_id": "request-id"},
  "error": null
}
```

`organization_id` is safe, token-bound login context. The client should pass it
to `POST /api/v1/auth/sessions` after registration because a global identity may
now have more than one active membership.

If two organizations' invitations for the same not-yet-activated identity are
accepted concurrently, exactly one request establishes the global identity.
The lock victim receives the same `INVALID_INVITATION` response and may retry
its still-valid, rebased token using the newly established password. The server
does not automatically replay a transaction after password verification.

Stable errors:

- `400 INVALID_REQUEST`: missing, oversized, or unsupported fields.
- `400 PASSWORD_POLICY_VIOLATION`: password policy failure with a password field
  error; policy is evaluated before invitation lookup and passwords are never
  echoed.
- `400 INVALID_INVITATION`: one indistinguishable response for every unusable
  invitation, including a wrong existing-account password.
- `429 LOGIN_RATE_LIMITED`: the shared source/account authentication bucket is
  exhausted; includes `Retry-After`.
- `503 LOGIN_RATE_LIMIT_UNAVAILABLE`: enabled Redis protection cannot decide;
  registration fails before password hashing.
- `503 DATABASE_UNAVAILABLE`: activation cannot be committed.

The frontend keeps invitation and password values only in component memory,
clears them after success, returns to the login tab, and prefills the
token-bound organization ID. An activated account without
`analytics.schema.read` sees a permission-pending state instead of an
unguarded workbench.

## Create session

`POST /api/v1/auth/sessions`

```json
{
  "email": "analyst@example.com",
  "password": "user supplied password",
  "organization_id": "optional-organization-uuid"
}
```

The optional organization is required when one identity has more than one active
membership. Success is HTTP 201 and returns public session facts plus the CSRF
proof. The raw session token is never returned in JSON.

Login uses the same ASCII-only, trimmed, case-folded email identity policy as
invitation and registration. Unsupported or malformed identifiers receive the
same `INVALID_CREDENTIALS` response as an unknown account.

```json
{
  "success": true,
  "data": {
    "session": {
      "user": {
        "id": "user-uuid",
        "email": "analyst@example.com",
        "display_name": "Analyst"
      },
      "organization": {
        "id": "organization-uuid",
        "name": "Hospital A",
        "membership_id": "membership-uuid"
      },
      "permissions": [
        "analytics.schema.read",
        "analytics.distinct.read",
        "analytics.query.execute",
        "analytics.agent.execute"
      ],
      "expires_at": "2026-08-24T22:00:00",
      "idle_expires_at": "2026-08-24T10:30:00"
    },
    "csrf_token": "one-session-csrf-proof",
    "csrf_cookie_name": "medical_ai_csrf",
    "csrf_header_name": "X-CSRF-Token"
  },
  "meta": {"request_id": "request-id"},
  "error": null
}
```

Before Argon2 verification, Redis atomically consumes source-IP and normalized-
account fixed-window buckets. Redis sees HMAC keys, not raw IPs or email values.
The direct peer address is used; forwarded headers are not trusted without a
separately reviewed reverse-proxy boundary.

Stable errors:

- `400 INVALID_REQUEST`: malformed JSON or bounded field input.
- `401 INVALID_CREDENTIALS`: identical response for unknown, disabled, or
  mismatched credentials.
- `403 MEMBERSHIP_UNAVAILABLE`: requested membership is unavailable.
- `409 ORGANIZATION_REQUIRED`: more than one active membership requires a choice.
- `429 LOGIN_RATE_LIMITED`: either login bucket is exhausted; includes
  `Retry-After`.
- `503 LOGIN_RATE_LIMIT_UNAVAILABLE`: enabled Redis protection could not decide;
  login fails closed before password verification.
- `503 DATABASE_UNAVAILABLE`: control-plane persistence is unavailable.

## Read current session

`GET /api/v1/auth/me`

Requires the opaque session cookie and returns the same public session shape,
plus the configured `csrf_cookie_name` and `csrf_header_name`. The SPA uses
those public names to restore the proof into memory after refresh, so a valid
deployment-specific name does not break subsequent writes. This endpoint does
not return the proof value, verifiers, token digests, facility scope internals,
or database metadata.

Stable errors:

- `401 AUTHENTICATION_REQUIRED`: absent, expired, revoked, or version-stale
  session.
- `404 AUTHENTICATION_DISABLED`: explicit anonymous loopback development mode.
- `503 DATABASE_UNAVAILABLE`: control-plane persistence is unavailable.

## Revoke current session

`DELETE /api/v1/auth/sessions/current`

Requires the opaque cookie and `X-CSRF-Token`. Success stores `revoked_at`,
expires both cookies, and returns HTTP 200.

```json
{
  "success": true,
  "data": {"revoked": true},
  "meta": {"request_id": "request-id"},
  "error": null
}
```

Stable errors:

- `401 AUTHENTICATION_REQUIRED`.
- `403 CSRF_VALIDATION_FAILED`.
- `503 DATABASE_UNAVAILABLE`.

## Governed analytics permissions

When `AUTH_ENFORCEMENT_ENABLED=true`, the following endpoints resolve the active
session and use only the membership's trusted facility allowlist:

| Endpoint | Required permission | CSRF |
| --- | --- | --- |
| `GET /schema` | `analytics.schema.read` | no |
| `GET /distinct` | `analytics.distinct.read` | no |
| `POST /query` | `analytics.query.execute` | yes |
| `POST /ask` | Agent + Schema + Distinct + Query | yes |

The Agent requirement is intentionally composite because planning may select any
of its three tools. A management role does not imply analytics access.

Every governed query also requires a non-empty facility scope and passes the
field-capability and minimum-group policies in ADR 0003. Client JSON and LLM
output cannot supply or broaden the trusted scope. Governed responses do not
include compiled SQL, SQL parameters, internal privacy columns, suppressed-group
counts, or trusted facility identifiers.

Stable governed errors include:

- `401 AUTHENTICATION_REQUIRED`.
- `403 PERMISSION_DENIED` for permission, organization, or empty-scope denial.
- `403 CSRF_VALIDATION_FAILED` on protected writes.
- `400 INVALID_QUERY_SPEC` for field capability, filter, aggregation, or
  QuerySpec rejection.

## Production configuration

For a loopback-only browser check without production TLS or Redis, start the
explicit authenticated development entry point after running the one-time
administrator bootstrap:

```bash
conda run -n medical-ai gunicorn --config gunicorn.conf.py medical_ai.api.auth_dev_wsgi:app
```

It enables the same session and authorization routes, but intentionally uses a
non-Secure cookie on `http://127.0.0.1`. It must never be bound to a public
interface and it never provisions a default credential.

At minimum, production WSGI requires:

```text
APP_ENVIRONMENT=production
AUTH_ENFORCEMENT_ENABLED=true
AUTH_SESSION_COOKIE_SECURE=true
MCP_ALLOW_UNSCOPED_TOOLS=false
RATE_LIMIT_ENABLED=true
REDIS_URL=rediss://...
RATE_LIMIT_KEY_SECRET=<at least 32 random UTF-8 bytes>
```

Production still requires TLS termination, trusted proxy configuration,
least-privilege database accounts, MFA for privileged users, audit integration,
monitoring, and recovery testing before release.
