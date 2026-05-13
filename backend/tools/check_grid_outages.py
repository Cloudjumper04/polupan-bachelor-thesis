import requests
import pandas as pd
import matplotlib.pyplot as plt

API_URL = "http://localhost:6001/api/grid/history"

params = {
    "start": "2025-10-06T00:00:00+03:00",
    "end": "2026-05-20T23:59:59+03:00",
}

response = requests.get(API_URL, params=params, timeout=60)
response.raise_for_status()
data = response.json()

points = data.get("points", [])
if not points:
    raise SystemExit("No grid history points returned from API.")

df = pd.DataFrame(points)

df["timestamp_local_parsed"] = (
    pd.to_datetime(df["timestamp_local"], utc=True)
      .dt.tz_convert("Europe/Kyiv")
)

df["date"] = df["timestamp_local_parsed"].dt.date

daily = (
    df.groupby("date")
      .agg(
          outage_hours=("daily_outage_hours", "max"),
          max_deficit=("deficit_percent", "max"),
          min_generation=("generation_health_percent", "min"),
          min_delivery=("delivery_health_percent", "min"),
      )
      .reset_index()
)

print(f"Days analyzed: {len(daily)}")
print(f"Days with outages: {int((daily['outage_hours'] > 0).sum())}")
print(f"Max outage hours/day: {daily['outage_hours'].max()}")
print(f"Max deficit percent: {daily['max_deficit'].max()}")

print("\nWorst outage days:")
print(
    daily.sort_values(["outage_hours", "max_deficit"], ascending=False)
         .head(30)
         .to_string(index=False)
)

print("\nWinter worst deficit days:")
winter = daily[
    (daily["date"].astype(str) >= "2025-11-01")
    & (daily["date"].astype(str) <= "2026-03-31")
]
print(
    winter.sort_values("max_deficit", ascending=False)
          .head(30)
          .to_string(index=False)
)

plt.figure(figsize=(14, 5))
plt.plot(daily["date"], daily["outage_hours"])
plt.title("Simulated daily power outage hours")
plt.xlabel("Date")
plt.ylabel("Hours without electricity")
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig("grid_outage_hours.png", dpi=160)

print("\nSaved chart: grid_outage_hours.png")
