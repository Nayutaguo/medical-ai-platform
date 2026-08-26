# Analysis history, favorites, and local exports

Base history path: `/api/v1/history`.

This completion slice stores a small, replayable record of successful governed
analysis. It does not store result rows, compiled SQL, SQL parameters, trusted
facility IDs, suppressed-group metadata, or the original natural-language
Agent question. Every repository operation is bound to the authenticated
`organization_id`, `membership_id`, and `user_id` from `AccessContext`.

Apply migration `007_demo_completion` before using these endpoints:

```bash
conda run -n medical-ai alembic upgrade head
```

The migration creates `analysis_history` with tenant/membership foreign keys,
descending history and favorite indexes, and optimistic versions. It also adds
the `password_reset` one-time-token purpose. Its downgrade preflight refuses to
contract that purpose while password-reset rows exist, and only then removes
the history table.

## Automatic recording

A successful authenticated `POST /api/v1/query` records a `query` entry; a
successful authenticated `POST /api/v1/ask` records an `agent` entry. Anonymous
loopback demo queries do not record history. History persistence is deliberately
outside the core analytics failure domain: if the optional history write fails,
the already completed aggregate response is still returned.

Stored fields are limited to:

- generated title and history kind;
- validated QuerySpec, when available;
- declarative Agent ChartSpec with model-authored free-text title/reason replaced
  by stable copy;
- aggregate row count, truncation flag, query duration, favorite state,
  timestamps, and optimistic version.

The original question and result rows are always absent. A history row is not a
result cache: reusing it submits its QuerySpec through the normal current
permission, facility-scope, field-capability, and minimum-group checks.

## List history

`GET /api/v1/history`

Query parameters:

- `cursor`: optional positive decimal history ID; the next page contains lower
  IDs.
- `limit`: 1 to 100, default 20.
- `favorite`: optional exact `true` or `false` filter.

The caller must have `analytics.query.execute`, `analytics.agent.execute`, or
both. Entries whose kind is no longer covered by the caller's current exact
permissions are not returned.

```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "42",
        "kind": "query",
        "title": "AgeGroup · patient_count",
        "question": null,
        "query_spec": {
          "table": "inpatient",
          "filters": [],
          "group_by": ["AgeGroup"],
          "metrics": [{"field": "*", "agg": "count", "alias": "patient_count"}],
          "order_by": [],
          "limit": 100
        },
        "chart_spec": null,
        "row_count": 8,
        "truncated": false,
        "query_time_ms": 18.4,
        "is_favorite": true,
        "created_at": "2026-08-26T09:00:00.000000Z",
        "updated_at": "2026-08-26T09:01:00.000000Z",
        "version": 2
      }
    ],
    "next_cursor": null
  },
  "meta": {"request_id": "request-id"},
  "error": null
}
```

IDs are strings in the browser contract because MySQL `BIGINT` can exceed
JavaScript's safe integer range.

## Set or clear a favorite

`PATCH /api/v1/history/{history_id}/favorite`

Requires the session CSRF proof and an optimistic version. `version` and
`expected_version` are accepted aliases, but must not both be sent.

```json
{"is_favorite": true, "version": 1}
```

Success returns the complete updated entry with an incremented version. The
entry must belong to the exact current membership context and its kind must
still be visible under the caller's current analytics permissions.

## Delete a history entry

`DELETE /api/v1/history/{history_id}`

Requires the session CSRF proof. Success returns:

```json
{"deleted": true, "id": "42"}
```

Deletion is permanent for that metadata row; no aggregate result rows are
stored or deleted because history never persists them.

## Stable history errors

| HTTP | Code | Meaning |
| --- | --- | --- |
| 400 | `INVALID_HISTORY_REQUEST` | Invalid cursor, limit, favorite value, or mutation body |
| 401 | `AUTHENTICATION_REQUIRED` | Missing, expired, revoked, or version-stale session |
| 403 | `CSRF_VALIDATION_FAILED` | Missing or incorrect CSRF proof on a mutation |
| 403 | `PERMISSION_DENIED` | Current membership lacks permission for any requested/visible history kind |
| 404 | `HISTORY_NOT_FOUND` | Entry is absent, outside the exact context, or no longer visible |
| 409 | `HISTORY_VERSION_CONFLICT` | Favorite update used a stale optimistic version |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | Favorite mutation did not use `application/json` |
| 503 | `DATABASE_UNAVAILABLE` | Control-plane persistence is unavailable |

All responses use the common envelope, `X-Request-Id`, and
`Cache-Control: no-store` contract.

## Browser-local exports

The workbench offers three downloads after a successful aggregate result:

- **CSV**: includes the returned columns/rows, quotes every cell, prefixes
  spreadsheet-formula-like values, emits UTF-8 with BOM, and adds `-partial` to
  the filename when the API says the result was truncated.
- **PNG**: uses the current controlled ECharts instance at 2x pixel ratio and is
  available only when a chartable result exists.
- **HTML report**: includes generation time, current analysis question,
  validated QuerySpec when available, aggregate summary/observations, execution
  warnings, optional chart image, returned table, truncation notice,
  limitations, and the non-clinical-use disclaimer. All text is HTML-escaped.

These files are generated entirely in the browser from the already authorized
response currently in memory. There is no export API endpoint, background job,
second server permission decision, durable artifact, server-side retention
policy, or export audit event. The report may include the current question
because the user explicitly downloads it, even though that question is not
persisted in history.

This is suitable only as an explicit training/demo convenience. A production
export/report feature must move generation to a server-authorized job with
tenant/scope/dataset-version binding, `exports.create`-style permission,
sanitized audit events, retention/deletion rules, idempotency, download
authorization, size/time limits, and worker failure recovery.
