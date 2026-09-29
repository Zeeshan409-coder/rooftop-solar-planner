"""Past weather for any district: the bundled Lahore file, a cached download, or a
fresh download (Open-Meteo archive, NASA POWER as the fallback)."""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

import pandas as pd

from .locations import Location
from .weather import fill_gaps, history_for, location_from_nasa, read_nasa_power

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SOLARPLAN_DATA", ROOT / "data"))
FIRST_YEAR = 2018
BUNDLED = {"lahore": "nasa_power_lahore_2018_2025.csv.gz"}

_lock = threading.Lock()
_memo: dict[str, tuple[pd.DataFrame, float, str]] = {}


def cache_path(loc: Location, data_dir: Path | None = None) -> Path:
    return (data_dir or DATA) / "cache" / f"weather_{loc.id}.csv.gz"


def available(loc: Location, data_dir: Path | None = None) -> bool:
    """True if history for this place is on disk (no download needed)."""
    data_dir = data_dir or DATA
    return (loc.id in BUNDLED and (data_dir / BUNDLED[loc.id]).exists()) or cache_path(loc, data_dir).exists()


def load_history(loc: Location, data_dir: Path | None = None) -> tuple[pd.DataFrame, float, str]:
    """(hourly weather, site elevation in m, source name) for a place, downloading once if needed."""
    data_dir = data_dir or DATA
    with _lock:
        key = f"{data_dir}:{loc.id}"
        if key not in _memo:
            if loc.id in BUNDLED and (data_dir / BUNDLED[loc.id]).exists():
                path = data_dir / BUNDLED[loc.id]
                weather, elevation, source = read_nasa_power(path), location_from_nasa(path)[2], "NASA POWER"
            else:
                weather, elevation, source = history_for(loc.lat, loc.lon, FIRST_YEAR, date.today().year - 1,
                                                         cache_path(loc, data_dir))
            _memo[key] = (fill_gaps(weather), elevation, source)
        return _memo[key]


def full_years(weather: pd.DataFrame) -> list[int]:
    ok = weather["ghi"].notna().groupby(weather.index.normalize()).all()
    days = ok[ok].index
    return sorted({d.year for d in days if (days.year == d.year).sum() > 360})
