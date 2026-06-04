from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Sequence


SYSTEM_TABLES = (
    "load_history_point",
    "load_cache_point",
    "battery_history_point",
    "battery_cache_point",
    "ems_history_point",
    "ems_cache_point",
)
HISTORY_TABLES = (
    "load_history_point",
    "battery_history_point",
    "ems_history_point",
)
CACHE_TABLES = (
    "load_cache_point",
    "battery_cache_point",
    "ems_cache_point",
)
STRESS_MODES = {
    "outage_mode",
    "force_charge",
    "backup_reserve",
    "inverter_protection_shutdown",
}
FLOW_FIELDS = (
    "grid_to_load_w",
    "grid_to_battery_w",
    "solar_to_load_w",
    "solar_to_battery_w",
    "battery_to_load_w",
    "applied_charge_power_w",
    "effective_load_power_w",
    "curtailed_or_cut_load_w",
)
BATTERY_FIELDS = (
    "soc_percent",
    "soh_percent",
    "voltage_v",
    "energy_wh",
    "net_battery_power_w",
)


@dataclass(frozen=True)
class ReviewRange:
    key: str
    title: str
    start: datetime
    end: datetime
    reason: str


def main() -> None:
    args = parse_args()
    db_path = args.db_path
    output_path = args.output
    config_path = args.config

    data = build_review_data(db_path, config_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(data), encoding="utf-8")

    print(f"wrote {output_path}")
    print(f"db_open_mode={data['meta']['db_open_mode']}")
    print(f"tariff_uah_per_kwh={data['meta']['tariff_uah_per_kwh']} source={data['meta']['tariff_source']}")
    for name, info in data["tables"].items():
        print(f"{name}: count={info['count']} min={info['min_timestamp_utc']} max={info['max_timestamp_utc']}")
    for item in data["ranges"]:
        print(f"range {item['key']}: {item['start']} to {item['end']} - {item['reason']}")
    soh = data["soh_summary"]
    print(
        "cache_soh_delta="
        f"{soh['cache_delta_percent']} percent over {soh['cache_days']} days; "
        f"loss_per_day={soh['cache_loss_percent_per_day']} suspicious={soh['cache_suspicious']}"
    )
    print(f"anomalies={len(data['anomalies'])}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a standalone visual review page for integrated system simulation data.",
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/system_simulation_visual_review.html"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("backend/config/station.default.yaml"),
    )
    return parser.parse_args()


def build_review_data(db_path: Path, config_path: Path) -> dict[str, Any]:
    rows = read_system_rows(db_path)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    tariff, tariff_source = read_grid_tariff(config_path)
    tables = summarize_tables(rows)
    ranges = choose_ranges(rows["ems_history_point"], tables)
    anomalies = compute_anomalies(rows, generated_at)
    soh_summary = compute_soh_summary(
        rows["battery_history_point"],
        rows["battery_cache_point"],
    )
    if soh_summary["cache_suspicious"]:
        anomalies.append(
            {
                "severity": "warning",
                "label": "Suspicious cache SoH degradation",
                "detail": (
                    f"Cache SoH changed by {soh_summary['cache_delta_percent']} percentage points "
                    f"({soh_summary['cache_loss_percent_per_day']} pp/day)."
                ),
            },
        )
    if not anomalies:
        anomalies.append({"severity": "ok", "label": "No blocking anomalies", "detail": "No configured anomaly checks failed."})

    history_start = tables["load_history_point"]["min_timestamp_utc"]
    history_end = tables["load_history_point"]["max_timestamp_utc"]

    return {
        "meta": {
            "db_path": str(db_path),
            "db_open_mode": "sqlite read-only URI mode=ro",
            "generated_at_utc": iso(generated_at),
            "history_note": "History tables contain 15-minute long-term points.",
            "cache_note": "Cache tables contain 1-minute current-day and +2 day forecast points.",
            "generation_data_range": f"{history_start} to {history_end}",
            "tariff_uah_per_kwh": tariff,
            "tariff_source": tariff_source,
        },
        "tables": tables,
        "ranges": [range_to_payload(item) for item in ranges],
        "soh_summary": soh_summary,
        "anomalies": anomalies,
        "stats": {
            "load": compute_load_stats(rows),
            "battery": compute_battery_stats(rows),
            "ems": compute_ems_stats(rows),
        },
        "charts": build_chart_payload(rows, ranges, tariff),
    }


