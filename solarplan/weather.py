"""Hourly weather: NASA POWER and Open-Meteo history, Open-Meteo forecasts.

History for any point in Pakistan comes from the Open-Meteo archive (ERA5 reanalysis,
one fast request) or, if that fails, NASA POWER (satellite-based, one request per
year). Both are free and need no key. Downloads are cached as plain CSV files.

Everything downstream works on one hourly DataFrame indexed by the start of each
hour in local time, with these columns:

    ghi, dni, dhi   W/m2, average over the hour
    ghi_clear       W/m2, clear-sky GHI (only in NASA data)
    temp            C, air temperature at 2 m
    wind            m/s at 10 m
    rh              %, relative humidity
    rain            mm/h
"""

from __future__ import annotations

import gzip
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

NASA_COLUMNS = {
    "ALLSKY_SFC_SW_DWN": "ghi",
    "ALLSKY_SFC_SW_DNI": "dni",
    "ALLSKY_SFC_SW_DIFF": "dhi",
    "CLRSKY_SFC_SW_DWN": "ghi_clear",
    "T2M": "temp",
    "WS10M": "wind",
    "RH2M": "rh",
    "PRECTOTCORR": "rain",
    "PS": "pressure",
}
NASA_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_HOURLY = {
    "shortwave_radiation": "ghi",
    "direct_normal_irradiance": "dni",
    "diffuse_radiation": "dhi",
    "temperature_2m": "temp",
    "wind_speed_10m": "wind",
    "relative_humidity_2m": "rh",
    "precipitation": "rain",
    "surface_pressure": "pressure",
}
USER_AGENT = "rooftop-solar-planner/0.2"


def _get(url: str, params: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _open_text(path: str | Path) -> str:
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as f:
            return f.read()
    return path.read_text()


def read_nasa_power(path: str | Path, tz: str = "Asia/Karachi") -> pd.DataFrame:
    """Read an hourly NASA POWER CSV (UTC time standard) into the common format."""
    return _parse_nasa_text(_open_text(path), tz)


def _parse_nasa_text(text: str, tz: str) -> pd.DataFrame:
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith("-END HEADER-")), 0)
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    stamps = pd.to_datetime(dict(year=df.YEAR, month=df.MO, day=df.DY, hour=df.HR), utc=True)
    df = df.rename(columns=NASA_COLUMNS)
    out = df[[c for c in NASA_COLUMNS.values() if c in df]]
    out = out.replace(-999.0, np.nan)
    out.index = stamps.dt.tz_convert(tz)
    out.index.name = "time"
    # NASA reports Wh/m2 for the hour, which is the same number as the mean W/m2
    return out.astype(float)


def location_from_nasa(path: str | Path) -> tuple[float, float, float]:
    """(latitude, longitude, elevation) from the NASA POWER header."""
    return _nasa_header(_open_text(path).splitlines()[:25])


def _nasa_header(lines: list[str]) -> tuple[float, float, float]:
    lat = lon = elev = None
    for line in lines[:25]:
        if line.startswith("Location:"):
            parts = line.replace("Latitude", "").replace("Longitude", "").split()
            lat, lon = float(parts[1]), float(parts[2])
        if line.startswith("Elevation"):
            try:
                elev = float(line.split("=")[1].split()[0])
            except (IndexError, ValueError):
                elev = 0.0
    if lat is None:
        raise ValueError("no location in NASA POWER header")
    return lat, lon, elev or 0.0


def download_nasa_power(lat: float, lon: float, start: str, end: str, dest: str | Path) -> Path:
    """Download hourly history for any location from NASA POWER (free, no key).

    start/end as YYYYMMDD. Saves the raw CSV (gzipped if dest ends in .gz).
    """
    text = _nasa_request(lat, lon, start, end)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix == ".gz":
        with gzip.open(dest, "wt") as f:
            f.write(text)
    else:
        dest.write_text(text)
    return dest


def _nasa_request(lat: float, lon: float, start: str, end: str) -> str:
    params = {
        "parameters": ",".join(NASA_COLUMNS),
        "community": "RE",
        "longitude": f"{lon:.4f}",
        "latitude": f"{lat:.4f}",
        "start": start,
        "end": end,
        "format": "CSV",
        "time-standard": "UTC",
    }
    return _get(NASA_URL, params, timeout=180).decode()


def nasa_history(lat: float, lon: float, first_year: int, last_year: int,
                 tz: str = "Asia/Karachi") -> tuple[pd.DataFrame, float]:
    """Several years of NASA POWER history, fetched a year at a time. Returns (weather, elevation)."""
    parts, elevation = [], 0.0
    for year in range(first_year, last_year + 1):
        text = _nasa_request(lat, lon, f"{year}0101", f"{year}1231")
        parts.append(_parse_nasa_text(text, tz))
        elevation = _nasa_header(text.splitlines())[2]
    return pd.concat(parts).sort_index(), elevation


