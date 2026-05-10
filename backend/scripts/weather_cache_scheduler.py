from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import update_weather_cache


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_HISTORY_START = date(2025, 10, 6)
DEFAULT_DAYS_AHEAD = 2
DEFAULT_INTERVAL_HOURS = 12.0
SECONDS_PER_HOUR = 60 * 60


@dataclass(frozen=True)
class SchedulerSettings:
    config: Path
    database_url: str
    history_start: date
    days_ahead: int
    interval_hours: float


MaintenanceRunner = Callable[
    [Path, str | None, date | None, int],
    update_weather_cache.WeatherCacheSummary,
]
SleepFunction = Callable[[float], None]


def main() -> None:
    settings = parse_args()
    run_forever(settings)


def parse_args(argv: Sequence[str] | None = None) -> SchedulerSettings:
    parser = argparse.ArgumentParser(
        description="Run weather cache maintenance immediately and then on a schedule.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument(
        "--history-start",
        type=_parse_date,
        default=DEFAULT_HISTORY_START,
    )
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD)
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=DEFAULT_INTERVAL_HOURS,
    )
    args = parser.parse_args(argv)

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.interval_hours <= 0:
        parser.error("--interval-hours must be greater than 0")

    return SchedulerSettings(
        config=args.config,
        database_url=args.database_url,
        history_start=args.history_start,
        days_ahead=args.days_ahead,
        interval_hours=args.interval_hours,
    )


def run_forever(
    settings: SchedulerSettings,
    maintenance_runner: MaintenanceRunner = update_weather_cache.update_weather_cache,
    sleep: SleepFunction = time.sleep,
) -> None:
    interval_seconds = settings.interval_hours * SECONDS_PER_HOUR
    while True:
        run_once(settings, maintenance_runner)
        _log(
            "weather cache scheduler sleeping "
            f"for {settings.interval_hours:g} hours"
        )
        sleep(interval_seconds)


def run_once(
    settings: SchedulerSettings,
    maintenance_runner: MaintenanceRunner = update_weather_cache.update_weather_cache,
) -> bool:
    _log(
        "weather cache update started "
        f"config={settings.config} "
        f"database_url={settings.database_url} "
        f"history_start={settings.history_start.isoformat()} "
        f"days_ahead={settings.days_ahead}"
    )
    try:
        summary = maintenance_runner(
            settings.config,
            settings.database_url,
            settings.history_start,
            settings.days_ahead,
        )
    except Exception as exc:
        _log_error(f"weather cache update failed: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return False

    _log(
        "weather cache update completed "
        f"historical_rows_inserted={summary.historical_rows_inserted} "
        f"forecast_rows_inserted={summary.forecast_rows_inserted} "
        f"validation_result={summary.validation_result}"
    )
    return True


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _log(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", file=sys.stderr, flush=True)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
