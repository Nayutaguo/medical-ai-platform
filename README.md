# Smart Medical Big Data and AI Analysis Platform

This repository contains the phase-1 product foundation for a hospital inpatient discharge data analysis platform.

The current product path is constrained to authenticated, facility-scoped
aggregate analysis with a minimum-group privacy boundary:

```text
QuerySpec JSON
  -> validation
  -> safe SQLAlchemy Core query compilation
  -> MySQL executor
  -> repository and service layers
  -> versioned Flask JSON API
  -> React/MUI/ECharts workbench
```

The current working loop also includes an OpenAI-compatible LLM-first Agent that returns intent, safe `QuerySpec`, chart recommendation, execution steps, and a short result interpretation. The frontend is retained and incrementally productized rather than rewritten. Spark/Hadoop and Python code execution are reserved for later phases.

## Project Root

The formal project lives in `medical-ai-platform/` because the parent workspace already contains design documents, raw data, and a read-only `.git/` directory that cannot be initialized safely.

## Environment

Conda owns the Python runtime. `pyproject.toml` owns Python package dependencies.

```bash
conda env create -f environment.yml
conda activate medical-ai
```

For an existing environment:

```bash
conda activate medical-ai
pip install -e ".[dev]"
```

## Configuration

```bash
cp .env.example .env
```

Edit `.env` locally. Never commit `.env`.

## MySQL

Docker is optional. The current macOS development machine uses a local MySQL
instance; the same `MYSQL_*` contract works with Docker or a package-managed
MySQL installation.

Ubuntu/WSL local MySQL route, when applicable:

```bash
sudo apt-get update
sudo apt-get install -y mysql-server mysql-client
sudo service mysql start
```

Create database and project user from `.env`:

```bash
set -a
source .env
set +a
sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}'; CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}'; GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'localhost'; GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_USER}'@'127.0.0.1'; FLUSH PRIVILEGES;"
```

The local development password is stored only in `.env` as `MYSQL_PASSWORD`. Do not commit `.env`.

