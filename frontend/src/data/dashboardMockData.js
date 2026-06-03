export const emsMockData = {
  initialControlMode: "auto",
  autoModeId: "battery_reserve",
  manualModeId: "battery_reserve",
  riskScore: 62,
  titleTooltip:
    "EMS - система керування енергією, яка вибирає режим роботи інвертора, батареї, мережі та навантаження.",
  riskTooltip:
    "Оцінка ризику враховує поточний стан мережі, заряд батареї та потребу зберегти резерв для можливих відключень.",
  modes: [
    {
      id: "grid",
      name: "Мережа",
      tooltip:
        "Мережа: навантаження живиться від мережі, АКБ заряджається за потреби.",
    },
    {
      id: "solar",
      name: "Сонце",
      tooltip:
        "Сонце: сонячна генерація має пріоритет для навантаження і заряду АКБ.",
    },
    {
      id: "self_consumption",
      name: "Самоспож.",
      tooltip:
        "Самоспоживання: система мінімізує використання мережі, поки це безпечно для АКБ.",
    },
    {
      id: "battery_priority",
      name: "АКБ пріор.",
      tooltip:
        "Пріоритет АКБ: батарея активніше використовується для живлення навантаження.",
    },
    {
      id: "battery_reserve",
      name: "Резерв АКБ",
      tooltip:
        "Резерв АКБ: система зберігає заряд батареї на випадок відключення.",
    },
    {
      id: "forced_charge",
      name: "Форс. заряд",
      tooltip:
        "Форсований заряд: система заряджає АКБ максимально допустимою потужністю.",
    },
  ],
  nodes: {
    grid: {
      label: "Мережа",
      value: "1.20 kW",
    },
    solar: {
      label: "Сонце",
      value: "2.48 kW",
    },
    battery: {
      label: "Батарея",
      value: "+0.35 kW",
    },
    load: {
      label: "Навантаження",
      value: "3.68 kW",
    },
  },
  metrics: [
    {
      label: "Стан інвертора",
      value: "Pass-through",
    },
    {
      label: "Заряд АКБ",
      value: "350 W",
    },
    {
      label: "Ціль SoC",
      value: "80%",
    },
    {
      label: "Cutoff SoC",
      value: "10%",
    },
  ],
};

export const batteryMockData = {
  timezone: "Europe/Kyiv",
  soc: 76,
  soh: 94,
  voltage: 12.47,
  energy: {
    currentWh: 730,
    totalWh: 960,
  },
  info: [
    { label: "Хімія", value: "Lead-acid" },
    { label: "Ємність", value: "200 Ah" },
    { label: "Номінальна напруга", value: "12 V" },
    { label: "Дата встановлення", value: "06.10.2025" },
  ],
  energyHistory: [
    { timestamp: "2026-05-31T00:00:00+03:00", wh: 820 },
    { timestamp: "2026-05-31T06:00:00+03:00", wh: 690 },
    { timestamp: "2026-05-31T12:00:00+03:00", wh: 760 },
    { timestamp: "2026-05-31T18:00:00+03:00", wh: 910 },
    { timestamp: "2026-06-01T00:00:00+03:00", wh: 780 },
    { timestamp: "2026-06-01T06:00:00+03:00", wh: 650 },
    { timestamp: "2026-06-01T12:00:00+03:00", wh: 735 },
    { timestamp: "2026-06-01T18:00:00+03:00", wh: 890 },
    { timestamp: "2026-06-02T00:00:00+03:00", wh: 760 },
    { timestamp: "2026-06-02T06:00:00+03:00", wh: 610 },
    { timestamp: "2026-06-02T12:00:00+03:00", wh: 705 },
    { timestamp: "2026-06-02T18:00:00+03:00", wh: 850 },
    { timestamp: "2026-06-03T00:00:00+03:00", wh: 730 },
  ],
};

export const loadMockData = {
  timezone: "Europe/Kyiv",
  currentPowerW: 428,
  dailyEnergyKwh: 3.84,
  solarCoveredPercent: 64,
  moneySavedUah: -8.4,
  monthlyEnergyKwh: 116.2,
  powerHistory: [
    { timestamp: "2026-06-02T00:00:00+03:00", w: 310 },
    { timestamp: "2026-06-02T02:00:00+03:00", w: 280 },
    { timestamp: "2026-06-02T04:00:00+03:00", w: 245 },
    { timestamp: "2026-06-02T06:00:00+03:00", w: 360 },
    { timestamp: "2026-06-02T08:00:00+03:00", w: 520 },
    { timestamp: "2026-06-02T10:00:00+03:00", w: 470 },
    { timestamp: "2026-06-02T12:00:00+03:00", w: 390 },
    { timestamp: "2026-06-02T14:00:00+03:00", w: 920 },
    { timestamp: "2026-06-02T16:00:00+03:00", w: 760 },
    { timestamp: "2026-06-02T18:00:00+03:00", w: 610 },
    { timestamp: "2026-06-02T20:00:00+03:00", w: 540 },
    { timestamp: "2026-06-02T22:00:00+03:00", w: 455 },
    { timestamp: "2026-06-03T00:00:00+03:00", w: 428 },
  ],
  monthlyEnergyHistory: [
    { date: "2026-05-01", wh: 4120 },
    { date: "2026-05-02", wh: 3560 },
    { date: "2026-05-03", wh: 4380 },
    { date: "2026-05-04", wh: 5120 },
    { date: "2026-05-05", wh: 4680 },
    { date: "2026-05-06", wh: 3900 },
    { date: "2026-05-07", wh: 4210 },
    { date: "2026-05-08", wh: 4860 },
    { date: "2026-05-09", wh: 5320 },
    { date: "2026-05-10", wh: 3760 },
    { date: "2026-05-11", wh: 3440 },
    { date: "2026-05-12", wh: 4010 },
    { date: "2026-05-13", wh: 4580 },
    { date: "2026-05-14", wh: 4920 },
    { date: "2026-05-15", wh: 4300 },
    { date: "2026-05-16", wh: 3860 },
    { date: "2026-05-17", wh: 3650 },
    { date: "2026-05-18", wh: 4170 },
    { date: "2026-05-19", wh: 4760 },
    { date: "2026-05-20", wh: 5260 },
    { date: "2026-05-21", wh: 4410 },
    { date: "2026-05-22", wh: 3990 },
    { date: "2026-05-23", wh: 3840 },
    { date: "2026-05-24", wh: 4520 },
    { date: "2026-05-25", wh: 4980 },
    { date: "2026-05-26", wh: 5520 },
    { date: "2026-05-27", wh: 4680 },
    { date: "2026-05-28", wh: 4250 },
    { date: "2026-05-29", wh: 5600 },
    { date: "2026-05-30", wh: 4920 },
    { date: "2026-05-31", wh: 4450 },
  ],
};
