import argparse
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt


DEFAULT_DB_PATH = Path("backend/data/smartenergy.db")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Plot daily ideal and weather-adjusted solar energy over a date range."
    )
    parser.add_argument("--start", required=True, help="Start date, e.g. 2025-10-06")
    parser.add_argument("--end", required=True, help="End date, e.g. 2026-05-09, exclusive")
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
            substr(timestamp_local, 1, 10) AS day,
            ROUND(SUM(ideal_power_w) * 0.25 / 1000, 4) AS ideal_kwh,
            ROUND(SUM(simulated_power_w) * 0.25 / 1000, 4) AS simulated_kwh,
            ROUND(AVG(weather_factor), 4) AS avg_weather_factor
        FROM simulatedsolarproduction
        WHERE timestamp_local >= ?
          AND timestamp_local < ?
        GROUP BY day
        ORDER BY day
        """,
        (args.start, args.end),
    ).fetchall()

    con.close()

    if not rows:
        raise RuntimeError("No simulated solar rows found for selected range.")

    days = [row[0] for row in rows]
    ideal_kwh = [row[1] for row in rows]
    simulated_kwh = [row[2] for row in rows]
    avg_weather_factor = [row[3] for row in rows]

    plt.figure(figsize=(15, 6))
    plt.plot(days, ideal_kwh, label="Ideal daily energy, kWh")
    plt.plot(days, simulated_kwh, label="Weather-adjusted daily energy, kWh")

    plt.title(f"Daily solar energy: {args.start} to {args.end}")
    plt.xlabel("Date")
    plt.ylabel("Energy, kWh/day")
    plt.legend()
    plt.grid(True)

    step = max(1, len(days) // 14)
    plt.xticks(ticks=range(0, len(days), step), labels=days[::step], rotation=45)

    plt.tight_layout()
    plt.show()

    print("Days:", len(days))
    print("Ideal total kWh:", round(sum(ideal_kwh), 3))
    print("Simulated total kWh:", round(sum(simulated_kwh), 3))
    print("Average ideal daily kWh:", round(sum(ideal_kwh) / len(ideal_kwh), 3))
    print("Average simulated daily kWh:", round(sum(simulated_kwh) / len(simulated_kwh), 3))
    print("Average weather factor:", round(sum(avg_weather_factor) / len(avg_weather_factor), 3))
    print("Best simulated day:", days[simulated_kwh.index(max(simulated_kwh))], round(max(simulated_kwh), 3), "kWh")
    print("Worst simulated day:", days[simulated_kwh.index(min(simulated_kwh))], round(min(simulated_kwh), 3), "kWh")


if __name__ == "__main__":
    main()