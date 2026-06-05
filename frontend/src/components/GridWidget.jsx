import { ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  fetchGridCurrent,
  fetchGridHistory,
  fetchGridOutages,
} from "../api/grid";
import { useStationClock } from "../hooks/useStationClock";

const GRID_REFRESH_MS = 45000;
const MAX_HISTORY_POINTS = 600;
const CHART_NAMES = [
  "Сумарний час відключень",
  "Цілісність мережі",
  "Графік відключень",
];
const WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"];
const FALLBACK_GRID_CURRENT = {
  timestamp_local: "2026-01-14T13:26:00+02:00",
  local_grid_available: true,
  is_outage_now: false,
  grid_voltage_v: 218,
  effective_health_percent: 36,
  daily_outage_hours: 15,
  outage_level: "severe_outage",
  outage_queue: "3.1",
  reason: "generation bottleneck",
};

export default function GridWidget({
  stationTimezone: sharedStationTimezone = null,
  selectedDashboardTime = null,
}) {
  const [expanded, setExpanded] = useState(false);
  const [chartIndex, setChartIndex] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [currentPayload, setCurrentPayload] = useState(null);
  const [currentStatus, setCurrentStatus] = useState("loading");
  const [currentError, setCurrentError] = useState("");
  const [currentLoadVersion, setCurrentLoadVersion] = useState(0);
  const [todayOutages, setTodayOutages] = useState(null);
  const [todayStatus, setTodayStatus] = useState("idle");
  const [todayError, setTodayError] = useState("");
  const [weekHistory, setWeekHistory] = useState(null);
  const [weekHistoryStatus, setWeekHistoryStatus] = useState("idle");
  const [weekHistoryError, setWeekHistoryError] = useState("");
  const [weekOutages, setWeekOutages] = useState({});
  const [weekOutagesStatus, setWeekOutagesStatus] = useState("idle");
  const [weekOutagesError, setWeekOutagesError] = useState("");
  const [historyStart, setHistoryStart] = useState("");
  const [historyEnd, setHistoryEnd] = useState("");
  const [historyPayload, setHistoryPayload] = useState(null);
  const [historyStatus, setHistoryStatus] = useState("idle");
  const [historyError, setHistoryError] = useState("");
  const [displayVoltage, setDisplayVoltage] = useState(() =>
    formatVoltage(FALLBACK_GRID_CURRENT.grid_voltage_v),
  );

  const currentPoint = currentPayload?.current ?? null;
  const visibleCurrent = currentPoint ?? FALLBACK_GRID_CURRENT;
  const stationTimezone =
    currentPayload?.station?.timezone ??
    todayOutages?.timezone ??
    sharedStationTimezone ??
    "Europe/Kyiv";
  const stationClock = useStationClock(stationTimezone);
  const historyMode = Boolean(selectedDashboardTime);
  const selectedClock = useMemo(
    () =>
      historyMode
        ? selectedClockParts(selectedDashboardTime, stationTimezone)
        : null,
    [historyMode, selectedDashboardTime, stationTimezone],
  );
  const dashboardClock = selectedClock ?? stationClock;
  const todayKey = dashboardClock.dateKey;
  const currentHour = dashboardClock.hourFloat;
  const currentTimeLabel = dashboardClock.timeLabel;
  const weekStartKey = startOfWeekKey(todayKey);
  const weekEndKey = addDaysToDateKey(weekStartKey, 6);
  const queueLabel =
    currentPoint?.outage_queue ??
    todayOutages?.outage_queue ??
    firstOutageQueue(weekOutages) ??
    "3.1";
  const todayOutagesLoaded =
    todayOutages?.date_local === todayKey && Array.isArray(todayOutages?.windows);
  const todayWindows = normalizeOutageWindows(
    todayOutagesLoaded ? todayOutages.windows : [],
    todayKey,
  );
  const gridOn = isGridAvailable(
    visibleCurrent,
    todayWindows,
    currentHour,
    todayOutagesLoaded,
  );
  const chartName = CHART_NAMES[chartIndex];
  const showHistory = chartIndex === 0 || chartIndex === 1;
  const weekDays = useMemo(
    () => buildWeekDays(weekStartKey, todayKey),
    [weekStartKey, todayKey],
  );
  const weeklySeries = useMemo(
    () => aggregateWeeklyHistory(weekHistory?.points ?? [], weekDays),
    [weekHistory, weekDays],
  );
  const weeklySchedule = useMemo(
    () => buildWeeklySchedule(weekDays, weekOutages, todayKey, currentHour),
    [weekDays, weekOutages, todayKey, currentHour],
  );
  const historySeries = useMemo(
    () => aggregateHistoryRange(historyPayload?.points ?? [], chartIndex),
    [historyPayload, chartIndex],
  );
  const shouldUseHistoryLine =
    chartIndex === 1 || selectedRangeDays(historyStart, historyEnd) > 14;

  useEffect(() => {
    const controller = new AbortController();

    async function loadCurrent() {
      setCurrentStatus("loading");
      try {
        const payload = await fetchGridCurrent({
          at: selectedDashboardTime,
          signal: controller.signal,
        });
        setCurrentPayload(payload);
        setCurrentStatus(payload?.current ? "ready" : "empty");
        setCurrentError("");
        setCurrentLoadVersion((value) => value + 1);
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setCurrentStatus("error");
        setCurrentError("дані мережі недоступні");
      }
    }

    loadCurrent();
    const timer = historyMode ? null : window.setInterval(loadCurrent, GRID_REFRESH_MS);
    return () => {
      controller.abort();
      if (timer) window.clearInterval(timer);
    };
  }, [historyMode, selectedDashboardTime]);

  useEffect(() => {
    setDisplayVoltage(calculateDisplayVoltage(visibleCurrent, gridOn, dashboardClock.nowMs));
  }, [
    gridOn,
    dashboardClock.nowMs,
    visibleCurrent.deficit_percent,
    visibleCurrent.grid_voltage_v,
    visibleCurrent.national_deficit_percent,
    visibleCurrent.outage_level,
    visibleCurrent.outage_queue,
  ]);

  useEffect(() => {
    if (!todayKey) return;
    const controller = new AbortController();

    async function loadTodayOutages() {
      setTodayStatus("loading");
      try {
        setTodayOutages(await fetchGridOutages(todayKey, { signal: controller.signal }));
        setTodayStatus("ready");
        setTodayError("");
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setTodayStatus("error");
        setTodayError("графік недоступний");
      }
    }

    loadTodayOutages();
    return () => controller.abort();
  }, [todayKey, currentLoadVersion]);

  useEffect(() => {
    if (!expanded || chartIndex > 1 || !weekStartKey || !weekEndKey) return;
    const controller = new AbortController();

    async function loadWeekHistory() {
      setWeekHistoryStatus("loading");
      try {
        const payload = await fetchGridHistory(
          `${weekStartKey}T00:00:00`,
          `${addDaysToDateKey(weekEndKey, 1)}T00:00:00`,
          { signal: controller.signal },
        );
        setWeekHistory(payload);
        setWeekHistoryStatus("ready");
        setWeekHistoryError("");
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setWeekHistoryStatus("error");
        setWeekHistoryError("дані графіка недоступні");
      }
    }

    loadWeekHistory();
    return () => controller.abort();
  }, [expanded, chartIndex, weekStartKey, weekEndKey]);

  useEffect(() => {
    if (!expanded || chartIndex !== 2 || !weekStartKey || !todayKey) return;
    const controller = new AbortController();
    const dates = datesBetween(weekStartKey, todayKey);

    async function loadWeekOutages() {
      setWeekOutagesStatus("loading");
      try {
        const payloads = await Promise.all(
          dates.map((dateKey) =>
            fetchGridOutages(dateKey, { signal: controller.signal }),
          ),
        );
        setWeekOutages(
          Object.fromEntries(payloads.map((payload) => [payload.date_local, payload])),
        );
        setWeekOutagesStatus("ready");
        setWeekOutagesError("");
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setWeekOutagesStatus("error");
        setWeekOutagesError("графік відключень недоступний");
      }
    }

    loadWeekOutages();
    return () => controller.abort();
  }, [expanded, chartIndex, weekStartKey, todayKey]);

  useEffect(() => {
    if (!expanded || !showHistory || historyStart || historyEnd) return;
    setHistoryStart(formatDateKeyForDisplay(weekStartKey));
    setHistoryEnd(formatDateKeyForDisplay(todayKey));
  }, [expanded, showHistory, historyStart, historyEnd, weekStartKey, todayKey]);

  function selectPreviousChart() {
    setChartIndex((value) => (value + CHART_NAMES.length - 1) % CHART_NAMES.length);
    setHistoryOpen(false);
  }

  function selectNextChart() {
    setChartIndex((value) => (value + 1) % CHART_NAMES.length);
    setHistoryOpen(false);
  }

  async function applyHistoryRange() {
    const normalized = normalizeHistoryInputs(historyStart, historyEnd);
    if (!normalized) {
      setHistoryError("Введіть дату у форматі ДД.ММ.РРРР");
      setHistoryPayload(null);
      setHistoryStatus("error");
      return;
    }
    setHistoryStatus("loading");
    try {
      const payload = await fetchGridHistory(normalized.startIso, normalized.endIso);
      setHistoryPayload(payload);
      const returnedRange = historyInputRangeFromPoints(payload?.points ?? []);
      if (returnedRange) {
        setHistoryStart(returnedRange.startInput);
        setHistoryEnd(returnedRange.endInput);
      }
      setHistoryStatus("ready");
      setHistoryError("");
    } catch {
      setHistoryStatus("error");
      setHistoryError("історія мережі недоступна");
      setHistoryPayload(null);
    }
  }

  return (
    <section className={`grid-card${expanded ? " expanded" : ""}`}>
      <div className="grid-main">
        <div className="grid-top">
          <div className="grid-status-cluster">
            <div className="switch-group network-tile">
              <div className="network-copy">
                <h1 className="grid-title">Мережа</h1>
                <div className="network-state-text">
                  {gridOn ? "Увімк." : "Вимк."}
                </div>
              </div>
              <div
                className={`power-leds ${gridOn ? "on" : "off"}`}
                title={gridOn ? "Мережа доступна" : "Мережа недоступна"}
              >
                <span className="power-state-bar green" aria-hidden="true" />
                <span className="power-state-bar red" aria-hidden="true" />
              </div>
            </div>

            <div className="grid-top-divider separator-after-switch" />

            <IntegrityStatus current={visibleCurrent} />

            <div className="grid-top-divider separator-after-integrity" />

            <div className="voltage-group">
              <div className="metric-label">Напруга</div>
              <div className="grid-voltage">
                {formatVoltage(displayVoltage)}
                <span className="unit"> V</span>
              </div>
            </div>
          </div>
        </div>

        <ScheduleStrip
          currentHour={currentHour}
          currentTimeLabel={currentTimeLabel}
          loading={todayStatus === "loading" && !todayOutages}
          nextOutageLabel={nextOutageLabel(todayWindows, currentHour)}
          todayWindows={todayWindows}
        />
        {(currentStatus === "error" || todayStatus === "error") && (
          <div className="grid-data-note">{currentError || todayError}</div>
        )}
        {currentStatus === "empty" && (
          <div className="grid-data-note">дані мережі ще не згенеровані</div>
        )}
      </div>

      <button
        className="grid-expand-button"
        type="button"
        aria-label={expanded ? "Згорнути графіки мережі" : "Розгорнути графіки мережі"}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <ChevronDown aria-hidden="true" />
      </button>

      <section className="grid-expanded" aria-label="Деталі стану мережі">
        <div className="expanded-chart-panel">
          <div className="expanded-chart-head">
            <button
              className="chart-select-button"
              type="button"
              aria-label="Попередній графік"
              onClick={selectPreviousChart}
            >
              <ChevronLeft aria-hidden="true" />
            </button>
            <div className="selected-chart-title">
              <span>{chartName}</span>
              {chartIndex === 2 && (
                <span className="week-queue-label" title={`Черга ${queueLabel}`}>
                  {queueLabel}
                </span>
              )}
            </div>
            <button
              className="chart-select-button"
              type="button"
              aria-label="Наступний графік"
              onClick={selectNextChart}
            >
              <ChevronRight aria-hidden="true" />
            </button>
          </div>

          <div className="grid-chart-body">
            {chartIndex === 0 && (
              <OutageHoursChart
                data={weeklySeries}
                status={weekHistoryStatus}
                error={weekHistoryError}
              />
            )}
            {chartIndex === 1 && (
              <IntegrityChart
                data={weeklySeries}
                status={weekHistoryStatus}
                error={weekHistoryError}
              />
            )}
            {chartIndex === 2 && (
              <ScheduleChart
                data={weeklySchedule}
                status={weekOutagesStatus}
                error={weekOutagesError}
              />
            )}
          </div>

          <div className="chart-footer">
            {showHistory && (
              <button
                className="view-history-link"
                type="button"
                onClick={() => setHistoryOpen((value) => !value)}
              >
                переглянути історію
              </button>
            )}
          </div>

          <div className={`history-inline ${historyOpen && showHistory ? "active" : ""}`}>
            <label className="grid-history-field">
              <span>Від</span>
              <input
                type="text"
                inputMode="numeric"
                placeholder="ДД.ММ.РРРР"
                value={historyStart}
                onChange={(event) => {
                  setHistoryStart(event.target.value);
                  setHistoryError("");
                }}
              />
            </label>
            <label className="grid-history-field">
              <span>До</span>
              <input
                type="text"
                inputMode="numeric"
                placeholder="ДД.ММ.РРРР"
                value={historyEnd}
                onChange={(event) => {
                  setHistoryEnd(event.target.value);
                  setHistoryError("");
                }}
              />
            </label>
            <button
              className="show-button"
              type="button"
              disabled={historyStatus === "loading"}
              onClick={applyHistoryRange}
            >
              Показати
            </button>
          </div>
          {historyOpen && showHistory && (
            <HistoryChart
              chartIndex={chartIndex}
              data={historySeries}
              error={historyError}
              loading={historyStatus === "loading"}
              useLine={shouldUseHistoryLine}
            />
          )}
        </div>
      </section>
    </section>
  );
}

