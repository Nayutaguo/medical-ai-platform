# Environment

The table below records the original 2026-08-18 WSL baseline. The active
2026-08-25 acceptance was run on macOS with the same Conda/MySQL configuration
contract; current product and data facts are listed after the table and in
`docs/PROJECT_STATUS.md`.

## System

| Check | Result | Status | Phase 1 need |
|---|---|---|---|
| `uname -a` | `Linux LAPTOP-PLALSJSA 6.6.87.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC Thu Jun 5 18:30:46 UTC 2025 x86_64 GNU/Linux` | present | yes |
| `/etc/os-release` | Ubuntu 24.04.3 LTS | present | yes |
| `which conda` | `/home/nayuta1/miniforge3/condabin/conda` | present | yes |
| `conda --version` | `conda 26.1.1` | present | yes |
| `conda env list` | `base`, `captcha`, `medical-ai`, `vis`, `vit` | present; `medical-ai` created | yes |
| `python --version` | command not found outside Conda | missing globally | yes, via Conda env |
| `python3 --version` | `Python 3.12.3` | present | fallback only |
| `git --version` | `git version 2.43.0` | present | yes |
| `node --version` | `v24.15.0` | present | later frontend |
| `npm --version` | `11.12.1` | present | later frontend |
| `docker --version` | command not found | missing | yes for preferred MySQL |
| `docker compose version` | command not found | missing | yes for preferred MySQL |
| `java -version` | OpenJDK 21.0.11 | present | later Spark only |
| `mysql --version` | `mysql Ver 8.0.46-0ubuntu0.24.04.3 for Linux on x86_64 ((Ubuntu))` | present | yes |

## Current Phase Requirements

Required now:

- Conda
- Python 3.11 environment
- Git
- MySQL service; local WSL MySQL or Docker Compose are both supported

Available now:

- Conda
- Git
- Node/npm
- Java
- System `python3`
- MySQL client/server
- Frontend npm dependencies under `frontend/node_modules`

Missing now:

- `python` command outside Conda
- Docker / Docker Compose

Created now:

- Conda env `medical-ai` at `/home/nayuta1/miniforge3/envs/medical-ai`
- Python 3.11.15 in `medical-ai`
- Local MySQL database `medical_ai`
- Local MySQL user `medical_ai`
- `inpatient` live table loaded with 2,101,588 accepted SPARCS 2021 rows
- Prior 1,000-row table retained as the task-specific rollback table documented
  in `docs/DATA_CLEANING.md`
- React/Vite/MUI/ECharts authenticated workbench in `frontend/`

Deferred:

- Hadoop
- Spark cluster
- Redis service deployment/HA (the Python client and fail-closed login limiter
  are implemented)
- production frontend hosting (the production build itself passes)
- model weights
- pandas for EDA/profiling; current cleaning scripts use streaming stdlib CSV for low memory use

## Conda Responsibility Split

- `environment.yml` declares the reproducible Conda runtime: environment name, Python 3.11, and pip bootstrap.
- `pyproject.toml` declares project Python package dependencies.

Do not add `requirements.txt` unless a future decision explains why it is needed.

## Network Notes

Initial GitHub access inside the sandbox failed because the environment tried `127.0.0.1:7897` as a proxy. Escalated `git ls-remote` and shallow probe clones to `/tmp` succeeded for third-party research.

Initial MySQL connection check failed with `ConnectionRefusedError: [Errno 111] Connection refused`.

After local MySQL installation and setup, these commands were verified:

```bash
mysql -h 127.0.0.1 -P 3306 -u "$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "SELECT 1;"
conda run -n medical-ai python scripts/load_sample_mysql.py --replace
conda run -n medical-ai python scripts/clean_sparcs_csv.py --output data/processed/inpatient_sparcs_2021_clean.csv
conda run -n medical-ai python scripts/load_sparcs_mysql.py --csv data/processed/inpatient_sparcs_2021_clean.csv --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json --dry-run
conda run -n medical-ai python scripts/load_sparcs_mysql.py --csv data/processed/inpatient_sparcs_2021_clean.csv --source-manifest data/processed/inpatient_sparcs_2021_clean.manifest.json --method load-data
RUN_MYSQL_TESTS=1 conda run -n medical-ai pytest tests/integration
conda run -n medical-ai python scripts/demo_query_mysql.py data/sample/queryspec_avg_charges_by_age.json
conda run -n medical-ai python scripts/demo_llm_query.py "2021年50到69岁和70岁以上患者的平均总费用是多少，按年龄组排序" --execute
MCP_TRANSPORT=streamable-http MCP_PORT=3002 conda run -n medical-ai python -m medical_ai.mcp_server.server
conda run -n medical-ai python scripts/demo_mcp_http_client.py --url http://127.0.0.1:3002/mcp
conda run -n medical-ai python scripts/run_demo_server.py --host 127.0.0.1 --port 8000
npm --prefix frontend install --no-audit --no-fund
npm --prefix frontend run build
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

Credential note: local generated database credentials are stored in `.env`, which is ignored by Git. Real password values are not recorded in documentation.

LLM credential note: DeepSeek/OpenAI-compatible settings are also local-only. `LLM_API_KEY` is set in `.env`; `.env.example` contains only `change_me`.

Network note: LLM demos require outbound HTTPS access. The managed sandbox blocks normal socket creation, so Codex needs escalated execution for those demos. One run hit a transient TLS EOF; the minimal LLM client now retries network-level failures up to two times.

MySQL bulk-load note: `LOAD DATA LOCAL INFILE` was enabled for the 2026-08-25
local full-data acceptance. Changing this server setting is an explicit database
administration action:

```bash
sudo mysql -e "SET GLOBAL local_infile = 1; SHOW GLOBAL VARIABLES LIKE 'local_infile';"
```

If it is unavailable in another environment, `--method auto` falls back to the
bounded insert path, or `--method insert` can require that path. Both still
require the successful complete cleaning manifest.
