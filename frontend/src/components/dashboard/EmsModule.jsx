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
            <path
              className="ems-flow-line grid"
              d="M164 42 H285 Q301 42 301 58 V82 Q301 98 317 98 H334"
              vectorEffect="non-scaling-stroke"
            />
            <path
              className="ems-flow-line solar"
              d="M164 115 H334"
              vectorEffect="non-scaling-stroke"
            />
            <path
              className="ems-flow-line dual-grid"
              d="M426 115 H582"
              vectorEffect="non-scaling-stroke"
            />
            <path
              className="ems-flow-line dual-solar"
              d="M426 115 H582"
              vectorEffect="non-scaling-stroke"
            />
            <path
              className="ems-flow-line dual-grid"
              d="M382 160 V176 Q382 192 366 192 H164"
              vectorEffect="non-scaling-stroke"
            />
            <path
              className="ems-flow-line dual-solar"
              d="M382 160 V176 Q382 192 366 192 H164"
              vectorEffect="non-scaling-stroke"
            />
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