Docker route, if Docker is available later:

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d mysql
```

Load synthetic development data:

```bash
conda run -n medical-ai python scripts/load_sample_mysql.py --replace
```

Clean a 1000-row development subset from the real SPARCS CSV:

```bash
conda run -n medical-ai python scripts/clean_sparcs_csv.py --limit 1000 --output data/processed/inpatient_sparcs_2021_clean_1000.csv
```

The subset manifest has `source_complete=false` and cannot replace the governed
live table. For the full SPARCS file, omit `--limit`. `data/processed/*` is
ignored by Git.

Fast full-load path:

```bash
sudo mysql -e "SET GLOBAL local_infile = 1; SHOW GLOBAL VARIABLES LIKE 'local_infile';"
conda run -n medical-ai python scripts/load_sparcs_mysql.py \
  --csv data/processed/inpatient_sparcs_2021_clean.csv \
  --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json \
  --dry-run
conda run -n medical-ai python scripts/load_sparcs_mysql.py \
  --csv data/processed/inpatient_sparcs_2021_clean.csv \
  --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json \
  --method load-data
```

If `local_infile` cannot be enabled, use the slower batch-insert path:

```bash
conda run -n medical-ai python scripts/load_sparcs_mysql.py \
  --csv data/processed/inpatient_sparcs_2021_clean.csv \
  --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json \
  --method insert
```

The full loader requires a successful complete cleaning manifest, loads an
isolated staging table, verifies quality and indexes, and atomically publishes
the new table while retaining the previous live table for rollback. It writes a
task-specific private append-only JSON Lines audit. Detailed acceptance facts
and recovery behavior are in [`docs/DATA_CLEANING.md`](docs/DATA_CLEANING.md) and
[`docs/adr/0004-recoverable-full-data-publish.md`](docs/adr/0004-recoverable-full-data-publish.md).

Verify:

```bash
RUN_MYSQL_TESTS=1 conda run -n medical-ai pytest tests/integration
conda run -n medical-ai python scripts/demo_query_mysql.py data/sample/queryspec_avg_charges_by_age.json
```

## Control-plane database migration

Identity, authorization, session, audit, and background-job tables are managed
through Alembic. The migration reads the existing `MYSQL_*` environment values;
credentials are never stored in `alembic.ini`.

```bash
conda run -n medical-ai alembic upgrade head
conda run -n medical-ai alembic current
```

The existing `inpatient` analytics table is an external baseline and is never
dropped by the control-plane migration. The reviewed rollback is available as
`alembic downgrade 001_existing_analytics_baseline`; it destroys control-plane
data and must not be run as a routine operation.

The browser-session and invited-registration contract is documented in
[`docs/API_AUTH.md`](docs/API_AUTH.md). Tenant administration endpoints,
permissions, optimistic versions, and facility-scope rules are documented in
[`docs/API_ADMIN.md`](docs/API_ADMIN.md).

Create the first organization administrator once, after migration. The password
is prompted securely and there is no shipped default credential:

```bash
conda run -n medical-ai python scripts/bootstrap_admin.py \
  --email admin@example.com \
  --display-name "Platform Administrator" \
  --organization-name "Hospital A" \
  --organization-slug hospital-a
```

The bootstrap administrator receives governance permissions only. Analytical
permissions and facility access must be assigned explicitly after data scopes
are configured.

Before the first facility-catalog synchronization, bind the imported inpatient
dataset to that organization in the ignored local `.env`:

```dotenv
INPATIENT_DATASET_OWNER_ORGANIZATION_ID=<organization-uuid>
```

The value is the organization ID shown by the bootstrap command. A missing or
different value makes synchronization fail closed; it is never inferred from
the first administrator who clicks the button.

Additional users register only from an administrator-issued, one-time
invitation. An authenticated member with `users.manage` can issue it from the
management workspace or `POST /api/v1/admin/invitations`. A trusted local
operator can also use the control-plane command when recovering or verifying a
development environment:

```bash
conda run -n medical-ai python scripts/create_user_invitation.py \
  --organization-id <organization-uuid> \
  --email analyst@example.com
```

The command prints the bearer invitation once. Transfer it through an approved
secret channel; never place it in Git, tickets, logs, or environment examples.
The invited user enters it on the frontend **注册** tab. Registration activates
only the user and organization membership; it grants no role or facility scope.
The management workspace separately assigns an organization role and an
explicit facility scope; an empty facility scope denies all medical-data access.

## Tests

```bash
pytest
```

Optional MySQL integration tests:

```bash
RUN_MYSQL_TESTS=1 pytest tests/integration
```

Current verified baseline: 428 tests pass, the normal suite skips nine
external integrations, and all nine MySQL integrations pass when explicitly
enabled. The frontend has 12 Vitest/Testing Library checks and also passes
`tsc --noEmit` and `npm run build`.

## Minimal Demo

Compile QuerySpec JSON to safe parameterized MySQL SQL without a database:

```bash
python scripts/demo_compile_query.py data/sample/queryspec_avg_charges_by_age.json
```

Execute against configured MySQL:

```bash
python scripts/demo_query_mysql.py data/sample/queryspec_avg_charges_by_age.json
```

Use the configured OpenAI-compatible LLM to plan and execute a query:

```bash
conda run -n medical-ai python scripts/demo_llm_query.py "2021年50到69岁和70岁以上患者的平均总费用是多少，按年龄组排序" --execute
```

Run the LLM-first Agent path with intent, chart recommendation, MySQL execution, and result interpretation:

```bash
conda run -n medical-ai python scripts/demo_agent_query.py "2021年50到69岁和70岁以上患者的平均总费用是多少，按年龄组排序"
```

Required local `.env` values:

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

## MCP Server

MCP is a local-development transport only at present. Product environments set
`MCP_ALLOW_UNSCOPED_TOOLS=false`; the server and every tool fail closed until an
authenticated principal adapter can provide the same `AccessContext` as HTTP.

Default stdio transport:

```bash
python -m medical_ai.mcp_server.server
```

Streamable HTTP transport:

```bash
MCP_TRANSPORT=streamable-http MCP_PORT=3001 python -m medical_ai.mcp_server.server
```

Available tools:

- `get_database_schema`
- `get_distinct_values`
- `query_medical_data`

Validated Streamable HTTP client demo:

```bash
conda run -n medical-ai python scripts/demo_mcp_http_client.py --url http://127.0.0.1:3001/mcp
```

## Web Application

The web frontend uses React + Vite + MUI + ECharts, following the workbench pattern from the MIT-licensed Data Formulator project. It shows schema, distinct values, Agent intent, QuerySpec, execution metadata, result table, chart, and written insight.

Start backend API:

```bash
conda run -n medical-ai python scripts/run_demo_server.py --host 127.0.0.1 --port 8000
```

Local loopback Gunicorn entry point:

```bash
conda run -n medical-ai gunicorn --config gunicorn.conf.py medical_ai.api.dev_wsgi:app
```

The development WSGI entry point is intentionally anonymous and must remain
bound to loopback. To display and exercise the real login/session/RBAC path
locally after bootstrapping the first administrator, use the authenticated
development entry point instead:

```bash
conda run -n medical-ai gunicorn --config gunicorn.conf.py medical_ai.api.auth_dev_wsgi:app
```

This authenticated development entry point is also loopback-only. It uses
plain-HTTP cookies for local testing, keeps unscoped MCP disabled, and does not
create a default account or password. The production entry point is
`medical_ai.api.wsgi:app`; it refuses to start unless authentication is
enforced, secure cookies are enabled, Redis login protection is configured, and
unscoped MCP tools are disabled.

```bash
APP_ENVIRONMENT=production \
AUTH_ENFORCEMENT_ENABLED=true \
AUTH_SESSION_COOKIE_SECURE=true \
MCP_ALLOW_UNSCOPED_TOOLS=false \
RATE_LIMIT_ENABLED=true \
REDIS_URL=rediss://redis.internal:6379/0 \
RATE_LIMIT_KEY_SECRET='<at-least-32-random-bytes>' \
conda run -n medical-ai gunicorn --config gunicorn.conf.py medical_ai.api.wsgi:app
```

`gunicorn.conf.py` keeps the WSGI timeout above the bounded LLM retry window. Override its local defaults with
`GUNICORN_*` environment variables when needed.

For OpenAI-compatible providers, `LLM_BASE_URL` must be the API base path expected before `/chat/completions`
(commonly a URL ending in `/v1`). The client validates JSON response envelopes, retries bounded transient failures,
and returns stable 502/504 API errors instead of exposing provider responses.

Versioned endpoints:

- `GET /api/v1/health`
- `GET /api/v1/health/live`
- `GET /api/v1/health/ready`
- `POST /api/v1/auth/registrations`
- `POST /api/v1/auth/sessions`
- `GET /api/v1/auth/me`
- `DELETE /api/v1/auth/sessions/current`
- `GET /api/v1/admin/members`
- `GET /api/v1/admin/roles`
- `GET /api/v1/admin/facilities`
- `POST /api/v1/admin/invitations`
- `PUT /api/v1/admin/members/{membership_id}/roles`
- `PUT /api/v1/admin/members/{membership_id}/facility-scope`
- `PATCH /api/v1/admin/members/{membership_id}/status`
- `POST /api/v1/admin/facilities/sync`
- `GET /api/v1/schema`
- `GET /api/v1/distinct`
- `POST /api/v1/query`
- `POST /api/v1/ask`

Every response uses the stable `success/data/meta/error` envelope and returns an
`X-Request-Id` header. Governed analytics require exact permissions, a non-empty
trusted facility scope, approved field capabilities, and groups at or above the
privacy threshold. See `docs/API_AUTH.md` and ADR 0003.
Administration is tenant-bound, requires exact `users.manage`, `roles.assign`,
or `imports.create` permissions by operation, and requires CSRF plus
`expected_version` on membership writes. See `docs/API_ADMIN.md`.
This is still a pre-production slice: privileged MFA/step-up, password reset,
authenticated browser acceptance, and the production deployment chain remain.

Start frontend:

```bash
cd frontend
npm install --no-audit --no-fund
npm run dev -- --host 127.0.0.1 --port 5173
```

Open:

```text
http://127.0.0.1:5173
```

## Documentation

Start every future Codex session by reading:

1. `AGENTS.md`
2. `docs/PROJECT_STATUS.md`
3. Relevant files in `docs/ROADMAP.md` and `docs/DECISIONS.md`