def read_system_rows(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    db_uri = f"file:{db_path.as_posix()}?mode=ro"
    result: dict[str, list[dict[str, Any]]] = {}
    with sqlite3.connect(db_uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        for table in SYSTEM_TABLES:
            result[table] = [dict(row) for row in con.execute(f"select * from {table} order by timestamp_utc")]
    for table_rows in result.values():
        for row in table_rows:
            row["_dt"] = parse_ts(row["timestamp_utc"])
            row["_iso"] = iso(row["_dt"])
            row["_date"] = local_date_key(row)
            row["_month"] = row["_date"][:7]
    return result


def read_grid_tariff(config_path: Path) -> tuple[float, str]:
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("grid_tariff_uah_per_kwh:"):
                value = float(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
                return value, str(config_path)
    except Exception:
        pass
    return 0.0, "unavailable; stored money_saved_uah fields used where direct derivation needs tariff"


def summarize_tables(rows: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    summary = {}
    for table, table_rows in rows.items():
        summary[table] = {
            "count": len(table_rows),
            "min_timestamp_utc": table_rows[0]["_iso"] if table_rows else None,
            "max_timestamp_utc": table_rows[-1]["_iso"] if table_rows else None,
            "kind": "history" if table in HISTORY_TABLES else "cache",
        }
    return summary


def choose_ranges(ems_history: list[dict[str, Any]], tables: dict[str, dict[str, Any]]) -> list[ReviewRange]:
    if not ems_history:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return [
            ReviewRange("whole", "Whole Station Existence", now, now, "No generated history rows were found."),
        ]

    min_dt = ems_history[0]["_dt"]
    max_dt = ems_history[-1]["_dt"]
    daily_scores = compute_daily_stress_scores(ems_history)

    first_stress = next((row for row in ems_history if is_stress_row(row)), ems_history[0])
    beginning_start = clamp_window_start(start_of_day(first_stress["_dt"]) - timedelta(days=1), min_dt, max_dt)
    beginning_start = ensure_window_fits(beginning_start, min_dt, max_dt, 14)

    strongest_start = choose_strongest_window(daily_scores, min_dt, max_dt)
    spring_start, spring_reason = choose_spring_window(daily_scores, min_dt, max_dt)

    return [
        ReviewRange(
            "whole",
            "Whole Station Existence",
            min_dt,
            max_dt,
            "Earliest generated history timestamp through the latest generated history timestamp.",
        ),
        ReviewRange(
            "beginning",
            "Beginning Of Outage / Stress Behavior",
            beginning_start,
            min(beginning_start + timedelta(days=14), max_dt),
            (
                "Starts near the first row with outage/stress indicators "
                "(stress mode, battery discharge, risk, protection, or curtailed load)."
            ),
        ),
        ReviewRange(
            "strongest",
            "Strongest Outage / Stress Period",
            strongest_start,
            min(strongest_start + timedelta(days=14), max_dt),
            "Highest-scoring rolling 14-day window by risk, protection, stress modes, battery discharge, and curtailed load.",
        ),
        ReviewRange(
            "spring",
            "Spring Transition Period",
            spring_start,
            min(spring_start + timedelta(days=14), max_dt),
            spring_reason,
        ),
    ]


def compute_daily_stress_scores(ems_history: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for row in ems_history:
        score = float(row["auto_risk_score"]) * 0.25
        if row["selected_mode"] in STRESS_MODES:
            score += 8.0
        if int(row["protection_active"]):
            score += 120.0
        score += max(0.0, float(row["battery_to_load_w"])) * 0.25 / 100.0
        score += max(0.0, float(row["curtailed_or_cut_load_w"])) * 0.25 / 10.0
        scores[row["_date"]] += score
    return dict(scores)


def choose_strongest_window(daily_scores: dict[str, float], min_dt: datetime, max_dt: datetime) -> datetime:
    dates = date_range(start_of_day(min_dt), start_of_day(max_dt))
    best_start = start_of_day(min_dt)
    best_score = float("-inf")
    for current in dates:
        if current + timedelta(days=14) > max_dt + timedelta(days=1):
            break
        score = sum(daily_scores.get(date_key(current + timedelta(days=offset)), 0.0) for offset in range(14))
        if score > best_score:
            best_score = score
            best_start = current
    return ensure_window_fits(best_start, min_dt, max_dt, 14)


def choose_spring_window(
    daily_scores: dict[str, float],
    min_dt: datetime,
    max_dt: datetime,
) -> tuple[datetime, str]:
    best_start: datetime | None = None
    best_drop = float("-inf")
    spring_start = datetime(max_dt.year, 3, 1, tzinfo=timezone.utc)
    spring_end = datetime(max_dt.year, 5, 31, tzinfo=timezone.utc)
    start = max(start_of_day(min_dt), spring_start)
    end = min(start_of_day(max_dt), spring_end)
    current = start
    while current + timedelta(days=14) <= end + timedelta(days=1):
        first_half = sum(daily_scores.get(date_key(current + timedelta(days=offset)), 0.0) for offset in range(7))
        second_half = sum(daily_scores.get(date_key(current + timedelta(days=offset)), 0.0) for offset in range(7, 14))
        drop = first_half - second_half
        if first_half > 0.0 and drop > best_drop:
            best_drop = drop
            best_start = current
        current += timedelta(days=1)

    if best_start is not None and best_drop > 0.0:
        return ensure_window_fits(best_start, min_dt, max_dt, 14), (
            "Selected in March-May because the first week has more stress than the second week, "
            "indicating a transition toward stable behavior."
        )

    nontrivial = [
        parse_date(key)
        for key, score in daily_scores.items()
        if score > 0.0 and parse_date(key) <= start_of_day(max_dt)
    ]
    if nontrivial:
        fallback_start = max(nontrivial) - timedelta(days=13)
        return ensure_window_fits(fallback_start, min_dt, max_dt, 14), (
            "No clear March-May stress-to-stable transition was detected; selected the last "
            "14-day window with nontrivial stress before the stable period."
        )
    return ensure_window_fits(start_of_day(max_dt) - timedelta(days=13), min_dt, max_dt, 14), (
        "No nontrivial stress was detected; selected the latest available 14-day window as fallback."
    )


def build_chart_payload(
    rows: dict[str, list[dict[str, Any]]],
    ranges: Sequence[ReviewRange],
    tariff: float,
) -> dict[str, Any]:
    selected_ranges = [item for item in ranges if item.key != "whole"]
    payload: dict[str, Any] = {
        "battery": {
            "whole": {
                "state": line_rows(rows["battery_history_point"], BATTERY_FIELDS, max_points=1400),
                "netPower": line_rows(rows["battery_history_point"], ("net_battery_power_w",), max_points=1400),
            },
            "cache": {
                "state": line_rows(rows["battery_cache_point"], BATTERY_FIELDS, max_points=1600),
                "netPower": line_rows(rows["battery_cache_point"], ("net_battery_power_w",), max_points=1600),
            },
            "windows": {},
        },
        "load": {
            "dailyEnergy": aggregate_load_daily(rows["load_history_point"]),
            "monthlyEnergy": aggregate_load_monthly(rows["load_history_point"]),
            "currentMonth": aggregate_current_month(rows["load_history_point"]),
            "cachePower": line_rows(rows["load_cache_point"], ("total_load_power_w", "effective_served_load_w"), max_points=1600),
            "windows": {},
        },
        "solarEconomics": {
            "monthly": aggregate_solar_monthly(rows["load_history_point"], rows["ems_history_point"], tariff),
            "windows": {},
        },
        "ems": {
            "riskWhole": line_rows(rows["ems_history_point"], ("auto_risk_score",), max_points=1400),
            "modeCounts": mode_counts(rows["ems_history_point"]),
            "dailyModeCounts": daily_mode_counts(rows["ems_history_point"]),
            "protectionDaily": aggregate_ems_daily(rows["ems_history_point"], "protection_active"),
            "curtailedDaily": aggregate_ems_curtailed_daily(rows["ems_history_point"]),
            "flowWhole": line_rows(rows["ems_history_point"], FLOW_FIELDS, max_points=1400),
            "windows": {},
        },
    }

    for review_range in selected_ranges:
        key = review_range.key
        battery_window = filter_range(rows["battery_history_point"], review_range.start, review_range.end)
        load_window = filter_range(rows["load_history_point"], review_range.start, review_range.end)
        ems_window = filter_range(rows["ems_history_point"], review_range.start, review_range.end)
        payload["battery"]["windows"][key] = {
            "state": line_rows(battery_window, BATTERY_FIELDS, max_points=1600),
            "netPower": line_rows(battery_window, ("net_battery_power_w",), max_points=1600),
        }
        payload["load"]["windows"][key] = {
            "power": line_rows(load_window, ("total_load_power_w", "effective_served_load_w", "load_cut_by_ems_w"), max_points=1600),
            "dailyPattern": line_rows(load_window, ("daily_energy_wh_so_far",), max_points=1600),
        }
        payload["solarEconomics"]["windows"][key] = aggregate_solar_daily(load_window, ems_window, tariff)
        payload["ems"]["windows"][key] = {
            "risk": line_rows(ems_window, ("auto_risk_score",), max_points=1600),
            "flows": line_rows(ems_window, FLOW_FIELDS, max_points=1600),
            "modes": daily_mode_counts(ems_window),
        }

    return payload


def compute_anomalies(rows: dict[str, list[dict[str, Any]]], now_utc: datetime) -> list[dict[str, str]]:
    anomalies: list[dict[str, str]] = []

    soc_bad = count_where(
        rows["battery_history_point"] + rows["battery_cache_point"],
        lambda row: float(row["soc_percent"]) < 0.0 or float(row["soc_percent"]) > 100.0,
    )
    append_count_anomaly(anomalies, "SoC outside 0..100", soc_bad)

    negative_flow_count = count_negative_ems_flows(rows["ems_history_point"] + rows["ems_cache_point"])
    append_count_anomaly(anomalies, "Unexpected negative EMS flow rows", negative_flow_count)

    for table in HISTORY_TABLES:
        violations = count_where(
            rows[table],
            lambda row: row["_dt"].minute not in (0, 15, 30, 45) or row["_dt"].second != 0 or row["_dt"].microsecond != 0,
        )
        append_count_anomaly(anomalies, f"{table} history cadence violations", violations)
        future = count_where(rows[table], lambda row: row["_dt"] > now_utc)
        append_count_anomaly(anomalies, f"{table} future history rows", future)

    for table in CACHE_TABLES:
        violations = count_where(
            rows[table],
            lambda row: row["_dt"].second != 0 or row["_dt"].microsecond != 0,
        )
        append_count_anomaly(anomalies, f"{table} whole-minute violations", violations)

    load_by_ts = {row["_iso"]: row for row in rows["load_history_point"]}
    inconsistent = 0
    for ems in rows["ems_history_point"]:
        load = load_by_ts.get(ems["_iso"])
        if load is not None and abs(float(ems["effective_load_power_w"]) - float(load["effective_served_load_w"])) > 1.0:
            inconsistent += 1
    append_count_anomaly(anomalies, "History EMS effective load vs Load served mismatch rows", inconsistent)

    voltage_extreme = count_where(
        rows["battery_history_point"] + rows["battery_cache_point"],
        lambda row: float(row["voltage_v"]) < 10.5 or float(row["voltage_v"]) > 14.8,
    )
    append_count_anomaly(anomalies, "Extreme 12V battery voltage rows (<10.5 or >14.8 V)", voltage_extreme)

    net_power_extreme = count_where(
        rows["battery_history_point"] + rows["battery_cache_point"],
        lambda row: abs(float(row["net_battery_power_w"])) > 2500.0,
    )
    append_count_anomaly(anomalies, "Extreme battery net power rows (>2500 W absolute)", net_power_extreme)

    return anomalies


def compute_soh_summary(
    history: list[dict[str, Any]],
    cache: list[dict[str, Any]],
) -> dict[str, Any]:
    history_start = float(history[0]["soh_percent"]) if history else None
    history_end = float(history[-1]["soh_percent"]) if history else None
    cache_start = float(cache[0]["soh_percent"]) if cache else None
    cache_end = float(cache[-1]["soh_percent"]) if cache else None
    cache_days = None
    cache_delta = None
    loss_per_day = None
    suspicious = False
    if cache and cache_start is not None and cache_end is not None:
        cache_days = max((cache[-1]["_dt"] - cache[0]["_dt"]).total_seconds() / 86400.0, 0.0)
        cache_delta = cache_end - cache_start
        if cache_days > 0:
            loss_per_day = -cache_delta / cache_days
            suspicious = loss_per_day > 0.05 or abs(cache_delta) > 0.10
    return {
        "history_start_percent": round_optional(history_start, 6),
        "history_end_percent": round_optional(history_end, 6),
        "history_delta_percent": round_optional((history_end - history_start) if history_start is not None and history_end is not None else None, 6),
        "cache_start_percent": round_optional(cache_start, 6),
        "cache_end_percent": round_optional(cache_end, 6),
        "cache_delta_percent": round_optional(cache_delta, 6),
        "cache_days": round_optional(cache_days, 3),
        "cache_loss_percent_per_day": round_optional(loss_per_day, 6),
        "cache_suspicious": suspicious,
        "suspicious_threshold": "flagged if cache loss >0.05 percentage points/day or total cache SoH change >0.10 percentage points",
    }


def compute_load_stats(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "history": load_stats(rows["load_history_point"]),
        "cache": load_stats(rows["load_cache_point"]),
        "spikes": top_rows(rows["load_history_point"], "total_load_power_w", 10),
    }


def compute_battery_stats(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "history": battery_stats(rows["battery_history_point"]),
        "cache": battery_stats(rows["battery_cache_point"]),
    }


def compute_ems_stats(rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "history": ems_stats(rows["ems_history_point"]),
        "cache": ems_stats(rows["ems_cache_point"]),
    }


def load_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    powers = [float(row["total_load_power_w"]) for row in rows]
    daily = [float(row["daily_energy_wh_so_far"]) for row in rows]
    return {
        "total_load_power_w": min_max_avg(powers),
        "daily_energy_wh_so_far": min_max(daily),
        "solar_covered_percent": min_max([float(row["solar_covered_percent"]) for row in rows]),
        "money_saved_uah": min_max([float(row["money_saved_uah"]) for row in rows]),
    }


def battery_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "soc_percent": min_max([float(row["soc_percent"]) for row in rows]),
        "soh_percent": min_max([float(row["soh_percent"]) for row in rows]),
        "voltage_v": min_max([float(row["voltage_v"]) for row in rows]),
        "energy_wh": min_max([float(row["energy_wh"]) for row in rows]),
        "net_battery_power_w": min_max([float(row["net_battery_power_w"]) for row in rows]),
    }


def ems_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "risk_score": min_max([float(row["auto_risk_score"]) for row in rows]),
        "protection_active_rows": count_where(rows, lambda row: bool(int(row["protection_active"]))),
        "negative_flow_rows": count_negative_ems_flows(rows),
        "flow_ranges": {field: min_max([float(row[field]) for row in rows]) for field in FLOW_FIELDS},
    }


def line_rows(
    rows: list[dict[str, Any]],
    fields: Sequence[str],
    *,
    max_points: int,
) -> list[dict[str, Any]]:
    selected = downsample(rows, max_points)
    return [
        {"t": row["_iso"], **{field: round_float(row[field], 6) for field in fields}}
        for row in selected
    ]


def aggregate_load_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, float] = defaultdict(float)
    for row in rows:
        by_day[row["_date"]] = max(by_day[row["_date"]], float(row["daily_energy_wh_so_far"]))
    return [{"label": day, "kwh": round(value / 1000.0, 4)} for day, value in sorted(by_day.items())]


def aggregate_load_monthly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily = aggregate_load_daily(rows)
    by_month: dict[str, float] = defaultdict(float)
    for item in daily:
        by_month[item["label"][:7]] += float(item["kwh"])
    return [{"label": month, "kwh": round(value, 4)} for month, value in sorted(by_month.items())]


def aggregate_current_month(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    current_month = rows[-1]["_date"][:7]
    return [item for item in aggregate_load_daily(rows) if item["label"].startswith(current_month)]


def aggregate_solar_monthly(
    load_rows: list[dict[str, Any]],
    ems_rows: list[dict[str, Any]],
    tariff: float,
) -> list[dict[str, Any]]:
    return aggregate_solar_by_key(load_rows, ems_rows, tariff, key_func=lambda row: row["_month"], step_hours=0.25)


def aggregate_solar_daily(
    load_rows: list[dict[str, Any]],
    ems_rows: list[dict[str, Any]],
    tariff: float,
) -> list[dict[str, Any]]:
    return aggregate_solar_by_key(load_rows, ems_rows, tariff, key_func=lambda row: row["_date"], step_hours=0.25)


def aggregate_solar_by_key(
    load_rows: list[dict[str, Any]],
    ems_rows: list[dict[str, Any]],
    tariff: float,
    *,
    key_func,
    step_hours: float,
) -> list[dict[str, Any]]:
    ems_by_ts = {row["_iso"]: row for row in ems_rows}
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"load_wh": 0.0, "solar_wh": 0.0})
    for load in load_rows:
        ems = ems_by_ts.get(load["_iso"])
        if ems is None:
            continue
        key = key_func(load)
        load_wh = max(0.0, float(load["total_load_power_w"])) * step_hours
        solar_wh = (
            max(0.0, float(ems["solar_to_load_w"]))
            + max(0.0, float(ems["solar_to_battery_w"]))
        ) * step_hours
        totals[key]["load_wh"] += load_wh
        totals[key]["solar_wh"] += solar_wh
    result = []
    for key, values in sorted(totals.items()):
        load_wh = values["load_wh"]
        solar_wh = values["solar_wh"]
        percent = clamp((solar_wh / load_wh * 100.0) if load_wh > 0 else 0.0, 0.0, 100.0)
        result.append(
            {
                "label": key,
                "solarCoveredPercent": round(percent, 4),
                "moneySavedUah": round((solar_wh / 1000.0) * tariff, 4),
                "solarCoveredKwh": round(solar_wh / 1000.0, 4),
                "loadKwh": round(load_wh / 1000.0, 4),
            },
        )
    return result


def mode_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(row["selected_mode"] for row in rows)
    return [{"label": key, "count": value} for key, value in counts.most_common()]


def daily_mode_counts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    modes = sorted({row["selected_mode"] for row in rows})
    by_day: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_day[row["_date"]][row["selected_mode"]] += 1
    result = []
    for day in sorted(by_day):
        item: dict[str, Any] = {"label": day}
        for mode in modes:
            item[mode] = by_day[day].get(mode, 0)
        result.append(item)
    return result


def aggregate_ems_daily(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    by_day: dict[str, int] = defaultdict(int)
    for row in rows:
        by_day[row["_date"]] += int(bool(int(row[field])))
    return [{"label": day, "count": value} for day, value in sorted(by_day.items())]


def aggregate_ems_curtailed_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, float] = defaultdict(float)
    for row in rows:
        by_day[row["_date"]] += max(0.0, float(row["curtailed_or_cut_load_w"])) * 0.25
    return [{"label": day, "wh": round(value, 4)} for day, value in sorted(by_day.items())]


def filter_range(rows: list[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if start <= row["_dt"] < end]


def downsample(rows: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    if len(rows) <= max_points:
        return rows
    if max_points < 2:
        return rows[:max_points]
    step = (len(rows) - 1) / (max_points - 1)
    return [rows[round(index * step)] for index in range(max_points)]


def top_rows(rows: list[dict[str, Any]], field: str, limit: int) -> list[dict[str, Any]]:
    selected = sorted(rows, key=lambda row: float(row[field]), reverse=True)[:limit]
    return [{"timestamp_utc": row["_iso"], field: round_float(row[field], 6)} for row in selected]


def is_stress_row(row: dict[str, Any]) -> bool:
    return (
        row["selected_mode"] in STRESS_MODES
        or int(row["protection_active"]) != 0
        or float(row["auto_risk_score"]) > 0.0
        or float(row["battery_to_load_w"]) > 1.0
        or float(row["curtailed_or_cut_load_w"]) > 0.0
    )


def count_negative_ems_flows(rows: list[dict[str, Any]]) -> int:
    return count_where(
        rows,
        lambda row: any(float(row[field]) < -1e-9 for field in FLOW_FIELDS),
    )


def append_count_anomaly(anomalies: list[dict[str, str]], label: str, count: int) -> None:
    if count:
        anomalies.append({"severity": "warning", "label": label, "detail": f"{count} rows"})


def count_where(rows: Iterable[dict[str, Any]], predicate) -> int:
    return sum(1 for row in rows if predicate(row))


def min_max(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None}
    return {"min": round(min(values), 6), "max": round(max(values), 6)}


def min_max_avg(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "avg": None}
    return {"min": round(min(values), 6), "max": round(max(values), 6), "avg": round(mean(values), 6)}


def round_optional(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def round_float(value: Any, digits: int) -> float:
    return round(float(value), digits)


def clamp(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def parse_ts(value: Any) -> datetime:
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_date_key(row: dict[str, Any]) -> str:
    local_value = row.get("timestamp_local")
    if local_value:
        return str(local_value)[:10]
    return date_key(row["_dt"])


def date_key(value: datetime) -> str:
    return value.date().isoformat()


def parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def date_range(start: datetime, end: datetime) -> Iterable[datetime]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def start_of_day(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def clamp_window_start(start: datetime, min_dt: datetime, max_dt: datetime) -> datetime:
    return min(max(start, start_of_day(min_dt)), start_of_day(max_dt))


def ensure_window_fits(start: datetime, min_dt: datetime, max_dt: datetime, days: int) -> datetime:
    min_start = start_of_day(min_dt)
    latest_start = max(start_of_day(max_dt) - timedelta(days=days - 1), min_start)
    return min(max(start, min_start), latest_start)


def range_to_payload(item: ReviewRange) -> dict[str, str]:
    return {
        "key": item.key,
        "title": item.title,
        "start": iso(item.start),
        "end": iso(item.end),
        "reason": item.reason,
    }


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def render_html(data: dict[str, Any]) -> str:
    json_payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SmartEnergy System Simulation Visual Review</title>
  <style>
    :root {{
      --bg: #f3f0e8;
      --paper: #fffdf7;
      --ink: #202124;
      --muted: #687076;
      --line: #d8d0c1;
      --battery: #148f5d;
      --battery2: #1f77b4;
      --load: #c84e2f;
      --load2: #f08a24;
      --solar: #d6a100;
      --grid: #2f63c7;
      --risk: #d33f49;
      --protection: #ff7a00;
      --ok: #16884a;
      --warn: #ad5f00;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      background: radial-gradient(circle at top left, #fff8db, transparent 30rem), var(--bg);
      color: var(--ink);
    }}
    header {{
      padding: 28px clamp(18px, 4vw, 54px);
      background: linear-gradient(135deg, #24312c, #47513a);
      color: #fffdf4;
      border-bottom: 6px solid #d7a928;
    }}
    header h1 {{ margin: 0 0 8px; font-size: clamp(28px, 4vw, 46px); letter-spacing: -0.03em; }}
    header p {{ margin: 4px 0; color: #e8e0ca; }}
    main {{ padding: 24px clamp(14px, 3vw, 42px) 48px; }}
    section {{ margin: 24px 0 36px; }}
    h2 {{ font-size: 26px; margin: 0 0 14px; }}
    h3 {{ font-size: 19px; margin: 0 0 10px; }}
    .grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .card {{
      background: rgba(255, 253, 247, 0.94);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 12px 24px rgba(57, 49, 32, 0.08);
      padding: 16px;
    }}
    .chart-card {{ overflow: hidden; }}
    .chart-wrap {{ overflow-x: auto; padding-bottom: 6px; }}
    .metric {{
      display: grid;
      gap: 5px;
      padding: 10px 0;
      border-bottom: 1px solid #ede5d8;
    }}
    .metric:last-child {{ border-bottom: 0; }}
    .metric span:first-child {{ color: var(--muted); font-size: 13px; }}
    .metric strong {{ font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: left; padding: 8px 9px; border-bottom: 1px solid #e8dfcf; vertical-align: top; }}
    th {{ color: #4f564f; background: #f7f1e5; position: sticky; top: 0; }}
    .table-scroll {{ overflow: auto; max-height: 420px; border: 1px solid var(--line); border-radius: 12px; }}
    .pill {{ display: inline-flex; padding: 4px 8px; border-radius: 999px; background: #efe7d6; color: #30362f; font-size: 12px; margin: 2px; }}
    .warn {{ color: var(--warn); font-weight: 700; }}
    .ok {{ color: var(--ok); font-weight: 700; }}
    svg {{ display: block; background: #fffaf0; border-radius: 12px; }}
    .axis text {{ fill: #666; font-size: 11px; }}
    .axis line, .axis path, .gridline {{ stroke: #d8d0c1; stroke-width: 1; }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 0; font-size: 12px; color: #4d5350; }}
    .legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
    .swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}
    .tooltip {{
      position: fixed;
      pointer-events: none;
      background: #1f241f;
      color: white;
      padding: 8px 10px;
      border-radius: 8px;
      font-size: 12px;
      max-width: 340px;
      z-index: 10;
      box-shadow: 0 12px 28px rgba(0,0,0,.24);
      display: none;
    }}
    .small {{ color: var(--muted); font-size: 13px; }}
  </style>
</head>
<body>
  <header>
    <h1>SmartEnergy System Simulation Visual Review</h1>
    <p id="subtitle"></p>
    <p class="small">Standalone diagnostics page. Data is embedded in this HTML from the swap DB.</p>
  </header>
  <main id="app"></main>
  <div id="tooltip" class="tooltip"></div>
  <script id="review-data" type="application/json">{json_payload}</script>
  <script>
    const DATA = JSON.parse(document.getElementById('review-data').textContent);
    const COLORS = {{
      soc_percent: '#148f5d',
      soh_percent: '#1f77b4',
      voltage_v: '#345995',
      energy_wh: '#11998e',
      net_battery_power_w: '#1a936f',
      total_load_power_w: '#c84e2f',
      effective_served_load_w: '#f08a24',
      load_cut_by_ems_w: '#a41623',
      daily_energy_wh_so_far: '#c84e2f',
      auto_risk_score: '#d33f49',
      grid_to_load_w: '#2f63c7',
      grid_to_battery_w: '#71a5ff',
      solar_to_load_w: '#d6a100',
      solar_to_battery_w: '#ffd166',
      battery_to_load_w: '#148f5d',
      applied_charge_power_w: '#38a169',
      effective_load_power_w: '#c84e2f',
      curtailed_or_cut_load_w: '#ff7a00',
      kwh: '#c84e2f',
      solarCoveredPercent: '#d6a100',
      moneySavedUah: '#16884a',
      count: '#d33f49',
      wh: '#ff7a00'
    }};
    const MODE_COLORS = ['#2f63c7','#148f5d','#d6a100','#d33f49','#ff7a00','#6f4e7c','#455a64','#8d6e63'];
    const app = document.getElementById('app');
    const tooltip = document.getElementById('tooltip');

    function el(tag, attrs = {{}}, children = []) {{
      const node = document.createElement(tag);
      for (const [key, value] of Object.entries(attrs)) {{
        if (key === 'class') node.className = value;
        else if (key === 'html') node.innerHTML = value;
        else node.setAttribute(key, value);
      }}
      for (const child of children) node.append(child);
      return node;
    }}
    function fmt(value, digits = 3) {{
      if (value === null || value === undefined || Number.isNaN(Number(value))) return 'n/a';
      return Number(value).toLocaleString(undefined, {{ maximumFractionDigits: digits }});
    }}
    function titleCase(value) {{
      return String(value).replaceAll('_', ' ');
    }}
    function section(title, children = []) {{
      const node = el('section', {{}}, [el('h2', {{ html: title }})]);
      for (const child of children) node.append(child);
      app.append(node);
      return node;
    }}
    function card(title, content, cls = '') {{
      const node = el('div', {{ class: `card ${{cls}}` }}, [el('h3', {{ html: title }})]);
      if (Array.isArray(content)) content.forEach(item => node.append(item));
      else node.append(content);
      return node;
    }}
    function table(headers, rows) {{
      const thead = el('thead', {{}}, [el('tr', {{}}, headers.map(h => el('th', {{ html: h }})))]);
      const tbody = el('tbody', {{}}, rows.map(row => el('tr', {{}}, row.map(cell => el('td', {{ html: String(cell) }})))));
      return el('div', {{ class: 'table-scroll' }}, [el('table', {{}}, [thead, tbody])]);
    }}
    function metric(label, value, cls = '') {{
      return el('div', {{ class: 'metric' }}, [el('span', {{ html: label }}), el('strong', {{ class: cls, html: value }})]);
    }}
    function chartCard(title, render) {{
      const wrap = el('div', {{ class: 'chart-wrap' }});
      render(wrap);
      return card(title, wrap, 'chart-card');
    }}

    function lineChart(container, points, fields, options = {{}}) {{
      const width = options.width || Math.max(980, Math.min(2200, points.length * 2.4));
      const height = options.height || 300;
      const margin = {{ top: 22, right: 28, bottom: 42, left: 62 }};
      const innerW = width - margin.left - margin.right;
      const innerH = height - margin.top - margin.bottom;
      const svg = svgEl('svg', {{ width, height, viewBox: `0 0 ${{width}} ${{height}}` }});
      container.append(svg);
      if (!points || !points.length) {{
        svg.append(svgEl('text', {{ x: 24, y: 45, fill: '#777' }}, 'No data'));
        return;
      }}
      const xs = points.map(p => new Date(p.t).getTime());
      const values = [];
      for (const p of points) for (const f of fields) if (Number.isFinite(Number(p[f]))) values.push(Number(p[f]));
      let yMin = options.yMin ?? Math.min(...values);
      let yMax = options.yMax ?? Math.max(...values);
      if (yMin === yMax) {{ yMin -= 1; yMax += 1; }}
      if (options.includeZero) {{ yMin = Math.min(0, yMin); yMax = Math.max(0, yMax); }}
      const xMin = Math.min(...xs), xMax = Math.max(...xs);
      const x = v => margin.left + ((v - xMin) / Math.max(1, xMax - xMin)) * innerW;
      const y = v => margin.top + (1 - ((v - yMin) / Math.max(1e-9, yMax - yMin))) * innerH;
      drawAxes(svg, width, height, margin, xMin, xMax, yMin, yMax, options.yUnit || '');
      if (yMin < 0 && yMax > 0) svg.append(svgEl('line', {{ x1: margin.left, x2: width - margin.right, y1: y(0), y2: y(0), stroke: '#948b7b', 'stroke-dasharray': '4 4' }}));
      for (const f of fields) {{
        const d = points
          .filter(p => Number.isFinite(Number(p[f])))
          .map((p, i) => `${{i === 0 ? 'M' : 'L'}}${{x(new Date(p.t).getTime()).toFixed(2)}},${{y(Number(p[f])).toFixed(2)}}`)
          .join(' ');
        svg.append(svgEl('path', {{ d, fill: 'none', stroke: COLORS[f] || '#333', 'stroke-width': options.strokeWidth || 2 }}));
      }}
      addLegend(container, fields.map(f => [titleCase(f), COLORS[f] || '#333']));
      addLineTooltip(svg, points, fields, xMin, xMax, yMin, yMax, margin, width, height);
    }}

    function barChart(container, points, field, options = {{}}) {{
      const width = options.width || Math.max(780, points.length * 22);
      const height = options.height || 300;
      const margin = {{ top: 20, right: 24, bottom: 72, left: 62 }};
      const innerW = width - margin.left - margin.right;
      const innerH = height - margin.top - margin.bottom;
      const svg = svgEl('svg', {{ width, height, viewBox: `0 0 ${{width}} ${{height}}` }});
      container.append(svg);
      if (!points || !points.length) {{
        svg.append(svgEl('text', {{ x: 24, y: 45, fill: '#777' }}, 'No data'));
        return;
      }}
      const maxV = Math.max(...points.map(p => Number(p[field]) || 0), 1);
      drawYAxis(svg, margin, height, 0, maxV, options.yUnit || '');
      const gap = 4;
      const bw = Math.max(4, innerW / points.length - gap);
      points.forEach((p, i) => {{
        const value = Number(p[field]) || 0;
        const h = (value / maxV) * innerH;
        const x = margin.left + i * (innerW / points.length) + gap / 2;
        const y = margin.top + innerH - h;
        const rect = svgEl('rect', {{ x, y, width: bw, height: h, fill: options.color || COLORS[field] || '#777', rx: 2 }});
        rect.addEventListener('mousemove', evt => showTip(evt, `${{p.label}}<br>${{titleCase(field)}}: ${{fmt(value)}}`));
        rect.addEventListener('mouseleave', hideTip);
        svg.append(rect);
      }});
      drawCategoryTicks(svg, points, margin, width, height, 10);
    }}

    function stackedBarChart(container, points, fields, options = {{}}) {{
      const width = options.width || Math.max(980, points.length * 14);
      const height = options.height || 320;
      const margin = {{ top: 22, right: 24, bottom: 70, left: 62 }};
      const innerW = width - margin.left - margin.right;
      const innerH = height - margin.top - margin.bottom;
      const svg = svgEl('svg', {{ width, height, viewBox: `0 0 ${{width}} ${{height}}` }});
      container.append(svg);
      if (!points || !points.length) return;
      const maxTotal = Math.max(...points.map(p => fields.reduce((sum, f) => sum + (Number(p[f]) || 0), 0)), 1);
      drawYAxis(svg, margin, height, 0, maxTotal, 'rows');
      const bw = Math.max(3, innerW / points.length - 2);
      points.forEach((p, i) => {{
        let yCursor = margin.top + innerH;
        const x = margin.left + i * (innerW / points.length);
        fields.forEach((f, idx) => {{
          const value = Number(p[f]) || 0;
          const h = (value / maxTotal) * innerH;
          yCursor -= h;
          const rect = svgEl('rect', {{ x, y: yCursor, width: bw, height: h, fill: MODE_COLORS[idx % MODE_COLORS.length] }});
          rect.addEventListener('mousemove', evt => showTip(evt, `${{p.label}}<br>${{f}}: ${{value}} rows`));
          rect.addEventListener('mouseleave', hideTip);
          svg.append(rect);
        }});
      }});
      drawCategoryTicks(svg, points, margin, width, height, 10);
      addLegend(container, fields.map((f, idx) => [f, MODE_COLORS[idx % MODE_COLORS.length]]));
    }}

    function svgEl(tag, attrs = {{}}, text = null) {{
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      if (text !== null) node.textContent = text;
      return node;
    }}
    function drawAxes(svg, width, height, margin, xMin, xMax, yMin, yMax, yUnit) {{
      drawYAxis(svg, margin, height, yMin, yMax, yUnit);
      const ticks = 6;
      for (let i = 0; i <= ticks; i++) {{
        const t = xMin + (i / ticks) * (xMax - xMin);
        const x = margin.left + (i / ticks) * (width - margin.left - margin.right);
        svg.append(svgEl('line', {{ x1: x, x2: x, y1: height - margin.bottom, y2: height - margin.bottom + 5, stroke: '#948b7b' }}));
        svg.append(svgEl('text', {{ x, y: height - 18, 'text-anchor': 'middle', fill: '#666', 'font-size': 11 }}, formatDate(t)));
      }}
    }}
    function drawYAxis(svg, margin, height, yMin, yMax, yUnit) {{
      const innerH = height - margin.top - margin.bottom;
      const ticks = 5;
      for (let i = 0; i <= ticks; i++) {{
        const value = yMin + (i / ticks) * (yMax - yMin);
        const y = margin.top + (1 - i / ticks) * innerH;
        svg.append(svgEl('line', {{ x1: margin.left, x2: '100%', y1: y, y2: y, stroke: '#e5dccd' }}));
        svg.append(svgEl('text', {{ x: margin.left - 8, y: y + 4, 'text-anchor': 'end', fill: '#666', 'font-size': 11 }}, `${{fmt(value, 2)}} ${{yUnit}}`));
      }}
    }}
    function drawCategoryTicks(svg, points, margin, width, height, maxLabels) {{
      const step = Math.max(1, Math.ceil(points.length / maxLabels));
      const innerW = width - margin.left - margin.right;
      points.forEach((p, i) => {{
        if (i % step !== 0 && i !== points.length - 1) return;
        const x = margin.left + i * (innerW / points.length) + innerW / points.length / 2;
        const text = svgEl('text', {{ x, y: height - 18, 'text-anchor': 'end', fill: '#666', 'font-size': 11, transform: `rotate(-35 ${{x}} ${{height - 18}})` }}, p.label);
        svg.append(text);
      }});
    }}
    function addLegend(container, items) {{
      const legend = el('div', {{ class: 'legend' }});
      items.forEach(([label, color]) => legend.append(el('span', {{}}, [el('i', {{ class: 'swatch', style: `background:${{color}}` }}), document.createTextNode(label)])));
      container.append(legend);
    }}
    function addLineTooltip(svg, points, fields) {{
      svg.addEventListener('mousemove', evt => {{
        const box = svg.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (evt.clientX - box.left - 62) / Math.max(1, box.width - 90)));
        const idx = Math.max(0, Math.min(points.length - 1, Math.round(ratio * (points.length - 1))));
        const p = points[idx];
        const lines = [`<strong>${{p.t}}</strong>`].concat(fields.map(f => `${{titleCase(f)}}: ${{fmt(p[f])}}`));
        showTip(evt, lines.join('<br>'));
      }});
      svg.addEventListener('mouseleave', hideTip);
    }}
    function showTip(evt, html) {{
      tooltip.innerHTML = html;
      tooltip.style.display = 'block';
      tooltip.style.left = `${{evt.clientX + 14}}px`;
      tooltip.style.top = `${{evt.clientY + 14}}px`;
    }}
    function hideTip() {{ tooltip.style.display = 'none'; }}
    function formatDate(ms) {{
      const d = new Date(ms);
      return d.toISOString().slice(0, 10);
    }}

    function render() {{
      document.getElementById('subtitle').textContent = `${{DATA.meta.db_path}} | ${{DATA.meta.generation_data_range}}`;
      renderSummary();
      renderRanges();
      renderBattery();
      renderLoad();
      renderSolarEconomics();
      renderEms();
      renderConsistency();
    }}

    function renderSummary() {{
      const tableRows = Object.entries(DATA.tables).map(([name, info]) => [
        name, info.kind, info.count, info.min_timestamp_utc, info.max_timestamp_utc
      ]);
      const anomalyRows = DATA.anomalies.map(a => [a.severity, a.label, a.detail]);
      section('1. Header / Summary', [
        el('div', {{ class: 'grid' }}, [
          card('Database', [
            metric('Path', DATA.meta.db_path),
            metric('Open mode', DATA.meta.db_open_mode),
            metric('Generated at', DATA.meta.generated_at_utc),
            metric('Tariff', `${{DATA.meta.tariff_uah_per_kwh}} UAH/kWh (${{DATA.meta.tariff_source}})`)
          ]),
          card('Notes', [
            metric('History', DATA.meta.history_note),
            metric('Cache', DATA.meta.cache_note),
            metric('Anomaly count', DATA.anomalies.length, DATA.anomalies.some(a => a.severity === 'warning') ? 'warn' : 'ok')
          ])
        ]),
        card('Table Row Counts And Ranges', table(['Table', 'Kind', 'Rows', 'Min UTC', 'Max UTC'], tableRows)),
        card('Detected Anomalies', table(['Severity', 'Check', 'Detail'], anomalyRows))
      ]);
    }}
    function renderRanges() {{
      const rows = DATA.ranges.map(r => [r.title, r.start, r.end, r.reason]);
      section('2. Automatically Chosen Analysis Ranges', [
        card('Selected Ranges', table(['Range', 'Start UTC', 'End UTC', 'Reason'], rows))
      ]);
    }}
    function renderBattery() {{
      const soh = DATA.soh_summary;
      const children = [
        el('div', {{ class: 'grid' }}, [
          card('SoH Diagnostic', [
            metric('History SoH start', `${{fmt(soh.history_start_percent)}} %`),
            metric('History SoH end', `${{fmt(soh.history_end_percent)}} %`),
            metric('Cache SoH start', `${{fmt(soh.cache_start_percent)}} %`),
            metric('Cache SoH end', `${{fmt(soh.cache_end_percent)}} %`),
            metric('Cache delta', `${{fmt(soh.cache_delta_percent)}} pp`, soh.cache_suspicious ? 'warn' : 'ok'),
            metric('Cache loss/day', `${{fmt(soh.cache_loss_percent_per_day)}} pp/day`, soh.cache_suspicious ? 'warn' : 'ok'),
            metric('Flag rule', soh.suspicious_threshold)
          ])
        ]),
        chartCard('Whole Period Battery SoC / SoH / Voltage', c => lineChart(c, DATA.charts.battery.whole.state, ['soc_percent', 'soh_percent', 'voltage_v'], {{ yUnit: '', yMin: 0, yMax: 105 }})),
        chartCard('Whole Period Battery Energy Wh', c => lineChart(c, DATA.charts.battery.whole.state, ['energy_wh'], {{ yUnit: 'Wh', includeZero: true }})),
        chartCard('Whole Period Battery Net Power W (positive charging, negative discharging)', c => lineChart(c, DATA.charts.battery.whole.netPower, ['net_battery_power_w'], {{ yUnit: 'W', includeZero: true }})),
        chartCard('Cache / Forecast Battery State', c => lineChart(c, DATA.charts.battery.cache.state, ['soc_percent', 'soh_percent', 'voltage_v', 'energy_wh'], {{ yUnit: '', includeZero: true }})),
        chartCard('Cache / Forecast Battery Net Power W', c => lineChart(c, DATA.charts.battery.cache.netPower, ['net_battery_power_w'], {{ yUnit: 'W', includeZero: true }}))
      ];
      for (const r of DATA.ranges.filter(r => r.key !== 'whole')) {{
        const win = DATA.charts.battery.windows[r.key];
        children.push(chartCard(`${{r.title}}: Battery SoC / SoH / Energy / Voltage`, c => lineChart(c, win.state, ['soc_percent', 'soh_percent', 'energy_wh', 'voltage_v'], {{ includeZero: true }})));
        children.push(chartCard(`${{r.title}}: Battery Net Power W`, c => lineChart(c, win.netPower, ['net_battery_power_w'], {{ yUnit: 'W', includeZero: true }})));
      }}
      section('3. Battery Visualizations', children);
    }}
    function renderLoad() {{
      const s = DATA.stats.load;
      const children = [
        el('div', {{ class: 'grid' }}, [
          card('Load Summary', [
            metric('History max load', `${{fmt(s.history.total_load_power_w.max)}} W`),
            metric('History avg load', `${{fmt(s.history.total_load_power_w.avg)}} W`),
            metric('Cache max load', `${{fmt(s.cache.total_load_power_w.max)}} W`),
            metric('Daily energy history max', `${{fmt(s.history.daily_energy_wh_so_far.max)}} Wh`)
          ]),
          card('Top Load Spikes', table(['Timestamp UTC', 'Power W'], s.spikes.map(p => [p.timestamp_utc, p.total_load_power_w])))
        ]),
        chartCard('Whole Period Daily Load Energy kWh', c => barChart(c, DATA.charts.load.dailyEnergy, 'kwh', {{ yUnit: 'kWh', color: COLORS.load }})),
        chartCard('Whole Period Monthly Load Energy kWh', c => barChart(c, DATA.charts.load.monthlyEnergy, 'kwh', {{ yUnit: 'kWh', color: COLORS.load2 }})),
        chartCard('Current Calendar Month Daily Load kWh (elapsed days only)', c => barChart(c, DATA.charts.load.currentMonth, 'kwh', {{ yUnit: 'kWh', color: COLORS.load }})),
        chartCard('Cache / Forecast Load Power W', c => lineChart(c, DATA.charts.load.cachePower, ['total_load_power_w', 'effective_served_load_w'], {{ yUnit: 'W', includeZero: true }}))
      ];
      for (const r of DATA.ranges.filter(r => r.key !== 'whole')) {{
        const win = DATA.charts.load.windows[r.key];
        children.push(chartCard(`${{r.title}}: Load Power W`, c => lineChart(c, win.power, ['total_load_power_w', 'effective_served_load_w', 'load_cut_by_ems_w'], {{ yUnit: 'W', includeZero: true }})));
        children.push(chartCard(`${{r.title}}: Daily Energy Wh So Far Pattern`, c => lineChart(c, win.dailyPattern, ['daily_energy_wh_so_far'], {{ yUnit: 'Wh', includeZero: true }})));
      }}
      section('4. Load Visualizations', children);
    }}
    function renderSolarEconomics() {{
      const children = [
        chartCard('Monthly Solar-Covered Percent', c => barChart(c, DATA.charts.solarEconomics.monthly, 'solarCoveredPercent', {{ yUnit: '%', color: COLORS.solar }})),
        chartCard('Monthly Money Saved UAH', c => barChart(c, DATA.charts.solarEconomics.monthly, 'moneySavedUah', {{ yUnit: 'UAH', color: COLORS.moneySavedUah }}))
      ];
      for (const r of DATA.ranges.filter(r => r.key !== 'whole')) {{
        const win = DATA.charts.solarEconomics.windows[r.key];
        children.push(chartCard(`${{r.title}}: Daily Solar-Covered Percent`, c => barChart(c, win, 'solarCoveredPercent', {{ yUnit: '%', color: COLORS.solar }})));
        children.push(chartCard(`${{r.title}}: Daily Money Saved UAH`, c => barChart(c, win, 'moneySavedUah', {{ yUnit: 'UAH', color: COLORS.moneySavedUah }})));
      }}
      section('5. Solar Coverage And Money Saving', children);
    }}
    function renderEms() {{
      const flowFields = ['grid_to_load_w','grid_to_battery_w','solar_to_load_w','solar_to_battery_w','battery_to_load_w','applied_charge_power_w','effective_load_power_w'];
      const dailyModeFields = Object.keys(DATA.charts.ems.dailyModeCounts[0] || {{}}).filter(k => k !== 'label');
      const modeRows = DATA.charts.ems.modeCounts.map(m => [m.label, m.count]);
      const children = [
        card('Selected Mode Distribution', table(['Mode', 'Rows'], modeRows)),
        chartCard('Whole Period Risk Score', c => lineChart(c, DATA.charts.ems.riskWhole, ['auto_risk_score'], {{ yUnit: '', yMin: 0, yMax: 100 }})),
        chartCard('Selected Mode Timeline / Daily Stacked Counts', c => stackedBarChart(c, DATA.charts.ems.dailyModeCounts, dailyModeFields)),
        chartCard('Protection Active Count By Day', c => barChart(c, DATA.charts.ems.protectionDaily, 'count', {{ yUnit: 'rows', color: COLORS.protection }})),
        chartCard('Curtailed / Cut Load By Day Wh', c => barChart(c, DATA.charts.ems.curtailedDaily, 'wh', {{ yUnit: 'Wh', color: COLORS.curtailed_or_cut_load_w }})),
        chartCard('Whole Period EMS Flow Powers W', c => lineChart(c, DATA.charts.ems.flowWhole, flowFields, {{ yUnit: 'W', includeZero: true }}))
      ];
      for (const r of DATA.ranges.filter(r => r.key !== 'whole')) {{
        const win = DATA.charts.ems.windows[r.key];
        const winModeFields = Object.keys(win.modes[0] || {{}}).filter(k => k !== 'label');
        children.push(chartCard(`${{r.title}}: Risk Score`, c => lineChart(c, win.risk, ['auto_risk_score'], {{ yUnit: '', yMin: 0, yMax: 100 }})));
        children.push(chartCard(`${{r.title}}: Flow Powers W`, c => lineChart(c, win.flows, flowFields, {{ yUnit: 'W', includeZero: true }})));
        children.push(chartCard(`${{r.title}}: Daily Mode Counts`, c => stackedBarChart(c, win.modes, winModeFields)));
      }}
      section('6. EMS Visualizations', children);
    }}
    function renderConsistency() {{
      const stats = DATA.stats;
      section('7. Cross-Module Consistency Checks', [
        el('div', {{ class: 'grid' }}, [
          card('Battery', [
            metric('History SoC range', `${{fmt(stats.battery.history.soc_percent.min)}}..${{fmt(stats.battery.history.soc_percent.max)}} %`),
            metric('Cache SoC range', `${{fmt(stats.battery.cache.soc_percent.min)}}..${{fmt(stats.battery.cache.soc_percent.max)}} %`),
            metric('History voltage range', `${{fmt(stats.battery.history.voltage_v.min)}}..${{fmt(stats.battery.history.voltage_v.max)}} V`),
            metric('Cache voltage range', `${{fmt(stats.battery.cache.voltage_v.min)}}..${{fmt(stats.battery.cache.voltage_v.max)}} V`)
          ]),
          card('EMS', [
            metric('History risk range', `${{fmt(stats.ems.history.risk_score.min)}}..${{fmt(stats.ems.history.risk_score.max)}}`),
            metric('Cache risk range', `${{fmt(stats.ems.cache.risk_score.min)}}..${{fmt(stats.ems.cache.risk_score.max)}}`),
            metric('History protection rows', fmt(stats.ems.history.protection_active_rows, 0)),
            metric('Negative EMS flow rows', fmt(stats.ems.history.negative_flow_rows + stats.ems.cache.negative_flow_rows, 0))
          ])
        ]),
        card('Anomaly Details', table(['Severity', 'Check', 'Detail'], DATA.anomalies.map(a => [a.severity, a.label, a.detail])))
      ]);
    }}
    render();
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
