# Frontend

React + Vite + MUI + ECharts frontend for the authenticated medical aggregate
analysis workbench. It keeps the original dense three-column layout and adds
only the product controls required for the training delivery:

- login, invitation registration, session restore, logout, change password,
  forgot-password request, and token-based reset;
- exact-permission workbench controls and permission-pending state;
- membership-scoped query history, favorites, deletion, and QuerySpec reuse;
- local CSV, chart PNG, and HTML report downloads for the current aggregate
  result;
- member/facility administration, custom roles/permissions, and a redacted
  audit-event query page.

Authentication/CSRF remains enforced by the backend. Sensitive tokens and
credentials stay in component/module memory and are never persisted in browser
local or session storage.

## Run

Start the backend API from the project root:

```bash
conda run -n medical-ai alembic upgrade head
conda run -n medical-ai gunicorn --config gunicorn.conf.py medical_ai.api.auth_dev_wsgi:app
```

`auth_dev_wsgi` is loopback-only and uses non-Secure cookies solely for local
HTTP verification. It does not create a default account. Bootstrap an
administrator and assign analytical roles/facilities as described in the root
README.

For a password-reset demonstration, the local `.env` may set:

```dotenv
AUTH_DEV_EXPOSE_PASSWORD_RESET_TOKEN=true
```

The issued token is shown once in the reset flow. Production rejects this flag
and requires a separate approved email/SMS delivery adapter.

Start the Vite dev server:

```bash
npm run dev -- --host 127.0.0.1 --port 5173
```

Open:

```text
http://127.0.0.1:5173
```

## Build

```bash
npm test
npx tsc --noEmit
npm run build
```

After a successful build, `scripts/run_demo_server.py` can also serve `frontend/dist` directly.

## Export boundary

CSV, PNG, and HTML report files are created in the browser from the currently
displayed, already authorized aggregate result. CSV cells are guarded against
spreadsheet-formula execution, HTML text is escaped, truncated results are
marked, and a PNG is offered only when an ECharts instance exists. This is a
training convenience feature: it is not a server-side export job, does not
perform a second permission check, and does not append an export audit event.
Production export/reporting must use a server-authorized, audited job workflow.

See [`../docs/API_HISTORY_EXPORTS.md`](../docs/API_HISTORY_EXPORTS.md) for the
history API and export behavior, [`../docs/API_AUTH.md`](../docs/API_AUTH.md)
for account flows, and [`../docs/API_ADMIN.md`](../docs/API_ADMIN.md) for role
and audit permissions.
