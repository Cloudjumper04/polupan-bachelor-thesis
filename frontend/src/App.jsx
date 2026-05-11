import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ChevronDown,
  Cloud,
  CloudFog,
  CloudRain,
  CloudSnow,
  CloudSun,
  Expand,
  History,
  Loader2,
  Moon,
  Sunrise,
  Sunset,
  Sun,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const CHARTS = [
  ["last30m", "Останні 30 хвилин"],
  ["last3h", "Останні 3 години"],
  ["last12h", "Останні 12 годин"],
  ["last24h", "Останні 24 години"],
  ["last7d", "Останні 7 днів"],
];

const POLL_INTERVAL_MS = 5000;
const CURRENT_BUFFER_FETCH_INTERVAL_MS = 30000;
const CURRENT_DISPLAY_INTERVAL_MS = 1000;
const CURRENT_BUFFER_SECONDS = 75;
const WEATHER_REFRESH_AFTER_HOUR_MS = 5000;
const DEFAULT_HISTORY_DAYS = 7;
const MAX_POWER_HISTORY_DAYS = 31;

export default function App() {
  const [dashboard, setDashboard] = useState(null);
  const [weatherData, setWeatherData] = useState(null);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyInputStart, setHistoryInputStart] = useState("");
  const [historyInputEnd, setHistoryInputEnd] = useState("");
  const [historyBounds, setHistoryBounds] = useState({
    loaded: false,
    powerStartKey: "",
    powerEndKey: "",
    dailyStartKey: "",
    dailyEndKey: "",
  });
  const [historyClampMessage, setHistoryClampMessage] = useState("");
  const [activeHistoryMode, setActiveHistoryMode] = useState("power");
  const [historyDataByMode, setHistoryDataByMode] = useState({
    power: null,
    daily_energy: null,
  });
  const [historyAppliedRanges, setHistoryAppliedRanges] = useState({
    power: null,
    daily_energy: null,
  });
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const scrollRef = useRef(null);
  const initialScrollDone = useRef(false);
  const historyInputsInitialized = useRef(false);
  const firstHistoryLoadDone = useRef(false);
  const lastFetchedHistoryKey = useRef("");
  const historyFetchController = useRef(null);

  useEffect(() => {
    const controller = new AbortController();

    async function loadDashboard() {
      try {
        const response = await fetch("/api/solar/dashboard", {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`API ${response.status}`);
        }
        const payload = await response.json();
        setDashboard(payload);
        setStatus("ready");
        setError("");
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setStatus("error");
        setError("Не вдалося отримати дані з API");
      }
    }

    loadDashboard();
    const timer = window.setInterval(loadDashboard, POLL_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer = null;

    async function loadWeather() {
      try {
        const response = await fetch("/api/solar/weather-current", {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`API ${response.status}`);
        }
        setWeatherData(await response.json());
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
      }
    }

    function scheduleHourlyRefresh() {
      timer = window.setTimeout(async () => {
        await loadWeather();
        scheduleHourlyRefresh();
      }, msUntilNextFullHour() + WEATHER_REFRESH_AFTER_HOUR_MS);
    }

    loadWeather();
    scheduleHourlyRefresh();
    return () => {
      controller.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    if (!dashboard || initialScrollDone.current || !scrollRef.current) return;
    const target = scrollRef.current.querySelector('[data-chart-card="last3h"]');
    if (!target) return;
    scrollRef.current.scrollTop = target.offsetTop - scrollRef.current.offsetTop;
    initialScrollDone.current = true;
  }, [dashboard]);

  const stationTimezone = dashboard?.station?.timezone;

  useEffect(() => {
    const controller = new AbortController();

    async function loadHistoryBounds() {
      try {
        const response = await fetch("/api/solar/history/bounds", {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`API ${response.status}`);
        }
        const payload = await response.json();
        setHistoryBounds(normalizeHistoryBoundsPayload(payload));
        setHistoryError("");
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
        setHistoryError("Не вдалося отримати межі доступної історії");
      }
    }

    loadHistoryBounds();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (historyInputsInitialized.current || !historyBounds.loaded) return;
    const initialRange = defaultPowerInputRange(historyBounds);
    if (!initialRange) return;
    setHistoryInputStart(formatDateInput(initialRange.startDate));
    setHistoryInputEnd(formatDateInput(initialRange.endDate));
    historyInputsInitialized.current = true;
  }, [historyBounds]);

  useEffect(() => {
    return () => {
      historyFetchController.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (firstHistoryLoadDone.current) return;
    if (!historyInputStart || !historyInputEnd || !historyBounds.loaded) return;
    firstHistoryLoadDone.current = true;
    refreshHistory({ force: false });
  }, [historyBounds.loaded, historyInputStart, historyInputEnd]);

  const current = dashboard?.current;
  const weather = weatherData;
  const chartData = dashboard?.charts ?? {};
  const historyData = historyDataByMode[activeHistoryMode];
  const activeAppliedRange = historyAppliedRanges[activeHistoryMode];
  const voltage = current?.voltage_v ?? 29.1;
  const currentA = current?.current_a ?? 0.1;

  function updateHistoryField(field, value) {
    setHistoryClampMessage("");
    setHistoryError("");
    if (field === "from") {
      setHistoryInputStart(value);
    } else {
      setHistoryInputEnd(value);
    }
  }

  function normalizeVisibleHistoryRange(mode, inputStart, inputEnd) {
    const parsedStart = parseHistoryDateInput(inputStart);
    const parsedEnd = parseHistoryDateInput(inputEnd);
    if (!parsedStart || !parsedEnd) {
      return {
        error: "Вкажіть дати у форматі ДД.ММ.РРРР",
      };
    }

    const modeBounds = getHistoryModeBounds(historyBounds, mode);
    if (!hasHistoryBounds(modeBounds)) {
      return {
        error: "Немає доступних даних для вибраного режиму",
      };
    }

    const normalized =
      mode === "daily_energy"
        ? normalizeDailyRange(parsedStart.key, parsedEnd.key, modeBounds)
        : normalizePowerRange(parsedStart.key, parsedEnd.key, modeBounds);
    if (!normalized) {
      return {
        error: "Немає доступних даних для вибраного режиму",
      };
    }

    return {
      ...normalized,
      wasAdjusted:
        normalized.wasAdjusted || parsedStart.normalized || parsedEnd.normalized,
    };
  }

  async function refreshHistory({ force = true } = {}) {
    if (!historyBounds.loaded) {
      setHistoryError("Дані про доступний період ще не завантажені");
      return;
    }

    const activeNormalized = normalizeVisibleHistoryRange(
      activeHistoryMode,
      historyInputStart,
      historyInputEnd,
    );
    if (activeNormalized.error) {
      setHistoryError(activeNormalized.error);
      setHistoryClampMessage("");
      return;
    }

    const nextInputStart = formatDateInput(activeNormalized.startDate);
    const nextInputEnd = formatDateInput(activeNormalized.endDate);
    setHistoryInputStart(nextInputStart);
    setHistoryInputEnd(nextInputEnd);

    const powerNormalized = normalizePowerRange(
      activeNormalized.startDate,
      activeNormalized.endDate,
      getHistoryModeBounds(historyBounds, "power"),
    );
    const dailyNormalized = normalizeDailyRange(
      activeNormalized.startDate,
      activeNormalized.endDate,
      getHistoryModeBounds(historyBounds, "daily_energy"),
    );
    const powerUrl = powerNormalized
      ? buildPowerHistoryUrl(powerNormalized.startDate, powerNormalized.endDate)
      : null;
    const dailyUrl = dailyNormalized
      ? buildDailyHistoryUrl(dailyNormalized.startDate, dailyNormalized.endDate)
      : null;
    const queryKey = `${activeHistoryMode}|${powerUrl ?? ""}|${dailyUrl ?? ""}`;
    if (
      !force &&
      lastFetchedHistoryKey.current === queryKey &&
      (!powerUrl || historyDataByMode.power) &&
      (!dailyUrl || historyDataByMode.daily_energy)
    ) {
      return;
    }

    historyFetchController.current?.abort();
    const controller = new AbortController();
    historyFetchController.current = controller;
    setHistoryLoading(true);

    try {
      const [powerPayload, dailyPayload] = await Promise.all([
        powerUrl ? fetchHistoryPayload(powerUrl, controller.signal) : Promise.resolve(null),
        dailyUrl ? fetchHistoryPayload(dailyUrl, controller.signal) : Promise.resolve(null),
      ]);
      setHistoryDataByMode({
        power: powerPayload,
        daily_energy: dailyPayload,
      });
      setHistoryAppliedRanges({
        power: powerNormalized?.range ?? null,
        daily_energy: dailyNormalized?.range ?? null,
      });
      setHistoryError("");
      lastFetchedHistoryKey.current = queryKey;
      if (
        activeNormalized.wasClamped ||
        activeNormalized.wasAdjusted ||
        (activeHistoryMode === "power" &&
          (powerPayload?.metadata?.clamped_start || powerPayload?.metadata?.clamped_end)) ||
        (activeHistoryMode === "daily_energy" &&
          (dailyPayload?.metadata?.clamped_start || dailyPayload?.metadata?.clamped_end))
      ) {
        setHistoryClampMessage(
          "Діапазон обмежено доступними даними або максимальною тривалістю.",
        );
      } else {
        setHistoryClampMessage("");
      }
    } catch (loadError) {
      if (loadError.name === "AbortError") return;
      setHistoryError("Не вдалося отримати історію генерації");
    } finally {
      if (historyFetchController.current === controller) {
        setHistoryLoading(false);
      }
    }
  }

  async function fetchHistoryPayload(url, signal) {
    const response = await fetch(url, {
      signal,
    });
    if (!response.ok) {
      throw new Error(`API ${response.status}`);
    }
    return response.json();
  }

  function applyHistoryUpdate() {
    refreshHistory({ force: true });
  }

  function changeHistoryMode(nextMode) {
    if (nextMode === activeHistoryMode) return;
    const appliedRange = historyAppliedRanges[nextMode];
    if (appliedRange) {
      setHistoryInputStart(formatDateInput(appliedRange.startDate));
      setHistoryInputEnd(formatDateInput(appliedRange.endDate));
    }
    setActiveHistoryMode(nextMode);
    setHistoryClampMessage("");
    setHistoryError("");
  }

  return (
    <>
      <header className="site-header">
        <div className="header-inner">
          <div className="brand">
            <div className="brand-title">SmartEnergy Lab</div>
            <div className="brand-subtitle">
              Програмний модуль автоматизації режимів зарядки акумуляторних
              батарей
            </div>
          </div>

          <div className="header-widgets" aria-label="Віджет прогнозу погоди">
            <WeatherWidget weather={weather} />
          </div>
        </div>
      </header>

      <main className="page">
        <section className="main-layout">
          <section className={`solar-card${expanded ? " expanded" : ""}`}>
            <button
              className="expand-card-button"
              type="button"
              aria-label={expanded ? "Згорнути блок графіків" : "Розгорнути блок графіків"}
              aria-expanded={expanded}
              onClick={() => setExpanded((value) => !value)}
            >
              <Expand aria-hidden="true" />
            </button>

            <header className="solar-card-head">
              <div className="solar-title">
                <div className="solar-title-bubble">
                  <h1>
                    Сонячна <br />
                    генерація
                  </h1>
                </div>
              </div>
              <div className="title-separator" aria-hidden="true" />
              <div className="power-summary-wrap">
                <div className="power-summary">
                  <div className="power-total">
                    <div className="metric-label">Загальна потужність</div>
                    <div className="power-value">
                      <CurrentPowerValue fallbackPower={current?.solar_power_w} />{" "}
                      <span className="unit">W</span>
                    </div>
                  </div>
                  <div className="sub-metrics">
                    <div className="sub-metric">
                      <div className="metric-label">Напруга</div>
                      <div className="value">
                        {formatFixed(voltage)} <span className="unit">V</span>
                      </div>
                    </div>
                    <div className="sub-metric">
                      <div className="metric-label">Струм</div>
                      <div className="value">
                        {formatFixed(currentA)} <span className="unit">A</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </header>

            <section className="chart-box">
              <div className="charts-scroll" ref={scrollRef}>
                {status === "loading" && (
                  <StatusMessage icon="loading" text="Завантаження даних..." />
                )}
                {status === "error" && <StatusMessage icon="error" text={error} />}
                {status === "ready" &&
                  CHARTS.map(([chartId, title]) => (
                    <ChartCard
                      key={chartId}
                      chartId={chartId}
                      title={title}
                      chart={chartData[chartId]}
                      timezone={stationTimezone}
                    />
                  ))}
              </div>
            </section>

            <footer className="solar-card-foot">
              <button
                className={`history-link${historyOpen ? " expanded" : ""}`}
                type="button"
                aria-expanded={historyOpen}
                onClick={() => setHistoryOpen((value) => !value)}
              >
                історія генерації
                <ChevronDown aria-hidden="true" />
              </button>
            </footer>
          </section>

          <section className={`history-panel${historyOpen ? " active" : ""}`}>
            <div className="history-head">
              <h2>
                <History aria-hidden="true" />
                Історія генерації
              </h2>
            </div>
            <div className="history-mode-switch" role="tablist" aria-label="Режим історії">
              <button
                type="button"
                className={activeHistoryMode === "power" ? "active" : ""}
                aria-selected={activeHistoryMode === "power"}
                onClick={() => changeHistoryMode("power")}
              >
                Графік генерації
              </button>
              <button
                type="button"
                className={activeHistoryMode === "daily_energy" ? "active" : ""}
                aria-selected={activeHistoryMode === "daily_energy"}
                onClick={() => changeHistoryMode("daily_energy")}
              >
                Добова генерація
              </button>
            </div>
            <div className="history-toolbar">
              <label className="field">
                <span>Від</span>
                <input
                  type="text"
                  inputMode="numeric"
                  placeholder="дд.мм.рррр"
                  value={historyInputStart}
                  onChange={(event) => updateHistoryField("from", event.target.value)}
                />
              </label>
              <label className="field">
                <span>До</span>
                <input
                  type="text"
                  inputMode="numeric"
                  placeholder="дд.мм.рррр"
                  value={historyInputEnd}
                  onChange={(event) => updateHistoryField("to", event.target.value)}
                />
              </label>
              <button
                className="history-apply-button"
                type="button"
                disabled={historyLoading}
                onClick={applyHistoryUpdate}
              >
                оновити графік
              </button>
            </div>
            {historyClampMessage && (
              <div className="history-clamp-message">{historyClampMessage}</div>
            )}
            <div className="history-chart-wrap">
              {historyLoading && !historyData && (
                <StatusMessage icon="loading" text="Завантаження історії..." />
              )}
              {historyError && !historyData && (
                <StatusMessage icon="error" text={historyError} />
              )}
              {!historyLoading && !historyError && !historyData && (
                <div className="empty-chart">Оберіть діапазон для історії</div>
              )}
              {historyData && (historyData?.points ?? []).length === 0 && (
                <div className="empty-chart">Немає даних для вибраного діапазону</div>
              )}
              {historyData &&
                (historyData?.points ?? []).length > 0 &&
                activeHistoryMode === "power" && (
                <div className="history-chart-scroll">
                  <div className="history-chart-canvas">
                    <SolarChart
                      points={historyData?.points ?? []}
                      timezone={stationTimezone}
                      axisMode="date"
                    />
                  </div>
                </div>
              )}
              {historyData &&
                (historyData?.points ?? []).length > 0 &&
                activeHistoryMode === "daily_energy" && (
                <div className="history-chart-scroll">
                  <div className="history-chart-canvas">
                    <DailyEnergyChart
                      points={historyData?.points ?? []}
                      range={activeAppliedRange}
                    />
                  </div>
                </div>
              )}
              {historyLoading && historyData && (
                <div className="history-refreshing">Оновлення...</div>
              )}
            </div>
          </section>
        </section>
      </main>

      <footer className="site-footer">
        <div className="footer-inner" aria-hidden="true" />
      </footer>
    </>
  );
}

function WeatherWidget({ weather }) {
  const Icon = weatherIcon(weather?.weather_code, weather?.weather_state);
  const label = weather?.weather_label ?? "невідомо";
  const cloudText =
    weather?.cloud_cover_percent === null || weather?.cloud_cover_percent === undefined
      ? "Хмарність: —"
      : `Хмарність: ${Math.round(weather.cloud_cover_percent)}%`;
  const tempText =
    weather?.temperature_c === null || weather?.temperature_c === undefined
      ? "Температура: —"
      : `Температура: ${Math.round(weather.temperature_c)}°C`;
  const sunriseText = `Схід: ${formatSunTime(weather?.sunrise_local ?? weather?.sunrise)}`;
  const sunsetText = `Захід: ${formatSunTime(weather?.sunset_local ?? weather?.sunset)}`;

  return (
    <div
      className="weather-widget"
      title={`Погода: ${label}. ${cloudText}. ${tempText}. ${sunriseText}. ${sunsetText}.`}
    >
      <div className="weather-icon" aria-hidden="true">
        <Icon />
      </div>
      <div className="weather-main">
        <div className="clouds">{cloudText}</div>
        <div className="temp">{tempText}</div>
      </div>
      <div className="sun-times" aria-label="Схід і захід сонця">
        <span className="sun-time">
          <Sunrise aria-hidden="true" />
          {sunriseText}
        </span>
        <span className="sun-time">
          <Sunset aria-hidden="true" />
          {sunsetText}
        </span>
      </div>
    </div>
  );
}

function CurrentPowerValue({ fallbackPower }) {
  const [displayPower, setDisplayPower] = useState(fallbackPower);
  const bufferRef = useRef([]);
  const fallbackPowerRef = useRef(fallbackPower);

  useEffect(() => {
    fallbackPowerRef.current = fallbackPower;
    setDisplayPower((currentPower) => currentPower ?? fallbackPower);
  }, [fallbackPower]);

  useEffect(() => {
    const controller = new AbortController();

    async function loadCurrentBuffer() {
      try {
        const response = await fetch(
          `/api/solar/current-buffer?seconds=${CURRENT_BUFFER_SECONDS}`,
          { signal: controller.signal },
        );
        if (!response.ok) {
          throw new Error(`API ${response.status}`);
        }
        const payload = await response.json();
        const points = normalizeCurrentBufferPoints(payload?.points ?? []);
        bufferRef.current = points;
        const bufferedPower = selectBufferedPower(points, Date.now());
        const nextPower =
          bufferedPower ?? payload?.current?.solar_power_w ?? fallbackPowerRef.current;
        setDisplayPower((currentPower) => nextPower ?? currentPower);
      } catch (loadError) {
        if (loadError.name === "AbortError") return;
      }
    }

    loadCurrentBuffer();
    const timer = window.setInterval(
      loadCurrentBuffer,
      CURRENT_BUFFER_FETCH_INTERVAL_MS,
    );
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const bufferedPower = selectBufferedPower(bufferRef.current, Date.now());
      if (bufferedPower !== null && bufferedPower !== undefined) {
        setDisplayPower(bufferedPower);
      }
    }, CURRENT_DISPLAY_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, []);

  return <>{formatPower(displayPower ?? fallbackPower)}</>;
}

function ChartCard({ chartId, title, chart, timezone }) {
  const points = chartPoints(chart);
  const lastPoint = points.at(-1);
  return (
    <article className="chart-card" data-chart-card={chartId}>
      <div className="chart-head">
        <div className="chart-title">{title}</div>
        <div className="chart-meta">
          {lastPoint
            ? `${formatDateTime(lastPoint.timestamp_local, timezone)} · ${formatPower(lastPoint.power_w)} W`
            : "немає даних"}
        </div>
      </div>
      <div className="chart-wrap">
        <SolarChart points={points} timezone={timezone} />
      </div>
    </article>
  );
}

function SolarChart({ points, timezone, axisMode = "time" }) {
  const data = points.map((point) => ({
    time: Date.parse(point.timestamp_local),
    power_w: Number(point.power_w ?? 0),
  }));

  if (data.length < 2) {
    return (
      <div className="empty-chart">
        {data.length === 0 ? "Немає даних для відображення" : "Недостатньо даних"}
      </div>
    );
  }

  const powers = data.map((point) => point.power_w);
  const rawMin = Math.min(...powers);
  const rawMax = Math.max(...powers);
  const span = Math.max(1, rawMax - rawMin);
  const yMin = rawMax <= 5 ? 0 : Math.max(0, rawMin - span * 0.18);
  const yMax = Math.max(yMin + 1, rawMax + span * 0.18);
  const last = data.at(-1);
  const dateTicks = axisMode === "date" ? buildPowerHistoryDateTicks(data, timezone) : undefined;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 10, right: 24, left: 8, bottom: 8 }}>
        <CartesianGrid stroke="rgba(0,0,0,0.13)" vertical={false} />
        <XAxis
          dataKey="time"
          type="number"
          domain={["dataMin", "dataMax"]}
          ticks={dateTicks}
          tickFormatter={(value) =>
            axisMode === "date"
              ? formatAxisDate(value, timezone)
              : formatAxisTime(value, timezone)
          }
          tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
          axisLine={false}
          tickLine={false}
          minTickGap={axisMode === "date" ? 18 : 10}
          height={axisMode === "date" ? 34 : 28}
        />
        <YAxis
          width={68}
          domain={[yMin, yMax]}
          tickFormatter={formatPowerAxis}
          tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          formatter={(value) => [`${formatPower(value)} W`, "Потужність"]}
          labelFormatter={(value) => formatDateTime(value, timezone)}
          contentStyle={{
            border: "1px solid #b8b8af",
            borderRadius: 8,
            fontFamily: "Jura, system-ui, sans-serif",
            fontWeight: 700,
          }}
        />
        {last && (
          <ReferenceLine
            x={last.time}
            stroke="rgba(0,0,0,0.58)"
            strokeDasharray="4 4"
          />
        )}
        <Area
          type="monotone"
          dataKey="power_w"
          stroke="#050505"
          strokeWidth={2.3}
          fill="rgba(255, 221, 85, 0.42)"
          dot={false}
          activeDot={{ r: 4, fill: "#ffdd55", stroke: "#050505", strokeWidth: 1.7 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function DailyEnergyChart({ points, range }) {
  const data = points
    .filter(
      (point) =>
        point.weather_adjusted_daily_energy_kwh !== null &&
        point.weather_adjusted_daily_energy_kwh !== undefined,
    )
    .map((point) => ({
      date: point.date_local,
      adjusted: Number(point.weather_adjusted_daily_energy_kwh),
    }))
    .filter((point) => !Number.isNaN(point.adjusted));

  if (data.length === 0) {
    return <div className="empty-chart">Немає даних для вибраного діапазону</div>;
  }

  const values = data.map((point) => point.adjusted);
  const maxValue = Math.max(1, ...values);
  const dateTicks = buildDailyDateTicks(data);
  const includeYear = data[0]?.date?.slice(0, 4) !== data.at(-1)?.date?.slice(0, 4);
  const rangeStart = range?.startDate ?? data[0]?.date;
  const rangeEnd = range?.endDate ?? data.at(-1)?.date;
  const selectedDays = Math.max(1, diffDateKeys(rangeStart, rangeEnd) + 1);

  if (selectedDays < 14) {
    return (
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ top: 14, right: 24, left: 14, bottom: 8 }}
          barCategoryGap={0}
          barGap={0}
        >
          <CartesianGrid stroke="rgba(0,0,0,0.13)" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={(value) => formatDateLabel(value, includeYear)}
            tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
            axisLine={false}
            tickLine={false}
            minTickGap={8}
            height={34}
          />
          <YAxis
            width={82}
            domain={[0, maxValue * 1.18]}
            tickFormatter={(value) => `${Number(value).toFixed(1)}`}
            tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
            axisLine={false}
            tickLine={false}
            label={{
              value: "кВт·год/день",
              angle: -90,
              position: "insideLeft",
              fill: "#575757",
              fontSize: 11,
              fontWeight: 800,
            }}
          />
          <Tooltip
            formatter={(value) => [
              `${Number(value).toFixed(2)} кВт·год/день`,
              "Добова генерація",
            ]}
            labelFormatter={(value) => formatDateLabel(value, true)}
            itemStyle={{ color: "#050505" }}
            labelStyle={{ color: "#050505" }}
            contentStyle={{
              border: "1px solid #b8b8af",
              borderRadius: 8,
              fontFamily: "Jura, system-ui, sans-serif",
              fontWeight: 700,
            }}
          />
          <Bar
            dataKey="adjusted"
            fill="#ffdd55"
            radius={0}
            isAnimationActive={false}
          />
        </BarChart>
      </ResponsiveContainer>
    );
  }

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 14, right: 24, left: 14, bottom: 8 }}>
        <CartesianGrid stroke="rgba(0,0,0,0.13)" vertical={false} />
        <XAxis
          dataKey="date"
          ticks={dateTicks}
          tickFormatter={(value) => formatDateLabel(value, includeYear)}
          tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
          axisLine={false}
          tickLine={false}
          minTickGap={18}
          height={34}
        />
        <YAxis
          width={82}
          domain={[0, maxValue * 1.18]}
          tickFormatter={(value) => `${Number(value).toFixed(1)}`}
          tick={{ fill: "#575757", fontSize: 12, fontWeight: 700 }}
          axisLine={false}
          tickLine={false}
          label={{
            value: "кВт·год/день",
            angle: -90,
            position: "insideLeft",
            fill: "#575757",
            fontSize: 11,
            fontWeight: 800,
          }}
        />
        <Tooltip
          formatter={(value) => [
            `${Number(value).toFixed(2)} кВт·год/день`,
            "Добова генерація",
          ]}
          labelFormatter={(value) => formatDateLabel(value, true)}
          itemStyle={{ color: "#050505" }}
          labelStyle={{ color: "#050505" }}
          contentStyle={{
            border: "1px solid #b8b8af",
            borderRadius: 8,
            fontFamily: "Jura, system-ui, sans-serif",
            fontWeight: 700,
          }}
        />
        <Line
          type="monotone"
          dataKey="adjusted"
          stroke="#050505"
          strokeWidth={2.4}
          dot={false}
          activeDot={{ r: 4, fill: "#ffdd55", stroke: "#050505", strokeWidth: 1.7 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

function StatusMessage({ icon, text }) {
  const Icon = icon === "loading" ? Loader2 : AlertCircle;
  return (
    <div className={`status-message ${icon}`}>
      <Icon aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}

function chartPoints(chart) {
  if (Array.isArray(chart)) return chart;
  return chart?.points ?? [];
}

function normalizeCurrentBufferPoints(points) {
  return points
    .map((point) => {
      const timestamp = Date.parse(point.timestamp_utc ?? point.timestamp_local);
      return {
        timestamp,
        power: Number(point.solar_power_w),
      };
    })
    .filter(
      (point) => !Number.isNaN(point.timestamp) && !Number.isNaN(point.power),
    )
    .sort((left, right) => left.timestamp - right.timestamp);
}

function selectBufferedPower(points, nowMs) {
  let selected = null;
  for (const point of points) {
    if (point.timestamp > nowMs) break;
    selected = point;
  }
  return selected?.power ?? null;
}

function weatherIcon(code, state) {
  if (state === "rain" || state === "drizzle" || state === "thunderstorm") {
    return CloudRain;
  }
  if (state === "snow") return CloudSnow;
  if (state === "fog") return CloudFog;
  if (code === 0) return Sun;
  if (code === 1 || code === 2) return CloudSun;
  if (code === 3) return Cloud;
  if (code === null || code === undefined) return Moon;
  return CloudSun;
}

function formatPower(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${Math.round(Number(value))}`;
}

function formatPowerAxis(value) {
  const numeric = Number(value);
  if (Number.isNaN(numeric)) return "";
  if (Math.abs(numeric) >= 1000) {
    return `${(numeric / 1000).toFixed(Math.abs(numeric) >= 10000 ? 0 : 1)} kW`;
  }
  return `${Math.round(numeric)} W`;
}

function formatFixed(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(1);
}

function formatDateTime(value, timezone) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value) : new Date(value);
  return date.toLocaleString("uk-UA", {
    timeZone: timezone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatAxisTime(value, timezone) {
  const date = new Date(value);
  return date.toLocaleString("uk-UA", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatAxisDate(value, timezone) {
  const date = new Date(value);
  return date.toLocaleDateString("uk-UA", {
    timeZone: timezone,
    day: "2-digit",
    month: "2-digit",
  });
}

function formatDateLabel(value, includeYear = false) {
  if (!value) return "—";
  const parts = splitDateKey(value);
  if (!parts) return value;
  const label = `${parts.day}.${parts.month}`;
  return includeYear ? `${label}.${parts.year}` : label;
}

function formatSunTime(value) {
  if (!value) return "—";
  const localTime = `${value}`.match(/T(\d{2}):(\d{2})/);
  if (localTime) {
    return `${localTime[1]}:${localTime[2]}`;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("uk-UA", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function msUntilNextFullHour(now = new Date()) {
  const nextHour = new Date(now);
  nextHour.setHours(now.getHours() + 1, 0, 0, 0);
  return Math.max(0, nextHour.getTime() - now.getTime());
}

function normalizeHistoryBoundsPayload(payload) {
  return {
    loaded: true,
    powerStartKey: localIsoToDateKey(payload?.power_start_local),
    powerEndKey: localIsoToDateKey(payload?.power_end_local),
    dailyStartKey: localIsoToDateKey(payload?.daily_start_local),
    dailyEndKey: localIsoToDateKey(payload?.daily_end_local),
  };
}

function localIsoToDateKey(value) {
  const match = `${value ?? ""}`.match(/^(\d{4}-\d{2}-\d{2})/);
  return match?.[1] ?? "";
}

function defaultPowerInputRange(bounds) {
  const powerBounds = getHistoryModeBounds(bounds, "power");
  if (!hasHistoryBounds(powerBounds)) return null;
  const endDate = powerBounds.endKey;
  const startDate = clampDateKey(
    addDaysToDateKey(endDate, -DEFAULT_HISTORY_DAYS),
    powerBounds.startKey,
    endDate,
  );
  return { startDate, endDate };
}

function getHistoryModeBounds(bounds, mode) {
  if (mode === "daily_energy") {
    return {
      startKey: bounds.dailyStartKey,
      endKey: bounds.dailyEndKey,
    };
  }
  return {
    startKey: bounds.powerStartKey,
    endKey: bounds.powerEndKey,
  };
}

function hasHistoryBounds(bounds) {
  return Boolean(bounds?.startKey && bounds?.endKey);
}

function parseUkrainianDateInput(value) {
  const match = `${value ?? ""}`.trim().match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
  if (!match) return null;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  if (month < 1 || month > 12) return null;
  return { year, month, day };
}

function parseHistoryDateInput(value) {
  const parsed = parseUkrainianDateInput(value);
  if (!parsed) return null;
  return normalizeInvalidDay(parsed.year, parsed.month, parsed.day);
}

function normalizeInvalidDay(year, month, day) {
  const normalizedDay = Math.min(Math.max(day, 1), daysInMonth(year, month));
  const key = [
    `${year}`.padStart(4, "0"),
    `${month}`.padStart(2, "0"),
    `${normalizedDay}`.padStart(2, "0"),
  ].join("-");
  return {
    key,
    normalized: normalizedDay !== day,
  };
}

function normalizePowerRange(startDate, endDate, bounds) {
  if (!hasHistoryBounds(bounds)) return null;
  let startKey = startDate;
  let endKey = endDate;
  let wasAdjusted = false;

  if (startKey > endKey) {
    [startKey, endKey] = [endKey, startKey];
    wasAdjusted = true;
  }

  const beforeClampStart = startKey;
  const beforeClampEnd = endKey;
  startKey = clampDateKey(startKey, bounds.startKey, bounds.endKey);
  endKey = clampDateKey(endKey, bounds.startKey, bounds.endKey);
  wasAdjusted ||= startKey !== beforeClampStart || endKey !== beforeClampEnd;

  if (startKey > endKey) {
    startKey = bounds.startKey;
    endKey = bounds.endKey;
    wasAdjusted = true;
  }

  if (diffDateKeys(startKey, endKey) > MAX_POWER_HISTORY_DAYS) {
    const preferredEndKey = addDaysToDateKey(startKey, MAX_POWER_HISTORY_DAYS);
    if (preferredEndKey <= bounds.endKey) {
      endKey = preferredEndKey;
    } else {
      endKey = bounds.endKey;
      startKey = clampDateKey(
        addDaysToDateKey(endKey, -MAX_POWER_HISTORY_DAYS),
        bounds.startKey,
        endKey,
      );
    }
    wasAdjusted = true;
  }

  if (diffDateKeys(startKey, endKey) < 1) {
    const expandedEndKey = addDaysToDateKey(startKey, 1);
    if (expandedEndKey <= bounds.endKey) {
      endKey = expandedEndKey;
    } else {
      endKey = bounds.endKey;
      startKey = clampDateKey(addDaysToDateKey(endKey, -1), bounds.startKey, endKey);
    }
    wasAdjusted = true;
  }

  return normalizedRangeResult(startKey, endKey, wasAdjusted);
}

function normalizeDailyRange(startDate, endDate, bounds) {
  if (!hasHistoryBounds(bounds)) return null;
  let startKey = startDate;
  let endKey = endDate;
  let wasAdjusted = false;

  if (startKey > endKey) {
    [startKey, endKey] = [endKey, startKey];
    wasAdjusted = true;
  }

  const beforeClampStart = startKey;
  const beforeClampEnd = endKey;
  startKey = clampDateKey(startKey, bounds.startKey, bounds.endKey);
  endKey = clampDateKey(endKey, bounds.startKey, bounds.endKey);
  wasAdjusted ||= startKey !== beforeClampStart || endKey !== beforeClampEnd;

  if (startKey > endKey) {
    startKey = bounds.startKey;
    endKey = bounds.endKey;
    wasAdjusted = true;
  }

  return normalizedRangeResult(startKey, endKey, wasAdjusted);
}

function normalizedRangeResult(startDate, endDate, wasAdjusted) {
  return {
    startDate,
    endDate,
    range: { startDate, endDate },
    wasAdjusted,
    wasClamped: wasAdjusted,
    message: wasAdjusted
      ? "Діапазон обмежено доступними даними або максимальною тривалістю."
      : "",
  };
}

function buildPowerHistoryUrl(startDate, endDate) {
  return buildHistoryUrl("/api/solar/history/power", startDate, endDate);
}

function buildDailyHistoryUrl(startDate, endDate) {
  return buildHistoryUrl("/api/solar/history/daily-energy", startDate, endDate);
}

function buildHistoryUrl(endpoint, startDate, endDate) {
  const params = new URLSearchParams({
    start: `${startDate}T00:00:00`,
    end: `${endDate}T23:59:59`,
  });
  return `${endpoint}?${params.toString()}`;
}

function buildPowerHistoryDateTicks(data, timezone) {
  if (data.length < 2) return undefined;
  const firstKey = formatDateKeyInTimezone(new Date(data[0].time), timezone);
  const lastKey = formatDateKeyInTimezone(new Date(data.at(-1).time), timezone);
  const spanDays = Math.max(0, diffDateKeys(firstKey, lastKey));
  const cadenceDays = spanDays < 14 ? 1 : 2;
  const firstPointByDate = new Map();

  for (const point of data) {
    const key = formatDateKeyInTimezone(new Date(point.time), timezone);
    if (!firstPointByDate.has(key)) {
      firstPointByDate.set(key, point.time);
    }
  }

  const ticks = [];
  for (const [key, value] of firstPointByDate) {
    if (diffDateKeys(firstKey, key) % cadenceDays === 0) {
      ticks.push(value);
    }
  }
  const lastTick = firstPointByDate.get(lastKey);
  if (lastTick && ticks.at(-1) !== lastTick) {
    ticks.push(lastTick);
  }
  return ticks;
}

function buildDailyDateTicks(data) {
  if (data.length < 2) return undefined;
  const firstKey = data[0].date;
  const lastKey = data.at(-1).date;
  const totalDays = Math.max(1, diffDateKeys(firstKey, lastKey) + 1);
  const cadenceDays =
    totalDays <= 14
      ? 1
      : totalDays <= 31
        ? Math.ceil(totalDays / 12)
        : Math.max(1, Math.round(totalDays / 12));
  const ticks = data
    .filter((point) => diffDateKeys(firstKey, point.date) % cadenceDays === 0)
    .map((point) => point.date);
  if (ticks.at(-1) !== lastKey) {
    ticks.push(lastKey);
  }
  return ticks;
}

function formatDateKeyInTimezone(value, timezone) {
  const parts = new Intl.DateTimeFormat("uk-UA", {
    timeZone: timezone,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).formatToParts(value);
  const part = (type) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

function formatDateInput(date) {
  return formatDateKeyForDisplay(date);
}

function formatDateKeyForDisplay(key) {
  const parts = splitDateKey(key);
  if (!parts) return "";
  return `${parts.day}.${parts.month}.${parts.year}`;
}

function addDaysToDateKey(key, days) {
  const parts = splitDateKey(key);
  if (!parts) return key;
  const date = new Date(
    Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)),
  );
  date.setUTCDate(date.getUTCDate() + days);
  return [
    date.getUTCFullYear(),
    `${date.getUTCMonth() + 1}`.padStart(2, "0"),
    `${date.getUTCDate()}`.padStart(2, "0"),
  ].join("-");
}

function daysInMonth(year, month) {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function clampDateKey(key, minKey, maxKey) {
  if (minKey && key < minKey) return minKey;
  if (maxKey && key > maxKey) return maxKey;
  return key;
}

function diffDateKeys(startKey, endKey) {
  const start = dateKeyToUtc(startKey);
  const end = dateKeyToUtc(endKey);
  if (!start || !end) return 0;
  return Math.round((end.getTime() - start.getTime()) / (24 * 60 * 60 * 1000));
}

function dateKeyToUtc(key) {
  const parts = splitDateKey(key);
  if (!parts) return null;
  return new Date(Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day)));
}

function splitDateKey(value) {
  const match = `${value}`.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return null;
  const [, year, month, day] = match;
  return { year, month, day };
}
