# SmartEnergy Lab - Battery Charging Automation Module

Bachelor thesis software project for a simulation-based SmartEnergy Lab stand with solar generation, battery/load/grid simulation, EMS logic, API access, and a React dashboard.

Official topic:

**Software module for automation of battery charging modes of SmartEnergy Lab with solar generation.**

## Prerequisites

Required:

- Git
- Docker Desktop with Docker Compose

Optional:

- Python virtual environment for local backend commands and tests
- Node.js/npm only if you want to run or build the frontend outside Docker

The project is designed to run through Docker Compose. Local Python and Node are useful for development, but they are not required for the normal demo run.

## Clone And Enter Project

```bash
git clone <repository-url>
cd ProjectFolder
```

On Windows, run commands from the repository root, for example:

```powershell
cd Q:\KPI\Дипломка\ProjectFolder
```

## Configuration

Station configuration lives in:

```text
backend/config/station.default.yaml
```

Default backend and scheduler config path:

```text
backend/config/station.default.yaml
```

Runtime database path:

```text
backend/data/smartenergy.db
```

Relevant environment variables:

- `SMARTENERGY_CONFIG_PATH`: optional backend API config path override. Default: `backend/config/station.default.yaml`.
- `SMARTENERGY_DATABASE_URL`: optional backend API database URL override. Default: `sqlite:///backend/data/smartenergy.db`.
- `VITE_API_PROXY_TARGET`: frontend Docker proxy target. In Docker Compose it is set to `http://backend-api:6001` so the frontend container does not call the backend through `localhost`.

The scheduler scripts also support command-line overrides such as `--config`, `--database-url`, and `--db-path`.

## Run With Docker Compose

Build and start all services:

```bash
docker compose up -d --build
```

Service names:

- `backend-api`
- `frontend`
- `data-scheduler`

Container names:

- `smartenergy-backend-api`
- `smartenergy-frontend`
- `smartenergy-data-scheduler`

Local URLs:

- Frontend: `http://localhost:5173`
- Backend API: `http://localhost:6001`

Useful Docker commands:

```bash
docker compose ps
docker compose logs -f backend-api
docker compose logs -f frontend
docker compose logs -f data-scheduler
docker compose down
```

## Data Scheduler

The `data-scheduler` service maintains the demo data in `backend/data/smartenergy.db`.

It updates and maintains:

- weather cache
- solar source data and interpolated solar cache
- grid availability
- load simulation data
- battery simulation data
- EMS dashboard data

The scheduler runs automatically with Docker Compose. The current Compose configuration starts it with a 360-minute main pipeline interval, plus its internal fast solar-cache refresh cycle.

Fallback source values are disabled by default. Use `--allow-fallbacks` only for deliberate demo/test runs where synthetic fallback source values are acceptable.

Large full-history rebuilds should not be treated as routine scheduler writes to the active runtime database. Use the swap-DB workflow described below: build data into a separate DB file, verify it, back up the current runtime DB, stop services that use the DB, then swap files only after validation.

## Manual One-Shot Update

These commands use the Docker scheduler image and mounted `backend/data` directory.

Dry-run, no writes:

```bash
docker compose run --rm --no-deps data-scheduler python backend/scripts/update_data_pipeline.py --dry-run
```

Real one-shot update, writes to the default runtime DB:

```bash
docker compose run --rm --no-deps data-scheduler python backend/scripts/update_data_pipeline.py
```

Optional custom DB path:

```bash
docker compose run --rm --no-deps data-scheduler python backend/scripts/update_data_pipeline.py --db-path backend/data/smartenergy_rebuild.db
```

Optional local Windows venv equivalents:

```powershell
.\.venv\Scripts\python.exe backend\scripts\update_data_pipeline.py --dry-run
.\.venv\Scripts\python.exe backend\scripts\update_data_pipeline.py
.\.venv\Scripts\python.exe backend\scripts\update_data_pipeline.py --db-path backend\data\smartenergy_rebuild.db
```

Before any command that intentionally mutates `backend/data/smartenergy.db`, create a backup.

## Backup And DB Safety

Runtime DB:

```text
backend/data/smartenergy.db
```

Backup script:

```powershell
powershell -ExecutionPolicy Bypass -File .\backend\scripts\backup_runtime_db.ps1
```

Use the backup script before:

- manual real one-shot updates against the runtime DB
- schema-changing work
- full-history rebuilds
- any operation where failed writes could affect the demo database

Backups are written under:

```text
backend/data/backups
```

Short swap-DB workflow for large rebuilds:

1. Create a backup of `backend/data/smartenergy.db`.
2. Generate or rebuild into a separate DB, for example `backend/data/smartenergy_rebuild.db` using `--db-path`.
3. Verify the rebuilt DB with focused API or script checks.
4. Stop services that read/write the runtime DB.
5. Rename/swap the DB files only after validation.
6. Restart services and check the dashboard/API.

Do not repair a damaged runtime DB in place if a backup is available.

## Useful API Checks

With Docker Compose running, open these URLs in a browser or call them with `curl`:

```text
http://localhost:6001/api/system/dashboard
http://localhost:6001/api/dashboard/range
http://localhost:6001/api/solar/dashboard
http://localhost:6001/api/grid/current
```

Example:

```bash
curl http://localhost:6001/api/system/dashboard
```

## Tests And Builds

Backend tests from the project virtual environment on Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests
```

Frontend production build through Docker Node, useful when local npm is unavailable:

```powershell
docker run --rm -v "${PWD}\frontend:/app" -w /app node:22-alpine npm run build
```

If local Node/npm is installed, the frontend can also be built from `frontend/`:

```bash
cd frontend
npm install
npm run build
```

## Troubleshooting

Stale frontend or backend containers:

```bash
docker compose up -d --build backend-api frontend data-scheduler
```

Scheduler diagnostics:

```bash
docker compose logs -f data-scheduler
```

`database disk image is malformed`:

- Stop services that use the DB.
- Restore the latest good backup from `backend/data/backups`.
- Do not attempt to repair the active runtime DB in place.

Backend API is unavailable:

```bash
docker compose ps
docker compose logs -f backend-api
```

Frontend cannot reach backend in Docker:

- Confirm the `frontend` service has `VITE_API_PROXY_TARGET=http://backend-api:6001`.
- Do not configure the frontend container to call `localhost` for the backend.

## Notes For Development

- Backend code lives under `backend/app/`.
- Backend scripts live under `backend/scripts/`.
- Frontend code lives under `frontend/`.
- UI references live under `docs/ui/`.
- Runtime data lives under `backend/data/` and should not be deleted as cleanup.
