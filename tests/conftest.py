import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LAHORE = ROOT / "data" / "nasa_power_lahore_2018_2025.csv.gz"


@pytest.fixture(scope="session")
def lahore_weather():
    from solarplan.weather import fill_gaps, read_nasa_power

    return fill_gaps(read_nasa_power(LAHORE))


@pytest.fixture
def june_day(lahore_weather) -> pd.DataFrame:
    return lahore_weather.loc["2024-06-15":].iloc[:48]


@pytest.fixture
def open_meteo_payload(lahore_weather):
    """Build responses shaped like Open-Meteo's (forecast and archive share the format)
    from real Lahore history, so the download code can be tested without internet."""
    def make(start: str, end: str) -> dict:
        w = lahore_weather.loc[start:end]
        stamps = w.index + pd.Timedelta(hours=1)          # values are for the preceding hour
        return {
            "latitude": 31.5, "longitude": 74.375, "elevation": 217.0, "timezone": "Asia/Karachi",
            "utc_offset_seconds": 18000,
            "hourly": {
                "time": [t.strftime("%Y-%m-%dT%H:%M") for t in stamps],
                "shortwave_radiation": w["ghi"].round(1).tolist(),
                "direct_normal_irradiance": w["dni"].round(1).tolist(),
                "diffuse_radiation": w["dhi"].round(1).tolist(),
                "temperature_2m": w["temp"].round(1).tolist(),
                "wind_speed_10m": w["wind"].round(1).tolist(),
                "relative_humidity_2m": w["rh"].round(0).tolist(),
                "precipitation": w["rain"].round(1).tolist(),
                "surface_pressure": (w["pressure"] * 10).round(1).tolist(),
            },
        }
    return make