function IntegrityStatus({ current }) {
  const integrity = cappedPercent(current.effective_health_percent);
  const lightHours = 24 - nullableNumber(current.daily_outage_hours, 0);
  const isBlackout = current.outage_level === "blackout" || lightHours < 1 || integrity <= 0;
  const integrityClass = integrityFillClass(integrity);
  const title = isBlackout
    ? "Повний blackout: менше однієї години світла за добу"
    : `Фактична цілісність: ${formatFixed(integrity)}%. Причина: ${reasonUk(current.reason)}`;

  return (
    <div className="integrity-status-group integrity-widget" title={title}>
      <div className="integrity-copy">
        <div className="metric-label">Цілісність мережі</div>
        {isBlackout ? (
          <div className="blackout-state inline">Blackout</div>
        ) : (
          <div className="integrity-main-number">
            {integrity.toFixed(0)}
            <span className="unit">%</span>
          </div>
        )}
      </div>
      {!isBlackout && (
        <IntegrityCircle value={integrity} colorClass={integrityClass} />
      )}
    </div>
  );
}

function IntegrityCircle({ value, colorClass }) {
  const radius = 45;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - value / 100);

  return (
    <div
      className="integrity-circle"
      style={{ "--integrity-color": integrityColor(colorClass) }}
      aria-hidden="true"
    >
      <svg className="integrity-ring-svg" viewBox="0 0 112 112">
        <circle className="integrity-ring-bg" cx="56" cy="56" r={radius} />
        <circle
          className="integrity-ring-value"
          cx="56"
          cy="56"
          r={radius}
          strokeDasharray={`${circumference} ${circumference}`}
          strokeDashoffset={offset}
        />
      </svg>
    </div>
  );
}

