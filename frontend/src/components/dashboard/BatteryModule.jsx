import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { batteryMockData } from "../../data/dashboardMockData";

export default function BatteryModule({ data = batteryMockData }) {
  const [infoOpen, setInfoOpen] = useState(false);

  const chartData = useMemo(
    () =>
      data.energyHistory.map((point) => ({
        ...point,
        time: Date.parse(point.timestamp),
      })),
    [data.energyHistory],
  );

  return (
    <section className="system-card battery-card" aria-label="Батарея">
      <header className="system-card-head">
        <h2 className="system-head-title">Батарея</h2>
        <div className="system-head-divider" aria-hidden="true" />
        <div className="system-head-value">{Math.round(data.soc)}%</div>
      </header>

      <div className="system-card-body battery-body">
        <div className="battery-stats-grid">
          <button
            className="battery-info-toggle"
            type="button"
            aria-label="Показати паспорт батареї"
            aria-expanded={infoOpen}
            onClick={() => setInfoOpen((value) => !value)}
          >
            ‹
          </button>

          {infoOpen && (
            <div className="battery-info-popover" role="dialog" aria-label="Паспорт батареї">
              {data.info.map((item) => (
                <div className="battery-info-row" key={item.label}>
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                </div>
              ))}
            </div>
          )}

          <BatteryMetric label="SoC" value={data.soc.toFixed(2)} unit="%" />
          <BatteryMetric label="SoH" value={data.soh.toFixed(2)} unit="%" />
          <BatteryMetric
            label="Напруга"
            value={data.voltage.toFixed(2)}
            unit="V"
          />
          <BatteryMetric
            className="battery-energy-value"
            label="Енергія"
            value={`${data.energy.currentWh} / ${data.energy.totalWh}`}
            unit="Wh"
          />
        </div>

        <div className="battery-chart-card">
          <div className="battery-chart-head">
            <div className="battery-chart-title">Залишок енергії у Wh</div>
          </div>
          <div className="battery-chart-wrap">
            <BatteryEnergyChart data={chartData} timezone={data.timezone} />
          </div>
        </div>
      </div>
    </section>
  );
}

function BatteryMetric({ label, value, unit, className = "" }) {
  return (
    <div className="battery-stat-card">
      <div className="battery-stat-label">{label}</div>
      <div className={`battery-stat-value ${className}`.trim()}>
        {value} <span className="battery-stat-unit">{unit}</span>
      </div>
    </div>
  );
}

function BatteryEnergyChart({ data, timezone }) {
  if (data.length === 0) {
    return <div className="compact-empty-chart">Немає даних батареї</div>;
  }

  const values = data.map((point) => point.wh);
  const maxValue = Math.max(...values, 960);
  const minValue = Math.max(0, Math.min(...values) - 80);
  const ticks = buildTimeTicks(data);

  return (
    <ResponsiveContainer width="100%" height="100%">
      <AreaChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="rgba(0,0,0,0.08)" vertical={false} />
        <XAxis
          dataKey="time"
          type="number"
          domain={["dataMin", "dataMax"]}
          ticks={ticks}
          tickFormatter={(value) => formatDateTick(value, timezone)}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
          minTickGap={10}
          height={22}
        />
        <YAxis
          width={38}
          domain={[minValue, maxValue]}
          tickFormatter={(value) => `${Math.round(value)}`}
          tick={{ fill: "rgba(0,0,0,0.52)", fontSize: 10, fontWeight: 800 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          formatter={(value) => [`${Math.round(Number(value))} Wh`, "Енергія"]}
          labelFormatter={(value) => formatDateTime(value, timezone)}
          cursor={{ stroke: "rgba(0,0,0,0.28)", strokeDasharray: "4 4" }}
          contentStyle={tooltipStyle}
          itemStyle={tooltipItemStyle}
          labelStyle={tooltipLabelStyle}
        />
        <Area
          type="monotone"
          dataKey="wh"
          stroke="#15964b"
          strokeWidth={2.2}
          fill="rgba(21, 150, 75, 0.18)"
          dot={false}
          activeDot={{ r: 4, fill: "#ffffff", stroke: "#15964b", strokeWidth: 2 }}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function buildTimeTicks(data) {
  if (data.length < 2) return undefined;
  const first = data[0].time;
  const middle = data[Math.floor(data.length / 2)].time;
  const last = data.at(-1).time;
  return [first, middle, last];
}

function formatDateTick(value, timezone) {
  return new Date(value).toLocaleDateString("uk-UA", {
    timeZone: timezone,
    day: "2-digit",
    month: "2-digit",
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
