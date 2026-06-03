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