function ScheduleStrip({
  currentHour,
  currentTimeLabel,
  loading,
  nextOutageLabel,
  todayWindows,
}) {
  const hourSlots = Array.from({ length: 24 }, (_, hour) => {
    const state = hourOffState(hour, todayWindows);
    const title =
      state === "idle"
        ? `${pad(hour)}:00-${pad(hour + 1)}:00 - світло є`
        : outageTooltipForHour(hour, todayWindows);
    return <div key={hour} className={`hour-slot ${state}`} title={title} />;
  });
  const currentLeft = (clamp(currentHour, 0, 24) / 24) * 100;

  return (
    <section className="schedule-panel">
      <div className="schedule-head">
        <div className="schedule-title-block">
          <div className="schedule-title">Графік відключень на сьогодні</div>
          <div className="schedule-meta">
            поточний час {currentTimeLabel || "--:--"}
          </div>
        </div>
        <div className="schedule-meta">
          {loading ? "оновлення графіка..." : nextOutageLabel}
        </div>
      </div>
      <div className="slot-wrap">
        <div className="slot-grid">{hourSlots}</div>
        <div
          className="time-marker"
          style={{ left: `${currentLeft}%` }}
          title={`Поточний час ${currentTimeLabel || "--:--"}`}
        />
        <div className="time-labels">
          {[0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24].map((value) => (
            <span
              key={value}
              className={value === 24 ? "end-label" : ""}
              style={{ left: `${(value / 24) * 100}%` }}
            >
              {value}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function OutageHoursChart({ data, status, error }) {
  const chartData = data.map((point) => ({ ...point, value: point.outage }));
  if (status === "loading" && chartData.every((point) => point.value === null)) {
    return <GridChartMessage text="завантаження графіка..." />;
  }
  if (error) return <GridChartMessage text={error} />;
  if (chartData.every((point) => point.value === null)) {
    return <GridChartMessage text="немає даних за поточний тиждень" />;
  }
  return (
    <BarChartSvg
      data={chartData}
      max={Math.max(16, ...chartData.map((point) => point.value ?? 0))}
      unit="год"
    />
  );
}

function IntegrityChart({ data, status, error }) {
  const chartData = data.map((point) => ({ ...point, value: point.health }));
  if (status === "loading" && chartData.every((point) => point.value === null)) {
    return <GridChartMessage text="завантаження графіка..." />;
  }
  if (error) return <GridChartMessage text={error} />;
  if (chartData.every((point) => point.value === null)) {
    return <GridChartMessage text="немає даних за поточний тиждень" />;
  }
  return <LineChartSvg data={chartData} max={100} unit="%" />;
}

function ScheduleChart({ data, status, error }) {
  if (status === "loading" && data.every((point) => point.blocks.length === 0)) {
    return <GridChartMessage text="завантаження графіка..." />;
  }
  if (error) return <GridChartMessage text={error} />;
  return <WeekScheduleTable data={data} />;
}

function HistoryChart({ chartIndex, data, error, loading, useLine }) {
  if (loading) return <GridChartMessage text="завантаження історії..." compact />;
  if (error) return <GridChartMessage text={error} compact />;
  if (!data || data.length === 0) {
    return <GridChartMessage text="оберіть період для історії" compact />;
  }
  const unit = chartIndex === 0 ? "год" : "%";
  const max =
    chartIndex === 0
      ? Math.max(1, ...data.map((point) => point.value ?? 0)) * 1.15
      : 100;
  return (
    <div className="grid-history-chart">
      {useLine ? (
        <LineChartSvg data={data} max={max} unit={unit} />
      ) : (
        <BarChartSvg data={data} max={max} unit={unit} />
      )}
    </div>
  );
}

function GridChartMessage({ text, compact = false }) {
  return <div className={`grid-chart-message${compact ? " compact" : ""}`}>{text}</div>;
}

function WeekScheduleTable({ data }) {
  const hours = Array.from({ length: 24 }, (_, index) => index);

  return (
    <div className="week-schedule-table">
      <div className="hour-label" />
      {hours.map((hour) => (
        <div key={`hour-${hour}`} className="hour-label">
          {pad(hour)}
        </div>
      ))}
      {data.map((day, rowIndex) => [
        <div key={`${day.dateKey}-label`} className="day-label">
          {day.day}
        </div>,
        ...hours.map((hour) => {
          const off = hourOffState(hour, day.blocks) !== "idle";
          const current = day.current && hour === Math.floor(day.currentHour);
          return (
            <div
              key={`${rowIndex}-${hour}`}
              className={`week-cell${off ? " off" : ""}${current ? " now" : ""}`}
              title={scheduleCellTitle(day, hour, off)}
            />
          );
        }),
      ])}
    </div>
  );
}

function BarChartSvg({ data, max, unit }) {
  const width = 1000;
  const height = 258;
  const padLeft = 48;
  const padRight = 20;
  const padTop = 16;
  const padBottom = 56;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const step = plotWidth / Math.max(1, data.length);
  const barWidth = Math.max(3, step * 0.58);

  return (
    <svg className="grid-chart-svg" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
      {[0, 0.5, 1].map((gridValue) => {
        const y = padTop + plotHeight - gridValue * plotHeight;
        const label = Math.round(max * gridValue);
        return (
          <g key={gridValue}>
            <line className="chart-grid-line" x1={padLeft} y1={y} x2={width - padRight} y2={y} />
            <text className="axis-label" x="8" y={y + 4}>
              {label}
              {unit}
            </text>
          </g>
        );
      })}
      {data.map((point, index) => {
        const x = padLeft + index * step + (step - barWidth) / 2;
        const value = point.value ?? 0;
        const barHeight = (value / max) * plotHeight;
        const y = padTop + plotHeight - barHeight;
        const centerX = x + barWidth / 2;
        const showLabel = shouldShowAxisLabel(point, index, data.length);
        const showDate = shouldShowBoundaryDateLabel(point, index, data.length, showLabel);

        return (
          <g key={point.dateKey ?? point.day}>
            {point.value !== null && point.value !== undefined && (
              <rect className="bar-rect" x={x} y={y} width={barWidth} height={barHeight} rx="5" />
            )}
            {showLabel && (
              <text className="axis-label" textAnchor="middle" x={centerX} y={height - 33}>
                {point.day}
              </text>
            )}
            {showDate && (
              <text className="axis-label date-under-label" textAnchor="middle" x={centerX} y={height - 13}>
                {point.date}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function LineChartSvg({ data, max, unit }) {
  const [hoveredPoint, setHoveredPoint] = useState(null);
  const width = 1000;
  const height = 258;
  const padLeft = 48;
  const padRight = 20;
  const padTop = 16;
  const padBottom = 56;
  const plotWidth = width - padLeft - padRight;
  const plotHeight = height - padTop - padBottom;
  const x = (index) =>
    data.length <= 1 ? padLeft + plotWidth / 2 : padLeft + (index / (data.length - 1)) * plotWidth;
  const y = (value) => padTop + plotHeight - (value / max) * plotHeight;
  const actual = data
    .map((point, index) => ({ ...point, index }))
    .filter((point) => point.value !== null && point.value !== undefined);
  const path = actual
    .map((point, index) => `${index === 0 ? "M" : "L"}${x(point.index).toFixed(1)},${y(point.value).toFixed(1)}`)
    .join(" ");
  const area =
    actual.length > 1
      ? `${path} L${x(actual.at(-1).index).toFixed(1)},${padTop + plotHeight} L${x(actual[0].index).toFixed(1)},${padTop + plotHeight} Z`
      : "";
  const activeX =
    hoveredPoint === null ? 0 : x(hoveredPoint.index);
  const activeY =
    hoveredPoint === null ? 0 : y(hoveredPoint.value);

  function updateHoveredPoint(event) {
    if (actual.length === 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const pointerX = ((event.clientX - rect.left) / rect.width) * width;
    const nextPoint = actual.reduce((closest, point) =>
      Math.abs(x(point.index) - pointerX) < Math.abs(x(closest.index) - pointerX)
        ? point
        : closest,
    );
    setHoveredPoint(nextPoint);
  }

  return (
    <div className="grid-chart-interactive">
      <svg
        className="grid-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        onMouseMove={updateHoveredPoint}
        onMouseLeave={() => setHoveredPoint(null)}
      >
        {[0, 0.5, 1].map((gridValue) => {
          const yy = padTop + plotHeight - gridValue * plotHeight;
          const label = Math.round(max * gridValue);
          return (
            <g key={gridValue}>
              <line className="chart-grid-line" x1={padLeft} y1={yy} x2={width - padRight} y2={yy} />
              <text className="axis-label" x="8" y={yy + 4}>
                {label}
                {unit}
              </text>
            </g>
          );
        })}
        {actual.length > 1 && <path className="blue-area" d={area} />}
        {actual.length > 1 && <path className="blue-line" d={path} />}
        {hoveredPoint && (
          <circle className="blue-point active" cx={activeX} cy={activeY} r="4" />
        )}
        {data.map((point, index) => {
          const showLabel = shouldShowAxisLabel(point, index, data.length);
          const showDate = shouldShowBoundaryDateLabel(point, index, data.length, showLabel);
          return (
            <g key={point.dateKey ?? point.day}>
              {showLabel && (
                <text className="axis-label" textAnchor="middle" x={x(index)} y={height - 33}>
                  {point.day}
                </text>
              )}
              {showDate && (
                <text className="axis-label date-under-label" textAnchor="middle" x={x(index)} y={height - 13}>
                  {point.date}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {hoveredPoint && (
        <div
          className="grid-line-tooltip"
          style={{
            left: `${(activeX / width) * 100}%`,
            top: `${(activeY / height) * 100}%`,
            transform:
              activeX > width * 0.72
                ? "translate(-100%, calc(-100% - 10px))"
                : "translate(10px, calc(-100% - 10px))",
          }}
        >
          <div className="grid-line-tooltip-label">{lineTooltipLabel(hoveredPoint)}</div>
          <div className="grid-line-tooltip-value">
            {lineTooltipValue(hoveredPoint.value, unit)}
          </div>
        </div>
      )}
    </div>
  );
}

function isGridAvailable(current, windows, currentHour, windowsLoaded) {
  if (windowsLoaded) {
    return !windows.some((window) => window.start <= currentHour && window.end > currentHour);
  }
  return current.local_grid_available !== false && current.is_outage_now !== true;
}

function calculateDisplayVoltage(current, gridOn, nowMs) {
  if (!gridOn || current.outage_level === "blackout") return 0;
  const baseVoltage = nullableNumber(current.grid_voltage_v, 230);
  const amplitude = voltageJitterAmplitude(current);
  const phase = hashString(`${current.outage_queue ?? ""}:${current.outage_level ?? ""}`) / 997;
  const seconds = nowMs / 1000;
  const wave =
    Math.sin(seconds * 0.92 + phase) * 0.62 +
    Math.sin(seconds * 0.21 + phase * 1.7) * 0.38;
  return clamp(baseVoltage + wave * amplitude, 180, 245);
}

function voltageJitterAmplitude(current) {
  const outageLevel = current.outage_level;
  if (outageLevel === "stable") return 1;
  if (outageLevel === "strained") return 2;
  if (outageLevel === "partial_outage") return 4;
  if (outageLevel === "severe_outage") return 7;
  if (outageLevel === "blackout") return 0;

  const deficit = nullableNumber(
    current.deficit_percent ?? current.national_deficit_percent,
    0,
  );
  if (deficit <= 0) return 1;
  if (deficit <= 10) return 2;
  if (deficit <= 40) return 4;
  return 7;
}

function hashString(value) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) % 1000003;
  }
  return hash;
}

function normalizeOutageWindows(windows, dateKey) {
  return (windows ?? [])
    .map((window) => windowToRange(window, dateKey))
    .filter((window) => window && window.end > window.start)
    .sort((left, right) => left.start - right.start);
}

function windowToRange(window, dateKey) {
  const start = localIsoParts(window.start_local);
  const end = localIsoParts(window.end_local);
  if (!start || !end) return null;
  if (start.date > dateKey || end.date < dateKey) return null;
  return {
    start: start.date < dateKey ? 0 : start.hour + start.minute / 60,
    end: end.date > dateKey ? 24 : end.hour + end.minute / 60,
    startLabel: formatPartsTime(start),
    endLabel: formatPartsTime(end),
  };
}

function nextOutageLabel(windows, currentHour) {
  const next = windows.find((window) => window.start > currentHour);
  if (!next) return "відключень не заплановано";
  return `наступне ${next.startLabel}-${next.endLabel}`;
}

function outageTooltipForHour(hour, windows) {
  const matching = windows.find((window) => window.end > hour && window.start < hour + 1);
  if (!matching) return `${pad(hour)}:00-${pad(hour + 1)}:00 - світло є`;
  return `можливе відключення ${matching.startLabel}-${matching.endLabel}`;
}

function hourOffState(hour, windows) {
  const left = windows.some((window) => window.start < hour + 0.5 && window.end > hour);
  const right = windows.some((window) => window.start < hour + 1 && window.end > hour + 0.5);
  if (left && right) return "off-full";
  if (left) return "off-left";
  if (right) return "off-right";
  return "idle";
}

function aggregateWeeklyHistory(points, weekDays) {
  const outageByDate = new Map();
  const healthByDate = new Map();
  for (const point of points) {
    const dateKey = localDateKey(point.timestamp_local);
    if (!dateKey) continue;
    const outage = nullableNumber(point.daily_outage_hours);
    if (outage !== null) {
      outageByDate.set(dateKey, Math.max(outageByDate.get(dateKey) ?? 0, outage));
    }
    const health = nullableNumber(point.effective_health_percent);
    if (health !== null) {
      healthByDate.set(
        dateKey,
        Math.min(healthByDate.get(dateKey) ?? 100, cappedPercent(health)),
      );
    }
  }

  return weekDays.map((day) => ({
    ...day,
    outage: day.future ? null : outageByDate.get(day.dateKey) ?? null,
    health: day.future ? null : healthByDate.get(day.dateKey) ?? null,
    value: day.future ? null : outageByDate.get(day.dateKey) ?? null,
  }));
}

function buildWeeklySchedule(weekDays, outagesByDate, todayKey, currentHour) {
  return weekDays.map((day) => {
    const payload = outagesByDate[day.dateKey];
    return {
      ...day,
      blocks:
        day.dateKey <= todayKey
          ? normalizeOutageWindows(payload?.windows ?? [], day.dateKey)
          : [],
      current: day.dateKey === todayKey,
      currentHour,
    };
  });
}

function aggregateHistoryRange(points, chartIndex) {
  if (!points || points.length === 0) return [];
  const valuesByDate = new Map();
  for (const point of points) {
    const dateKey = localDateKey(point.timestamp_local);
    if (!dateKey) continue;
    const value =
      chartIndex === 0
        ? nullableNumber(point.daily_outage_hours)
        : nullableNumber(point.effective_health_percent);
    if (value === null) continue;
    if (chartIndex === 0) {
      valuesByDate.set(dateKey, Math.max(valuesByDate.get(dateKey) ?? 0, value));
    } else {
      valuesByDate.set(dateKey, Math.min(valuesByDate.get(dateKey) ?? 100, cappedPercent(value)));
    }
  }

  const aggregated = Array.from(valuesByDate.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([dateKey, value]) => ({
      dateKey,
      day: formatShortDate(dateKey),
      date: formatShortDate(dateKey),
      value,
    }));
  return markSparseLabels(downsample(aggregated, MAX_HISTORY_POINTS));
}

function buildWeekDays(weekStartKey, todayKey) {
  return WEEKDAY_LABELS.map((label, index) => {
    const dateKey = addDaysToDateKey(weekStartKey, index);
    return {
      day: label,
      date: formatShortDate(dateKey),
      dateKey,
      future: dateKey > todayKey,
      value: null,
    };
  });
}

function markSparseLabels(data) {
  const every = Math.max(1, Math.ceil(data.length / 12));
  return data.map((point, index) => ({
    ...point,
    showLabel: index % every === 0 || index === data.length - 1,
  }));
}

function downsample(data, maxPoints) {
  if (data.length <= maxPoints) return data;
  const step = Math.ceil(data.length / maxPoints);
  const sampled = data.filter((_, index) => index % step === 0);
  const last = data.at(-1);
  if (sampled.at(-1) !== last) sampled.push(last);
  return sampled;
}

function normalizeHistoryInputs(startInput, endInput) {
  let start = parseHistoryInputDate(startInput);
  let end = parseHistoryInputDate(endInput);
  if (!start || !end) return null;
  if (start > end) {
    [start, end] = [end, start];
  }
  return {
    startInput: formatDateKeyForDisplay(start),
    endInput: formatDateKeyForDisplay(end),
    startIso: `${start}T00:00:00`,
    endIso: `${end}T23:59:59`,
  };
}

function historyInputRangeFromPoints(points) {
  if (!Array.isArray(points) || points.length === 0) return null;
  const firstDate = points.map((point) => localDateKey(point.timestamp_local)).find(isValidDateKey);
  let lastDate = null;
  for (let index = points.length - 1; index >= 0; index -= 1) {
    const dateKey = localDateKey(points[index]?.timestamp_local);
    if (isValidDateKey(dateKey)) {
      lastDate = dateKey;
      break;
    }
  }
  if (!firstDate || !lastDate) return null;
  return {
    startInput: formatDateKeyForDisplay(firstDate),
    endInput: formatDateKeyForDisplay(lastDate),
  };
}

function selectedRangeDays(startInput, endInput) {
  const startKey = parseHistoryInputDate(startInput);
  const endKey = parseHistoryInputDate(endInput);
  if (!startKey || !endKey) return 0;
  return Math.abs(diffDateKeys(startKey, endKey)) + 1;
}

function scheduleCellTitle(day, hour, off) {
  if (!off) return `${day.day} ${pad(hour)}:00-${pad(hour + 1)}:00 світло є`;
  const matching = day.blocks.find((window) => window.end > hour && window.start < hour + 1);
  if (!matching) return `${day.day} ${pad(hour)}:00-${pad(hour + 1)}:00 можливе відключення`;
  return `${day.day} ${matching.startLabel}-${matching.endLabel} можливе відключення`;
}

function shouldShowAxisLabel(point, index, length) {
  if (length <= 14) return true;
  return point.showLabel || index === 0 || index === length - 1;
}

function shouldShowBoundaryDateLabel(point, index, length, showPrimaryLabel) {
  const isBoundary = index === 0 || index === length - 1;
  if (!isBoundary || !point.date) return false;
  return !(showPrimaryLabel && point.date === point.day);
}

function lineTooltipLabel(point) {
  if (point.timestamp_local) {
    const parts = localIsoParts(point.timestamp_local);
    if (parts) return `${formatDateKeyTooltip(parts.date)} ${formatPartsTime(parts)}`;
  }
  if (point.dateKey) return formatDateKeyTooltip(point.dateKey);
  return point.date ?? point.day ?? "";
}

function lineTooltipValue(value, unit) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return `0 ${unit}`;
  if (unit === "%") return `${Math.round(numeric)}%`;
  const formatted = numeric.toFixed(1).replace(/\.0$/, "");
  return `${formatted} ${unit}`;
}

function firstOutageQueue(outagesByDate) {
  return Object.values(outagesByDate).find((payload) => payload?.outage_queue)?.outage_queue;
}

function selectedClockParts(value, timezone) {
  const match = `${value ?? ""}`.match(
    /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/,
  );
  if (!match) return null;
  const hour = Number(match[2]);
  const minute = Number(match[3]);
  const second = Number(match[4] ?? 0);
  return {
    nowMs: Date.parse(`${match[1]}T${match[2]}:${match[3]}:${pad(second)}`),
    dateKey: match[1],
    timeLabel: `${pad(hour)}:${pad(minute)}`,
    hourFloat: hour + minute / 60 + second / 3600,
    timezone,
  };
}

function localIsoParts(value) {
  const match = `${value ?? ""}`.match(
    /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/,
  );
  if (!match) return null;
  return {
    date: match[1],
    hour: Number(match[2]),
    minute: Number(match[3]),
  };
}

function localDateKey(value) {
  return localIsoParts(value)?.date ?? null;
}

function formatPartsTime(parts) {
  return `${pad(parts.hour)}:${pad(parts.minute)}`;
}

function formatVoltage(value) {
  const voltage = nullableNumber(value, 230);
  return Math.round(voltage);
}

function cappedPercent(value) {
  return clamp(nullableNumber(value, 0), 0, 100);
}

function nullableNumber(value, fallback = null) {
  if (value === null || value === undefined || value === "") return fallback;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function formatFixed(value) {
  return Number(value).toFixed(1);
}

function reasonUk(reason) {
  const value = String(reason || "").toLowerCase();
  if (value.includes("generation") && value.includes("delivery")) {
    return "комбіноване обмеження генерації та доставки";
  }
  if (value.includes("generation")) return "обмеження генерації";
  if (value.includes("delivery")) return "обмеження доставки";
  if (value.includes("recovery")) return "відновлення після пошкоджень";
  if (value.includes("no active")) return "активних пошкоджень немає";
  return "обмеження енергосистеми";
}

function integrityFillClass(value) {
  if (value >= 99.5) return "green";
  if (value >= 80) return "yellow";
  if (value >= 60) return "orange";
  return "red";
}

function integrityColor(value) {
  if (value === "green") return "#cfeecf";
  if (value === "yellow") return "#fff4bf";
  if (value === "orange") return "#f8c995";
  return "#e9a4a8";
}

function startOfWeekKey(dateKey) {
  const day = dateKeyToUtc(dateKey);
  if (!day) return dateKey;
  const mondayOffset = (day.getUTCDay() + 6) % 7;
  return addDaysToDateKey(dateKey, -mondayOffset);
}

function datesBetween(startKey, endKey) {
  const dates = [];
  let current = startKey;
  while (current <= endKey) {
    dates.push(current);
    current = addDaysToDateKey(current, 1);
  }
  return dates;
}

function addDaysToDateKey(key, days) {
  const date = dateKeyToUtc(key);
  if (!date) return key;
  date.setUTCDate(date.getUTCDate() + days);
  return [
    date.getUTCFullYear(),
    `${date.getUTCMonth() + 1}`.padStart(2, "0"),
    `${date.getUTCDate()}`.padStart(2, "0"),
  ].join("-");
}

function diffDateKeys(startKey, endKey) {
  const start = dateKeyToUtc(startKey);
  const end = dateKeyToUtc(endKey);
  if (!start || !end) return 0;
  return Math.round((end.getTime() - start.getTime()) / 86400000);
}

function dateKeyToUtc(key) {
  if (!isDateKey(key)) return null;
  const [year, month, day] = key.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day));
}

function isDateKey(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function isValidDateKey(value) {
  if (!isDateKey(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

function formatShortDate(dateKey) {
  if (!isDateKey(dateKey)) return "";
  const [, month, day] = dateKey.split("-");
  return `${day}.${month}`;
}

function formatDateKeyForDisplay(dateKey) {
  if (!isValidDateKey(dateKey)) return "";
  const [year, month, day] = dateKey.split("-");
  return `${day}.${month}.${year}`;
}

function parseDisplayDate(value) {
  const match = String(value ?? "").trim().match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
  if (!match) return null;
  const [, day, month, year] = match;
  const dateKey = `${year}-${month}-${day}`;
  return isValidDateKey(dateKey) ? dateKey : null;
}

function parseHistoryInputDate(value) {
  const trimmed = String(value ?? "").trim();
  if (isValidDateKey(trimmed)) return trimmed;
  return parseDisplayDate(trimmed);
}

function formatDateKeyTooltip(dateKey) {
  if (!isDateKey(dateKey)) return dateKey;
  const [year, month, day] = dateKey.split("-");
  return `${day}.${month}.${year}`;
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function pad(value) {
  return String(Math.floor(value)).padStart(2, "0");
}
