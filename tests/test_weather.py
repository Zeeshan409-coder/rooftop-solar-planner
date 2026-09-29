import json

import numpy as np
import pandas as pd
import pytest

from solarplan.weather import complete_days, location_from_nasa, parse_open_meteo, read_nasa_power

NASA = """-BEGIN HEADER-
NASA/POWER Source Native Resolution Hourly Data
Location: Latitude  31.5587   Longitude 74.3507
Elevation from MERRA-2: Average for 0.5 x 0.625 degree lat/lon region = 209.49 meters
-END HEADER-
YEAR,MO,DY,HR,CLRSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DNI,ALLSKY_SFC_SW_DIFF,SZA,T2M,QV2M,RH2M,PRECTOTCORR,PS,WS10M,WD10M
2024,6,15,5,700.0,650.0,800.0,120.0,30.0,40.0,10.0,20.0,0.0,97.0,2.0,180.0
2024,6,15,6,720.0,-999.0,-999.0,-999.0,28.0,41.0,10.0,20.0,0.0,97.0,2.0,180.0
"""


def test_read_nasa_power(tmp_path):
    f = tmp_path / "nasa.csv"
    f.write_text(NASA)
    df = read_nasa_power(f)
    # 05:00 UTC is 10:00 in Pakistan
    assert df.index[0] == pd.Timestamp("2024-06-15 10:00", tz="Asia/Karachi")
    assert df["ghi"].iloc[0] == 650 and df["temp"].iloc[0] == 40
    assert np.isnan(df["ghi"].iloc[1])      # -999 means missing
    assert location_from_nasa(f) == (31.5587, 74.3507, 209.49)


def test_open_meteo_hours_are_shifted_to_hour_start():
    payload = json.loads(json.dumps({"hourly": {
        "time": ["2026-06-15T12:00", "2026-06-15T13:00"],
        "shortwave_radiation": [800, 850], "direct_normal_irradiance": [700, 720],
        "diffuse_radiation": [120, 110], "temperature_2m": [40, 41], "wind_speed_10m": [2, 3],
        "relative_humidity_2m": [20, 21], "precipitation": [0, 0]}}))
    df = parse_open_meteo(payload)
    # the 12:00 value is the average over 11:00-12:00
    assert df.index[0] == pd.Timestamp("2026-06-15 11:00", tz="Asia/Karachi")
    assert df["ghi"].tolist() == [800, 850]


def test_complete_days_drops_days_with_gaps():
    idx = pd.date_range("2024-01-01", periods=48, freq="h", tz="Asia/Karachi")
    df = pd.DataFrame({"ghi": 1.0, "dni": 1.0, "dhi": 1.0, "temp": 1.0}, index=idx)
    df.iloc[30, 0] = np.nan
    assert set(complete_days(df).index.day) == {1}


def _fake_get(payload, calls):
    def get(url, params, timeout):
        calls.append((url, params))
        if callable(payload):
            payload_ = payload(url, params)
        else:
            payload_ = payload
        if isinstance(payload_, Exception):
            raise payload_
        return payload_ if isinstance(payload_, bytes) else json.dumps(payload_).encode()
    return get


def test_live_forecast_request_and_parsing(monkeypatch, open_meteo_payload):
    from solarplan import weather

    calls = []
    monkeypatch.setattr(weather, "_get", _fake_get(open_meteo_payload("2024-06-15", "2024-06-18 22:00"), calls))
    df = weather.fetch_forecast(31.55, 74.34, days=3)
    url, params = calls[0]
    assert url == "https://api.open-meteo.com/v1/forecast"
    assert params["forecast_days"] == 4 and params["timezone"] == "Asia/Karachi" and params["wind_speed_unit"] == "ms"
    assert set(params["hourly"].split(",")) >= {"shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation"}
    assert df.index[0] == pd.Timestamp("2024-06-15 00:00", tz="Asia/Karachi")
    assert df.attrs["elevation"] == 217.0
    assert df["pressure"].between(90, 110).all()        # kPa, like NASA


def test_history_download_is_cached(monkeypatch, tmp_path, open_meteo_payload, lahore_weather):
    from solarplan import weather

    calls = []
    monkeypatch.setattr(weather, "_get", _fake_get(open_meteo_payload("2023-01-01", "2024-12-31 22:00"), calls))
    cache = tmp_path / "cache" / "weather_x.csv.gz"
    w, elev, source = weather.history_for(31.55, 74.34, 2023, 2024, cache)
    assert calls[0][0] == "https://archive-api.open-meteo.com/v1/archive"
    assert calls[0][1]["start_date"] == "2023-01-01" and calls[0][1]["end_date"] == "2024-12-31"
    assert source == "Open-Meteo archive" and elev == 217.0 and cache.exists()
    assert np.allclose(w["ghi"].loc["2024-06-15"], lahore_weather["ghi"].loc["2024-06-15"], atol=0.1)

    # second time: straight from the cache, no request
    w2, elev2, source2 = weather.history_for(31.55, 74.34, 2023, 2024, cache)
    assert len(calls) == 1
    assert (elev2, source2) == (217.0, "Open-Meteo archive")
    pd.testing.assert_index_equal(w2.index, w.index)
    assert np.allclose(w2["ghi"], w["ghi"], atol=0.01, equal_nan=True)


def test_history_falls_back_to_nasa(monkeypatch, tmp_path):
    from solarplan import weather

    def answer(url, params):
        if "open-meteo" in url:
            return OSError("blocked")
        return NASA.replace("2024,", f"{params['start'][:4]},").encode()

    calls = []
    monkeypatch.setattr(weather, "_get", _fake_get(answer, calls))
    w, elev, source = weather.history_for(31.55, 74.34, 2023, 2024, tmp_path / "c.csv.gz")
    assert source == "NASA POWER" and elev == 209.49
    nasa_calls = [p for u, p in calls if "nasa" in u]
    assert [(p["start"], p["end"]) for p in nasa_calls] == [("20230101", "20231231"), ("20240101", "20241231")]
    assert len(w) == 4


def test_history_error_when_offline(monkeypatch, tmp_path):
    from solarplan import weather

    monkeypatch.setattr(weather, "_get", _fake_get(OSError("no internet"), []))
    with pytest.raises(OSError, match="could not download"):
        weather.history_for(31.55, 74.34, 2023, 2024, tmp_path / "c.csv.gz")
