from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from generate_ideal_solar import _resolve_date_range


def test_year_range_uses_station_local_timezone_for_non_leap_year() -> None:
    args = argparse.Namespace(year=2026, start=None, end=None)

    start, end = _resolve_date_range(args, ZoneInfo("Europe/Kyiv"))

    assert start.isoformat() == "2026-01-01T00:00:00+02:00"
    assert end.isoformat() == "2027-01-01T00:00:00+02:00"
    assert (end.date() - start.date()).days == 365


def test_year_range_uses_station_local_timezone_for_leap_year() -> None:
    args = argparse.Namespace(year=2028, start=None, end=None)

    start, end = _resolve_date_range(args, ZoneInfo("Europe/Kyiv"))

    assert start.isoformat() == "2028-01-01T00:00:00+02:00"
    assert end.isoformat() == "2029-01-01T00:00:00+02:00"
    assert (end.date() - start.date()).days == 366
