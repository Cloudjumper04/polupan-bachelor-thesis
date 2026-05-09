import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt


DEFAULT_DB_PATH = Path("backend/data/smartenergy.db")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Plot ideal and weather-adjusted solar generation for a date range."
    )
    parser.add_argument("--start", required=True, help="Start datetime/date, e.g. 2026-03-21")
    parser.add_argument("--end", required=True, help="End datetime/date, e.g. 2026-03-22")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    return parser.parse_args()


def main():
    args = parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    con = sqlite3.connect(db_path)

    rows = con.execute(
        """
        SELECT
            s.timestamp_local,
            s.ideal_power_w,
            s.simulated_power_w,
            s.weather_state,
            s.cloud_cover_percent,
            s.weather_factor
        FROM simulatedsolarproduction s
        WHERE s.timestamp_local >= ?
          AND s.timestamp_local < ?
        ORDER BY s.timestamp_local
        """,
        (args.start, args.end),
    ).fetchall()

    con.close()

    if not rows:
        raise RuntimeError("No simulated solar rows found for selected range.")

    timestamps = [row[0] for row in rows]
    ideal_power = [row[1] for row in rows]
    simulated_power = [row[2] for row in rows]
    weather_state = [row[3] for row in rows]
    cloud_cover = [row[4] for row in rows]
    weather_factor = [row[5] for row in rows]

    plt.figure(figsize=(14, 6))
    plt.plot(timestamps, ideal_power, label="Ideal clear-sky power, W")
    plt.plot(timestamps, simulated_power, label="Weather-adjusted power, W")

    plt.title(f"Solar generation: {args.start} to {args.end}")
    plt.xlabel("Time")
    plt.ylabel("Power, W")
    plt.legend()
    plt.grid(True)

    step = max(1, len(timestamps) // 12)
    plt.xticks(ticks=range(0, len(timestamps), step), labels=timestamps[::step], rotation=45)

    plt.tight_layout()
    plt.show()

    print("Rows:", len(rows))
    print("Ideal max W:", round(max(ideal_power), 2))
    print("Simulated max W:", round(max(simulated_power), 2))
    print("Average weather factor:", round(sum(weather_factor) / len(weather_factor), 3))
    print("Weather states:", sorted(set(weather_state)))
    print("Average cloud cover:", round(sum(c for c in cloud_cover if c is not None) / len(cloud_cover), 2))


if __name__ == "__main__":
    main()