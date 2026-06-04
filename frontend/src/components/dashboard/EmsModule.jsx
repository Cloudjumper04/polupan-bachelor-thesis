import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Battery,
  Home,
  Plug,
  Sun,
} from "lucide-react";
import inverterImage from "../../assets/inverter.png";
import { emsMockData } from "../../data/dashboardMockData";

const NODE_ICONS = {
  grid: Plug,
  solar: Sun,
  battery: Battery,
  load: Home,
};

const POWER_ACTIVE_THRESHOLD_W = 0.5;
const FLOW_PATHS = {
  grid: "M164 42 H285 Q301 42 301 58 V82 Q301 98 317 98 H334",
  solar: "M164 115 H334",
  load: "M426 115 H582",
  battery: "M382 160 V176 Q382 192 366 192 H164",
};

export default function EmsModule({ data = emsMockData }) {
  const [controlMode, setControlMode] = useState(data.initialControlMode);
  const [manualModeId, setManualModeId] = useState(data.manualModeId);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const previousInitialControlMode = useRef(data.initialControlMode);
  const previousManualModeId = useRef(data.manualModeId);

  const modeById = useMemo(
    () => new Map(data.modes.map((mode) => [mode.id, mode])),
    [data.modes],
  );
  const selectedMode =
    controlMode === "auto"
      ? modeById.get(data.autoModeId)
      : modeById.get(manualModeId);
  const modeLocked = controlMode === "auto";
  const flow = useMemo(() => normalizeFlow(data), [data]);
  const rawMetrics = normalizeRawMetrics(data);
  const gridLineState = getGridLineState(flow);
  const solarLineState = getSolarLineState(flow);
  const batteryLineState = getBatteryLineState(flow);
  const loadLineState = getLoadLineState(flow, rawMetrics);

  useEffect(() => {
    if (controlMode === previousInitialControlMode.current) {
      setControlMode(data.initialControlMode);
    }
    previousInitialControlMode.current = data.initialControlMode;
  }, [controlMode, data.initialControlMode]);

  useEffect(() => {
    if (manualModeId === previousManualModeId.current) {
      setManualModeId(data.manualModeId);
    }
    previousManualModeId.current = data.manualModeId;
  }, [data.manualModeId, manualModeId]);

  function selectControlMode(nextMode) {
    setControlMode(nextMode);
    setDropdownOpen(false);
  }

  function toggleModeDropdown() {
    if (modeLocked) return;
    setDropdownOpen((value) => !value);
  }

  function selectManualMode(modeId) {
    setManualModeId(modeId);
    setDropdownOpen(false);
  }

  return (
    <section className="ems-card" aria-label="EMS">
      <header className="ems-header">
        <div className="ems-title-wrap">
          <h2 className="ems-title" title={data.titleTooltip}>
            EMS
          </h2>
        </div>

        <div className="ems-header-divider" aria-hidden="true" />

        <div className="ems-head-control">
          <div className="ems-toggle" role="group" aria-label="Режим керування EMS">
            <button
              className={controlMode === "manual" ? "active" : ""}
              type="button"
              aria-pressed={controlMode === "manual"}
              onClick={() => selectControlMode("manual")}
            >
              Manual
            </button>
            <button
              className={controlMode === "auto" ? "active" : ""}
              type="button"
              aria-pressed={controlMode === "auto"}
              onClick={() => selectControlMode("auto")}
            >
              Auto
            </button>
          </div>

          <ArrowRight className="ems-mode-arrow" aria-hidden="true" />

          <div className="ems-mode-wrap">
            <button
              className={`ems-mode-button ${modeLocked ? "locked" : "selectable"}`}
              type="button"
              aria-haspopup={!modeLocked ? "listbox" : undefined}
              aria-expanded={!modeLocked ? dropdownOpen : undefined}
              aria-disabled={modeLocked}
              title={
                modeLocked
                  ? `${selectedMode?.name}: режим вибрано автоматично. Перемкніть EMS у Manual, щоб змінити його вручну.`
                  : `${selectedMode?.name}: натисніть, щоб вибрати інший ручний режим.`
              }
              onClick={toggleModeDropdown}
            >
              <span className="ems-mode-name">{selectedMode?.name}</span>
            </button>

            {!modeLocked && dropdownOpen && (
              <div className="ems-mode-dropdown" role="listbox" aria-label="Ручний режим EMS">
                {data.modes.map((mode) => (
                  <button
                    key={mode.id}
                    className={mode.id === manualModeId ? "active" : ""}
                    type="button"
                    role="option"
                    aria-selected={mode.id === manualModeId}
                    title={mode.tooltip}
                    onClick={() => selectManualMode(mode.id)}
                  >
                    {mode.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="ems-header-divider" aria-hidden="true" />

        <div className="ems-risk" title={data.riskTooltip}>
          <div className="ems-risk-label">
            <span>Оцінка</span>
            <span>ризику</span>
          </div>
          <div className="ems-risk-score">
            {data.riskScore}
            <span>/100</span>
          </div>
        </div>
      </header>

      <div className="ems-body">
        <section className="ems-flow" aria-label="Схема потоків енергії">
          <svg className="ems-flow-svg" viewBox="0 0 760 230" preserveAspectRatio="none" aria-hidden="true">
            {renderFlowLine("grid", gridLineState)}
            {renderFlowLine("solar", solarLineState)}
            {renderFlowLine("load", loadLineState)}
            {renderFlowLine("battery", batteryLineState)}
          </svg>

          <EnergyNode type="grid" node={data.nodes.grid} />
          <EnergyNode type="solar" node={data.nodes.solar} />
          <EnergyNode type="battery" node={data.nodes.battery} />

          <div className="ems-inverter" aria-label="Інвертор">
            <img src={inverterImage} alt="" />
          </div>

          <EnergyNode type="load" node={data.nodes.load} />
        </section>

        <div className="ems-metrics-row">
          {data.metrics.map((metric) => (
            <div className="ems-metric-tile" key={metric.label}>
              <div className="ems-metric-label">{metric.label}</div>
              <div className="ems-metric-value">{metric.value}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function renderFlowLine(pathId, state) {
  const path = FLOW_PATHS[pathId];
  const sources = state.sources.length > 0 ? state.sources : ["inactive"];

  return sources.map((source, index) => (
    <path
      key={`${pathId}-${source}-${index}`}
      className={flowLineClass(state, source, index)}
      d={path}
      vectorEffect="non-scaling-stroke"
    />
  ));
}

function getGridLineState(flow) {
  if (isActivePower(flow.grid_to_load_w) || isActivePower(flow.grid_to_battery_w)) {
    return singleSourceLine("grid", "to-inverter");
  }
  return inactiveLine();
}

function getSolarLineState(flow) {
  if (isActivePower(flow.solar_to_load_w) || isActivePower(flow.solar_to_battery_w)) {
    return singleSourceLine("solar", "to-inverter");
  }
  return inactiveLine();
}

function getBatteryLineState(flow) {
  const batteryDischarging =
    isActivePower(flow.battery_to_load_w) || flow.battery_net_power_w < -POWER_ACTIVE_THRESHOLD_W;
  const gridCharging = isActivePower(flow.grid_to_battery_w);
  const solarCharging = isActivePower(flow.solar_to_battery_w);

  if (batteryDischarging) {
    return singleSourceLine("battery", "to-inverter");
  }
  if (gridCharging && solarCharging) {
    return mixedSourceLine(["grid", "solar"], "to-battery");
  }
  if (gridCharging) {
    return singleSourceLine("grid", "to-battery");
  }
  if (solarCharging) {
    return singleSourceLine("solar", "to-battery");
  }
  return inactiveLine();
}

function getLoadLineState(flow, rawMetrics) {
  const loadCutByProtection =
    rawMetrics.protection_active && isActivePower(flow.curtailed_or_cut_load_w);
  const loadReceivesPower =
    isActivePower(flow.effective_load_power_w) &&
    rawMetrics.inverter_output_enabled !== false &&
    !loadCutByProtection;

  if (!loadReceivesPower) {
    return inactiveLine();
  }

  const sources = [];
  if (isActivePower(flow.grid_to_load_w)) sources.push("grid");
  if (isActivePower(flow.solar_to_load_w)) sources.push("solar");
  if (isActivePower(flow.battery_to_load_w)) sources.push("battery");

  if (sources.length === 0) return inactiveLine();
  if (sources.length === 1) return singleSourceLine(sources[0], "to-load");
  return mixedSourceLine(sources, "to-load");
}

function inactiveLine() {
  return { sources: ["inactive"], direction: "none", mixed: false };
}

function singleSourceLine(source, direction) {
  return { sources: [source], direction, mixed: false };
}

function mixedSourceLine(sources, direction) {
  return { sources, direction, mixed: true };
}

function flowLineClass(state, source, index) {
  const classes = ["ems-flow-line", `flow-${source}`, `flow-${state.direction}`];
  if (source === "inactive") {
    classes.push("flow-inactive");
  }
  if (state.mixed) {
    classes.push(
      "flow-mixed",
      `flow-layer-${index + 1}`,
      mixedFlowClassName(state.sources),
    );
  }
  return classes.join(" ");
}

function mixedFlowClassName(sources) {
  const key = ["battery", "grid", "solar"]
    .filter((source) => sources.includes(source))
    .join("-");
  return `flow-mixed-${key}`;
}

function normalizeFlow(data) {
  const fallback = inferFlowFromDisplayData(data);
  const raw = data.flow ?? {};
  return {
    grid_to_load_w: readFlowNumber(raw.grid_to_load_w, fallback.grid_to_load_w),
    grid_to_battery_w: readFlowNumber(raw.grid_to_battery_w, fallback.grid_to_battery_w),
    solar_to_load_w: readFlowNumber(raw.solar_to_load_w, fallback.solar_to_load_w),
    solar_to_battery_w: readFlowNumber(
      raw.solar_to_battery_w,
      fallback.solar_to_battery_w,
    ),
    battery_to_load_w: readFlowNumber(raw.battery_to_load_w, fallback.battery_to_load_w),
    battery_net_power_w: readFlowNumber(
      raw.battery_net_power_w,
      fallback.battery_net_power_w,
    ),
    effective_load_power_w: readFlowNumber(
      raw.effective_load_power_w,
      fallback.effective_load_power_w,
    ),
    curtailed_or_cut_load_w: readFlowNumber(raw.curtailed_or_cut_load_w, 0),
  };
}

function inferFlowFromDisplayData(data) {
  const gridPowerW = Math.max(0, parseDisplayPowerW(data.nodes?.grid?.value));
  const solarPowerW = Math.max(0, parseDisplayPowerW(data.nodes?.solar?.value));
  const batteryNetPowerW = parseDisplayPowerW(data.nodes?.battery?.value);
  const loadPowerW = Math.max(0, parseDisplayPowerW(data.nodes?.load?.value));
  const chargingPowerW = Math.max(0, batteryNetPowerW);
  const dischargingPowerW = Math.max(0, -batteryNetPowerW);
  const solarToBatteryW = Math.min(chargingPowerW, solarPowerW);
  const gridToBatteryW = Math.max(0, chargingPowerW - solarToBatteryW);
  const solarToLoadW = Math.max(0, solarPowerW - solarToBatteryW);
  const remainingLoadW = Math.max(0, loadPowerW - solarToLoadW);
  const gridToLoadW = Math.min(gridPowerW, remainingLoadW);

  return {
    grid_to_load_w: gridToLoadW,
    grid_to_battery_w: gridToBatteryW,
    solar_to_load_w: solarToLoadW,
    solar_to_battery_w: solarToBatteryW,
    battery_to_load_w: dischargingPowerW,
    battery_net_power_w: batteryNetPowerW,
    effective_load_power_w: loadPowerW,
    curtailed_or_cut_load_w: 0,
  };
}

function normalizeRawMetrics(data) {
  const raw = data.rawMetrics ?? {};
  return {
    inverter_output_enabled: raw.inverter_output_enabled !== false,
    protection_active: raw.protection_active === true,
  };
}

function isActivePower(value) {
  return readFlowNumber(value, 0) > POWER_ACTIVE_THRESHOLD_W;
}

function readFlowNumber(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function parseDisplayPowerW(value) {
  const text = `${value ?? ""}`.replace(",", ".");
  const match = text.match(/([+-]?\d+(?:\.\d+)?)\s*(kW|W)\b/i);
  if (!match) return 0;
  const numeric = Number(match[1]);
  if (!Number.isFinite(numeric)) return 0;
  return match[2].toLowerCase() === "kw" ? numeric * 1000 : numeric;
}

function EnergyNode({ type, node }) {
  const Icon = NODE_ICONS[type];

  return (
    <div className={`ems-flow-node ${type}`}>
      <div className="ems-flow-icon">
        <Icon aria-hidden="true" />
      </div>
      <div className="ems-flow-copy">
        <div className="ems-flow-label">{node.label}</div>
        <div className="ems-flow-value">{node.value}</div>
      </div>
    </div>
  );
}
