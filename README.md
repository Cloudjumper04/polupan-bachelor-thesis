# SmartEnergy Lab — Battery Charging Automation Module

Bachelor thesis software project.

Official topic:

**Програмний модуль автоматизації режимів зарядки акумуляторних батарей лабораторного стенду з сонячною генерацією енергії.**

## Overview

This project implements a simulation-based software system for SmartEnergy Lab.

The system will simulate a solar-powered laboratory stand and provide charging automation logic through an API. The main goal is to demonstrate how battery charging modes can be selected and controlled based on solar generation, battery state, load, and system configuration.

## Planned stack

- Python / FastAPI backend
- React frontend
- YAML station configuration
- SQLite telemetry storage
- Docker deployment

## Current tasks

1. Station configuration
2. Solar generation simulation
3. Battery and load simulation
4. Charging automation logic
5. API endpoints
6. Web dashboard
7. Docker setup

## Data pipeline maintenance

The `data-scheduler` service maintains SQLite dashboard data through the host
directory `./backend/data`, mounted into the container at `/app/backend/data`.
It updates weather, solar, grid, Load, Battery, and EMS data through the
dependency-ordered one-shot pipeline.

Manual one-shot update:

```bash
python backend/scripts/update_data_pipeline.py
```

Long-running scheduler:

```bash
python backend/scripts/run_data_pipeline_scheduler.py
```

Build the scheduler image:

```bash
docker compose build data-scheduler
```

Start the scheduler:

```bash
docker compose up -d data-scheduler
```

View scheduler logs:

```bash
docker compose logs -f data-scheduler
```

Stop the scheduler:

```bash
docker compose stop data-scheduler
```

Fallback source values are disabled by default. Use `--allow-fallbacks` only for
deliberate demo or test runs where synthetic fallback data is acceptable.
