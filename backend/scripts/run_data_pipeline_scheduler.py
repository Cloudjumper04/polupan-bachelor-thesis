from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import update_data_pipeline


DEFAULT_CONFIG_PATH = Path("backend/config/station.default.yaml")
DEFAULT_DATABASE_URL = "sqlite:///backend/data/smartenergy.db"
DEFAULT_LOCK_PATH = Path("backend/data/pipeline.lock")
DEFAULT_INTERVAL_MINUTES = 360.0
DEFAULT_SOLAR_CACHE_INTERVAL_MINUTES = 5.0
DEFAULT_SOURCE_DAYS_AHEAD = update_data_pipeline.DEFAULT_SOURCE_DAYS_AHEAD
DEFAULT_GRID_DAYS_AHEAD = update_data_pipeline.DEFAULT_GRID_DAYS_AHEAD


PipelineRunner = Callable[..., object]
CacheRunner = Callable[..., object]


class PipelineLockHeld(RuntimeError):
    def __init__(self, lock_path: Path, details: str) -> None:
        super().__init__(f"pipeline lock already exists: {lock_path} {details}".strip())
        self.lock_path = lock_path
        self.details = details


@dataclass(frozen=True)
class SchedulerSettings:
    config: Path
    database_url: str | None
    history_start: date | None
    source_days_ahead: int
    grid_days_ahead: int
    full_history: bool
    allow_fallbacks: bool
    dry_run: bool
    interval_minutes: float
    solar_cache_interval_minutes: float
    run_once: bool
    lock_path: Path


class PipelineFileLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._acquired = False

    def __enter__(self) -> PipelineFileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            f"pid={os.getpid()}\n"
            f"created_utc={datetime.now(timezone.utc).isoformat()}\n"
        ).encode("utf-8")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.lock_path), flags)
        except FileExistsError as exc:
            raise PipelineLockHeld(self.lock_path, _read_lock_details(self.lock_path)) from exc
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        self._acquired = True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._acquired = False


def main() -> None:
    settings = parse_args()
    if settings.allow_fallbacks:
        _log_error(
            "warning: --allow-fallbacks is enabled; generated dashboard data may "
            "use synthetic fallback values"
        )

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    exit_code = run_scheduler(settings, stop_event=stop_event)
    raise SystemExit(exit_code)


def parse_args(argv: Sequence[str] | None = None) -> SchedulerSettings:
    parser = argparse.ArgumentParser(
        description="Run the SmartEnergy dependency-ordered data pipeline scheduler.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--history-start", type=_parse_date, default=None)
    parser.add_argument(
        "--source-days-ahead",
        "--days-ahead",
        dest="source_days_ahead",
        type=int,
        default=DEFAULT_SOURCE_DAYS_AHEAD,
    )
    parser.add_argument(
        "--grid-days-ahead",
        type=int,
        default=DEFAULT_GRID_DAYS_AHEAD,
    )
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Pass through to the one-shot pipeline. Keep explicit for rebuilds.",
    )
    parser.add_argument(
        "--allow-fallbacks",
        action="store_true",
        help="Allow source fallbacks. Off by default; intended only for demo/test use.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=DEFAULT_INTERVAL_MINUTES,
        help="Main full pipeline interval. Default: 360 minutes.",
    )
    parser.add_argument(
        "--solar-cache-interval-minutes",
        type=float,
        default=DEFAULT_SOLAR_CACHE_INTERVAL_MINUTES,
        help="Fast interpolated solar cache refresh interval. Default: 5 minutes.",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run one scheduler cycle and exit. Intended for smoke tests.",
    )
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_LOCK_PATH,
        help="Cross-process scheduler lock file path.",
    )
    args = parser.parse_args(argv)

    if args.source_days_ahead < 0:
        parser.error("--source-days-ahead must be 0 or greater")
    if args.grid_days_ahead < 0:
        parser.error("--grid-days-ahead must be 0 or greater")
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be greater than 0")
    if args.solar_cache_interval_minutes <= 0:
        parser.error("--solar-cache-interval-minutes must be greater than 0")

    return SchedulerSettings(
        config=args.config,
        database_url=_database_url_from_args(args),
        history_start=args.history_start,
        source_days_ahead=args.source_days_ahead,
        grid_days_ahead=args.grid_days_ahead,
        full_history=args.full_history,
        allow_fallbacks=args.allow_fallbacks,
        dry_run=args.dry_run,
        interval_minutes=args.interval_minutes,
        solar_cache_interval_minutes=args.solar_cache_interval_minutes,
        run_once=args.run_once,
        lock_path=args.lock_path,
    )