def fetch_history(lat: float, lon: float, first_year: int, last_year: int,
                  tz: str = "Asia/Karachi") -> tuple[pd.DataFrame, float]:
    """Hourly history from the Open-Meteo archive (ERA5). Returns (weather, elevation)."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": f"{first_year}-01-01",
        "end_date": f"{last_year}-12-31",
        "hourly": ",".join(OPEN_METEO_HOURLY),
        "wind_speed_unit": "ms",
        "timezone": tz,
    }
    data = json.loads(_get(OPEN_METEO_ARCHIVE_URL, params, timeout=180).decode())
    return parse_open_meteo(data, tz), float(data.get("elevation") or 0.0)


def history_for(lat: float, lon: float, first_year: int, last_year: int, cache: str | Path,
                tz: str = "Asia/Karachi") -> tuple[pd.DataFrame, float, str]:
    """History for a point, from the cache file if present, else downloaded and cached.

    Returns (weather, elevation, source name)."""
    cache = Path(cache)
    if cache.exists():
        return read_cache(cache, tz)
    errors = []
    for source, fetch in (("Open-Meteo archive", fetch_history), ("NASA POWER", nasa_history)):
        try:
            weather, elevation = fetch(lat, lon, first_year, last_year, tz)
        except (OSError, ValueError, KeyError) as e:
            errors.append(f"{source}: {e}")
            continue
        write_cache(cache, weather, elevation, source)
        return weather, elevation, source
    raise OSError("could not download weather history (" + "; ".join(errors) + ")")


def write_cache(path: str | Path, weather: pd.DataFrame, elevation: float, source: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = weather.copy()
    out.index = out.index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%MZ")
    with gzip.open(path, "wt") as f:
        f.write(f"# elevation={elevation:.0f} source={source}\n")
        out.round(2).to_csv(f, index_label="time_utc")


def read_cache(path: str | Path, tz: str = "Asia/Karachi") -> tuple[pd.DataFrame, float, str]:
    with gzip.open(path, "rt") as f:
        header = f.readline()
        df = pd.read_csv(f)
    elevation = re.search(r"elevation=(-?[\d.]+)", header)
    source = header.split("source=", 1)[1].strip() if "source=" in header else ""
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("time_utc"), utc=True)).tz_convert(tz)
    df.index.name = "time"
    return df.astype(float), float(elevation.group(1)) if elevation else 0.0, source


def fetch_forecast(lat: float, lon: float, days: int = 2, tz: str = "Asia/Karachi") -> pd.DataFrame:
    """Hourly forecast from Open-Meteo (free, no key) in the common format, from 00:00
    today local time for `days` days after today. The site elevation is in `.attrs`."""
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": ",".join(OPEN_METEO_HOURLY),
        "wind_speed_unit": "ms",
        "timezone": tz,
        "forecast_days": days + 1,
    }
    data = json.loads(_get(OPEN_METEO_URL, params, timeout=30).decode())
    df = parse_open_meteo(data, tz)
    df.attrs["elevation"] = float(data.get("elevation") or 0.0)
    return df


def parse_open_meteo(data: dict, tz: str = "Asia/Karachi") -> pd.DataFrame:
    if "hourly" not in data:
        raise ValueError(data.get("reason", "unexpected response from Open-Meteo"))
    h = data["hourly"]
    # Open-Meteo radiation is the mean over the *preceding* hour; shift so each row
    # describes the hour that starts at its timestamp, like the NASA data.
    idx = pd.DatetimeIndex(pd.to_datetime(h["time"])).tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    df = pd.DataFrame({ours: h[theirs] for theirs, ours in OPEN_METEO_HOURLY.items() if theirs in h},
                      index=idx - pd.Timedelta(hours=1)).astype(float)
    df = df[df.index.notna()]
    if "pressure" in df:
        df["pressure"] = df["pressure"] / 10     # hPa -> kPa, as in the NASA data
    df.index.name = "time"
    return df


def fill_gaps(df: pd.DataFrame, max_hours: int = 3) -> pd.DataFrame:
    """Interpolate short gaps; longer gaps stay NaN (whole days are dropped later)."""
    return df.interpolate(limit=max_hours, limit_area="inside")


def complete_days(df: pd.DataFrame, columns=("ghi", "dni", "dhi", "temp")) -> pd.DataFrame:
    """Keep only days with all 24 hours present for the given columns."""
    ok = df[list(columns)].notna().all(axis=1)
    counts = ok.groupby(df.index.date).transform("sum")
    return df[counts == 24]
