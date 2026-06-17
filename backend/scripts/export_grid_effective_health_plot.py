from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from typing import Sequence
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = Path(__file__).resolve().parents[1]
for path in (SCRIPTS_DIR, BACKEND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.config_loader import load_config
from app.simulation.grid import (
    DEFAULT_GRID_HISTORY_START,
    GridAvailabilityPoint,
    generate_grid_availability_points,
)
from generate_grid_availability import grid_settings_from_config


DEFAULT_CONFIG_PATH = BACKEND_DIR / "config" / "station.default.yaml"
DEFAULT_OUTPUT_PATH = Path(
    r"Q:\KPI\Дипломка\config&other_data\data_preview\grid_effective_health_history.png"
)


@dataclass(frozen=True)
class PlotSummary:
    output_path: Path
    start_date: date
    end_date: date
    point_count: int
    min_effective_health: float
    max_effective_health: float
    mean_effective_health: float


def main(argv: Sequence[str] | None = None) -> None:
    _configure_text_output()
    args = parse_args(argv)
    summary = export_grid_effective_health_plot(
        config_path=args.config,
        output_path=args.output,
        start_date=args.start_date,
        end_date=args.end_date,
        days_ahead=args.days_ahead,
        now=args.now,
    )
    print_summary(summary)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a black-and-white matplotlib plot of simulated effective "
            "grid integrity over the configured history period."
        ),
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--start-date", type=_parse_date, default=None)
    parser.add_argument("--end-date", type=_parse_date, default=None)
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=0,
        help=(
            "Extend the default end date by this many days. Keep 0 for a "
            "history-only thesis figure."
        ),
    )
    parser.add_argument(
        "--now",
        type=_parse_datetime,
        default=None,
        help="Override current time for reproducible exports, ISO format.",
    )
    args = parser.parse_args(argv)

    if args.days_ahead < 0:
        parser.error("--days-ahead must be 0 or greater")
    if args.start_date is not None and args.end_date is not None:
        if args.end_date < args.start_date:
            parser.error("--end-date must be the same as or later than --start-date")
    return args


def export_grid_effective_health_plot(
    *,
    config_path: Path,
    output_path: Path,
    start_date: date | None = None,
    end_date: date | None = None,
    days_ahead: int = 0,
    now: datetime | None = None,
) -> PlotSummary:
    if days_ahead < 0:
        raise ValueError("days_ahead must be 0 or greater")

    config = load_config(config_path)
    settings = grid_settings_from_config(config)
    timezone_info = ZoneInfo(settings.local_timezone)
    resolved_start_date = start_date or _history_start_date(config)
    current_local_date = _resolve_now_utc(now).astimezone(timezone_info).date()
    resolved_end_date = end_date or current_local_date
    if days_ahead:
        resolved_end_date = resolved_end_date + timedelta(days=days_ahead)
    if resolved_end_date < resolved_start_date:
        raise ValueError("resolved end date must be the same as or later than start date")

    points, _events = generate_grid_availability_points(
        resolved_start_date,
        resolved_end_date,
        settings=settings,
    )
    if not points:
        raise RuntimeError("grid simulation produced no availability points")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _plot_points(points, output_path)
    values = [point.effective_health_percent for point in points]
    return PlotSummary(
        output_path=output_path.resolve(),
        start_date=points[0].timestamp_local.date(),
        end_date=points[-1].timestamp_local.date(),
        point_count=len(points),
        min_effective_health=min(values),
        max_effective_health=max(values),
        mean_effective_health=fmean(values),
    )


def _plot_points(points: list[GridAvailabilityPoint], output_path: Path) -> None:
    x_values = [point.timestamp_local.replace(tzinfo=None) for point in points]
    y_values = [point.effective_health_percent for point in points]

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.edgecolor": "black",
            "axes.labelcolor": "black",
            "xtick.color": "black",
            "ytick.color": "black",
        }
    )
    fig, ax = plt.subplots(figsize=(7.2, 4.2), facecolor="white")
    ax.set_facecolor("white")
    ax.plot(
        x_values,
        y_values,
        color="black",
        linewidth=1.1,
        label="Ефективна цілісність",
    )
    ax.axhline(
        100.0,
        color="0.35",
        linestyle="--",
        linewidth=0.9,
        label="Норма 100%",
    )
    ax.set_xlabel("Дата")
    ax.set_ylabel("Ефективна цілісність електромережі, %")
    ax.set_ylim(0, 150)
    ax.grid(True, color="0.82", linewidth=0.5)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%Y"))
    ax.legend(loc="lower left", frameon=False, fontsize=8)
    fig.autofmt_xdate(rotation=30, ha="right")
    fig.tight_layout()
    fig.savefig(output_path, dpi=300, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def print_summary(summary: PlotSummary) -> None:
    print(f"saved_path={summary.output_path}")
    print(f"start_date={summary.start_date.isoformat()}")
    print(f"end_date={summary.end_date.isoformat()}")
    print(f"points={summary.point_count}")
    print(f"min_effective_health={summary.min_effective_health:.2f}")
    print(f"max_effective_health={summary.max_effective_health:.2f}")
    print(f"mean_effective_health={summary.mean_effective_health:.2f}")


def _history_start_date(config: object) -> date:
    station = getattr(config, "station", None)
    raw_value = getattr(station, "installation_date", None)
    if isinstance(raw_value, date) and not isinstance(raw_value, datetime):
        return raw_value
    if isinstance(raw_value, str):
        return date.fromisoformat(raw_value)
    return DEFAULT_GRID_HISTORY_START


def _resolve_now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--now must be timezone-aware")
    return parsed


def _configure_text_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    main()
