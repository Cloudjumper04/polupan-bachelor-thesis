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

## Docker solar data maintenance

The solar data scheduler persists SQLite data through the host directory
`./backend/data`, mounted into the container at `/app/backend/data`.

Build the scheduler image:

```bash
docker compose build weather-scheduler
```

Run the full scheduler in the foreground:

```bash
docker compose run --rm weather-scheduler python backend/scripts/solar_data_scheduler.py --history-start 2025-10-06 --days-ahead 2 --interval-hours 12
```

Start the scheduler:

```bash
docker compose up -d weather-scheduler
```

View scheduler logs:

```bash
docker compose logs -f weather-scheduler
```

Stop the scheduler:

```bash
docker compose stop weather-scheduler
```
