from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.schemas import AppConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        raw_config = yaml.safe_load(file)

    config = AppConfig(**raw_config)
    _validate_solar_panel_references(config)
    return config


def calculate_config_hash(config: AppConfig) -> str:
    payload = _model_to_plain_data(config)
    stable_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable_json.encode("utf-8")).hexdigest()


def _validate_solar_panel_references(config: AppConfig) -> None:
    panel_type_ids = {panel.id for panel in config.station.solar.panel_types}
    for connection in config.station.solar.array.series_connections:
        if connection.panel_type_id not in panel_type_ids:
            raise ValueError(
                "Solar series connection "
                f"'{connection.id}' references missing panel_type_id "
                f"'{connection.panel_type_id}'"
            )


def _model_to_plain_data(config: AppConfig) -> dict[str, Any]:
    if hasattr(config, "model_dump"):
        return config.model_dump()
    return config.dict()