def run_scheduler(
    settings: SchedulerSettings,
    *,
    pipeline_runner: PipelineRunner | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    runner = pipeline_runner or update_data_pipeline.run_data_pipeline
    shutdown = stop_event or threading.Event()
    _log(
        "data pipeline scheduler started "
        f"database_url={settings.database_url} "
        f"config={settings.config} "
        f"interval_minutes={settings.interval_minutes:g} "
        f"solar_cache_interval_minutes={settings.solar_cache_interval_minutes:g} "
        f"run_once={settings.run_once} "
        f"dry_run={settings.dry_run} "
        f"full_history={settings.full_history} "
        f"allow_fallbacks={settings.allow_fallbacks} "
        f"lock_path={settings.lock_path}"
    )

    cycle_ok = run_pipeline_cycle(settings, pipeline_runner=runner)
    last_pipeline = time.monotonic()
    last_solar_cache = last_pipeline if cycle_ok else 0.0
    if settings.run_once:
        _log("data pipeline scheduler run-once mode exiting")
        return 0 if cycle_ok else 1

    pipeline_interval_seconds = settings.interval_minutes * 60.0
    solar_cache_interval_seconds = settings.solar_cache_interval_minutes * 60.0
    while not shutdown.is_set():
        now_monotonic = time.monotonic()
        if now_monotonic - last_pipeline >= pipeline_interval_seconds:
            cycle_ok = run_pipeline_cycle(settings, pipeline_runner=runner)
            last_pipeline = time.monotonic()
            if cycle_ok:
                last_solar_cache = last_pipeline
            continue

        if now_monotonic - last_solar_cache >= solar_cache_interval_seconds:
            run_solar_cache_cycle(settings)
            last_solar_cache = time.monotonic()
            continue

        next_pipeline_seconds = pipeline_interval_seconds - (now_monotonic - last_pipeline)
        next_cache_seconds = solar_cache_interval_seconds - (now_monotonic - last_solar_cache)
        sleep_seconds = max(0.1, min(5.0, next_pipeline_seconds, next_cache_seconds))
        shutdown.wait(sleep_seconds)

    _log("data pipeline scheduler stopped")
    return 0


def run_pipeline_cycle(
    settings: SchedulerSettings,
    *,
    pipeline_runner: PipelineRunner | None = None,
) -> bool:
    runner = pipeline_runner or update_data_pipeline.run_data_pipeline
    cycle_started = time.monotonic()
    _log(
        "data pipeline cycle started "
        f"database_url={settings.database_url} "
        f"dry_run={settings.dry_run} "
        f"full_history={settings.full_history} "
        f"allow_fallbacks={settings.allow_fallbacks}"
    )
    try:
        with PipelineFileLock(settings.lock_path):
            summary = runner(
                config_path=settings.config,
                database_url=settings.database_url,
                history_start=settings.history_start,
                source_days_ahead=settings.source_days_ahead,
                grid_days_ahead=settings.grid_days_ahead,
                full_history=settings.full_history,
                allow_fallbacks=settings.allow_fallbacks,
                dry_run=settings.dry_run,
            )
            update_data_pipeline.print_data_pipeline_summary(summary)
    except PipelineLockHeld as exc:
        _log_error(f"data pipeline cycle skipped: {exc}")
        return False
    except Exception as exc:
        _log_error(f"data pipeline cycle failed: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return False

    duration_seconds = time.monotonic() - cycle_started
    _log(f"data pipeline cycle completed duration_seconds={duration_seconds:.2f}")
    return True


def run_solar_cache_cycle(
    settings: SchedulerSettings,
    *,
    cache_runner: CacheRunner | None = None,
) -> bool:
    runner = (
        cache_runner
        or update_data_pipeline.solar_data_scheduler.run_fast_interpolated_solar_cache_refresh
    )
    _log(
        "interpolated solar cache refresh started "
        f"database_url={settings.database_url} "
        f"dry_run={settings.dry_run}"
    )
    if settings.dry_run:
        _log("interpolated solar cache refresh dry-run skipped writes")
        return True

    cycle_started = time.monotonic()
    try:
        with PipelineFileLock(settings.lock_path):
            summary = runner(settings.config, settings.database_url)
    except PipelineLockHeld as exc:
        _log_error(f"interpolated solar cache refresh skipped: {exc}")
        return False
    except Exception as exc:
        _log_error(f"interpolated solar cache refresh failed: {exc}")
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        return False

    duration_seconds = time.monotonic() - cycle_started
    _log(
        "interpolated solar cache refresh completed "
        f"rows={getattr(summary, 'rows', 0)} "
        f"windows={getattr(summary, 'windows', 0)} "
        f"start_utc={_optional_iso(getattr(summary, 'start_utc', None))} "
        f"end_utc={_optional_iso(getattr(summary, 'end_utc', None))} "
        f"duration_seconds={duration_seconds:.2f}"
    )
    return True


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_shutdown(signum: int, frame: object) -> None:
        _log(f"data pipeline scheduler shutdown requested signal={signum}")
        stop_event.set()

    for signal_name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, signal_name, None)
        if signum is None:
            continue
        signal.signal(signum, request_shutdown)


def _database_url_from_args(args: argparse.Namespace) -> str:
    if args.db_path is None:
        return args.database_url
    return f"sqlite:///{args.db_path}"


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _read_lock_details(lock_path: Path) -> str:
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return "(existing lock could not be read)"
    return f"({content})" if content else "(existing lock is empty)"


def _log(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", flush=True)


def _log_error(message: str) -> None:
    print(f"{_utc_timestamp()} {message}", file=sys.stderr, flush=True)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _optional_iso(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else "none"


if __name__ == "__main__":
    main()
