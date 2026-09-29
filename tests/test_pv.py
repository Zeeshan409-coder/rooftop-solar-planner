import pandas as pd
import pytest

from solarplan.pv import PVSystemSpec, clear_sky, pv_output

LAT, LON = 31.56, 74.35


def clear_year():
    idx = pd.date_range("2024-01-01", "2024-12-31 23:00", freq="h", tz="Asia/Karachi")
    cs = clear_sky(idx, LAT, LON, 200)
    return pd.DataFrame({"ghi": cs["ghi"], "dni": cs["dni"], "dhi": cs["dhi"], "temp": 25.0, "wind": 1.0}, index=idx)


def test_no_output_at_night_and_never_above_inverter():
    w = clear_year()
    ac = pv_output(w, PVSystemSpec(kwp=10, inverter_kw=6), LAT, LON, 200)
    assert ac[w.index.hour == 1].max() == 0
    assert ac.max() <= 6.0 + 1e-9


def test_clear_sky_yield_is_plausible():
    ac = pv_output(clear_year(), PVSystemSpec(), LAT, LON, 200)
    per_kwp = ac.sum() / 10
    assert 1500 < per_kwp < 2300   # clear sky every day, so above real-world ~1450


def test_south_beats_north():
    w = clear_year()
    south = pv_output(w, PVSystemSpec(azimuth=180), LAT, LON).sum()
    north = pv_output(w, PVSystemSpec(azimuth=0), LAT, LON).sum()
    assert south > north * 1.15


def test_real_lahore_yield(lahore_weather):
    ac = pv_output(lahore_weather.loc["2023"], PVSystemSpec(), LAT, LON, 209)
    assert 1300 < ac.sum() / 10 < 1600
    assert PVSystemSpec().loss_fraction() == pytest.approx(0.13, abs=0.02)
