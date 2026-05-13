import { useEffect, useMemo, useState } from "react";

export const DEFAULT_STATION_TIMEZONE = "Europe/Kyiv";

export function useStationClock(
  timezone = DEFAULT_STATION_TIMEZONE,
  updateMs = 1000,
) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const stationTimezone = timezone || DEFAULT_STATION_TIMEZONE;

  useEffect(() => {
    setNowMs(Date.now());
    const timer = window.setInterval(() => {
      setNowMs(Date.now());
    }, updateMs);
    return () => window.clearInterval(timer);
  }, [updateMs]);

  return useMemo(() => {
    const parts = datePartsInTimezone(nowMs, stationTimezone);
    return {
      nowMs,
      dateKey: parts.dateKey,
      timeLabel: `${pad(parts.hour)}:${pad(parts.minute)}`,
      hourFloat: parts.hour + parts.minute / 60 + parts.second / 3600,
    };
  }, [nowMs, stationTimezone]);
}

function datePartsInTimezone(timestampMs, timezone) {
  try {
    const parts = new Intl.DateTimeFormat("uk-UA", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(timestampMs));
    const part = (type) =>
      parts.find((item) => item.type === type)?.value ?? "00";
    return {
      dateKey: `${part("year")}-${part("month")}-${part("day")}`,
      hour: Number(part("hour")),
      minute: Number(part("minute")),
      second: Number(part("second")),
    };
  } catch {
    if (timezone !== DEFAULT_STATION_TIMEZONE) {
      return datePartsInTimezone(timestampMs, DEFAULT_STATION_TIMEZONE);
    }
    const fallback = new Date(timestampMs);
    return {
      dateKey: fallback.toISOString().slice(0, 10),
      hour: fallback.getHours(),
      minute: fallback.getMinutes(),
      second: fallback.getSeconds(),
    };
  }
}

function pad(value) {
  return String(Math.floor(value)).padStart(2, "0");
}
