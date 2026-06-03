import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { loadMockData } from "../../data/dashboardMockData";

export default function LoadModule({ data = loadMockData }) {
  const powerData = useMemo(
    () =>
      data.powerHistory.map((point) => ({
        ...point,
        time: Date.parse(point.timestamp),
      })),
    [data.powerHistory],
  );

  return (
    <section className="system-card load-card" aria-label="Навантаження">
      <header className="system-card-head">
        <h2 className="system-head-title">Навантаження</h2>
        <div className="system-head-divider" aria-hidden="true" />
        <div className="system-head-value">{data.currentPowerW} W</div>
      </header>

      <div className="system-card-body load-body">
        <div className="load-stats-grid">
          <LoadMetric label="Поточна потужність" value={data.currentPowerW} unit="W" />
          <LoadMetric label="За день" value={data.dailyEnergyKwh.toFixed(2)} unit="kWh" />
          <div className="load-stat-card">
            <div className="load-stat-label">Сонце / економія</div>
            <div className="load-stat-value load-money-value">
              {data.solarCoveredPercent}% / {formatMoney(data.moneySavedUah)}
              <span className="load-currency-unit">₴</span>
            </div>
          </div>
          <LoadMetric label="За місяць" value={data.monthlyEnergyKwh.toFixed(1)} unit="kWh" />
        </div>

        <LoadChartCard title="Споживана потужність за 24 год" unit="W">
          <LoadPowerChart data={powerData} timezone={data.timezone} />
        </LoadChartCard>

        <LoadChartCard title="Загальне споживання за місяць" unit="Wh">
          <LoadMonthlyChart data={data.monthlyEnergyHistory} />
        </LoadChartCard>
      </div>
    </section>
  );
}

function LoadMetric({ label, value, unit }) {
  return (
    <div className="load-stat-card">
      <div className="load-stat-label">{label}</div>
      <div className="load-stat-value">
        {value} <span className="load-stat-unit">{unit}</span>
      </div>
    </div>
  );
}

function LoadChartCard({ title, unit, children }) {
  return (
    <div className="load-chart-card">
      <div className="load-chart-head">
        <div className="load-chart-title">{title}</div>
        <div className="load-chart-meta">{unit}</div>
      </div>
      <div className="load-chart-wrap">{children}</div>
    </div>
  );
}

function LoadPowerChart({ data, timezone }) {
  if (data.length === 0) {
    return <div className="compact-empty-chart">Немає даних навантаження</div>;
  }

  const maxValue = Math.max(...data.map((point) => point.w), 1000);
  const ticks = buildTimeTicks(data);

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="rgba(0,0,0,0.08)" vertical={false} />
        <XAxis
          dataKey="time"
          type="number"
          domain={["dataMin", "dataMax"]}
          ticks={ticks}
          tickFormatter={(value) => formatTimeTick(value, timezone)}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
          minTickGap={8}
          height={22}
        />
        <YAxis
          width={36}
          domain={[0, Math.ceil(maxValue * 1.12)]}
          tickFormatter={(value) => `${Math.round(value)}`}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          formatter={(value) => [`${Math.round(Number(value))} W`, "Потужність"]}
          labelFormatter={(value) => formatDateTime(value, timezone)}
          cursor={{ stroke: "rgba(0,0,0,0.28)", strokeDasharray: "4 4" }}
          contentStyle={tooltipStyle}
          itemStyle={tooltipItemStyle}
          labelStyle={tooltipLabelStyle}
        />
        <Line
          type="monotone"
          dataKey="w"
          stroke="#e45460"
          strokeWidth={2.2}
          dot={false}
          activeDot={{ r: 4, fill: "#e45460", stroke: "#ffffff", strokeWidth: 1.7 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

function LoadMonthlyChart({ data }) {
  if (data.length === 0) {
    return <div className="compact-empty-chart">Немає місячних даних</div>;
  }

  const maxValue = Math.max(...data.map((point) => point.wh), 6000);
  const ticks = [data[0].date, data[Math.floor(data.length / 2)].date, data.at(-1).date];

  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }} barCategoryGap={3}>
        <CartesianGrid stroke="rgba(0,0,0,0.08)" vertical={false} />
        <XAxis
          dataKey="date"
          ticks={ticks}
          tickFormatter={formatDateLabel}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
          height={22}
        />
        <YAxis
          width={38}
          domain={[0, Math.ceil(maxValue * 1.12)]}
          tickFormatter={(value) => `${Math.round(value)}`}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          formatter={(value) => [`${Math.round(Number(value))} Wh`, "Споживання"]}
          labelFormatter={(value) => formatDateLabel(value)}
          cursor={{ fill: "rgba(0,0,0,0.06)" }}
          contentStyle={tooltipStyle}
          itemStyle={tooltipItemStyle}
          labelStyle={tooltipLabelStyle}
        />
        <Bar dataKey="wh" fill="#e45460" radius={[3, 3, 0, 0]} isAnimationActive={false} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function buildTimeTicks(data) {
  if (data.length < 2) return undefined;
  return [data[0].time, data[Math.floor(data.length / 2)].time, data.at(-1).time];
}

function formatTimeTick(value, timezone) {
  return new Date(value).toLocaleTimeString("uk-UA", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDateTime(value, timezone) {
  return new Date(value).toLocaleString("uk-UA", {
    timeZone: timezone,
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDateLabel(value) {
  const [, , month, day] = `${value}`.match(/^(\d{4})-(\d{2})-(\d{2})$/) ?? [];
  return month && day ? `${day}.${month}` : value;
}

function formatMoney(value) {
  const fixed = Number(value).toFixed(2);
  return fixed.startsWith("-") ? `−${fixed.slice(1)}` : fixed;
}

const tooltipStyle = {
  border: "1px solid #b8b8af",
  borderRadius: 5,
  padding: "2px 5px",
  fontFamily: "Jura, system-ui, sans-serif",
  fontSize: 9,
  fontWeight: 800,
  lineHeight: 1.05,
  maxWidth: 112,
};

const tooltipItemStyle = {
  padding: 0,
  fontSize: 9,
  lineHeight: 1.05,
};

const tooltipLabelStyle = {
  marginBottom: 1,
  fontSize: 9,
  lineHeight: 1.05,
};
