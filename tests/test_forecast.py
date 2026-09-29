import numpy as np
import pandas as pd

from solarplan.forecast import LoadForecaster, SolarForecaster, persistence
from solarplan.household import simulate

LAT, LON, ALT = 31.5587, 74.3507, 209.0


def test_persistence_is_the_same_hour_earlier():
    idx = pd.date_range("2024-01-01", periods=72, freq="h", tz="Asia/Karachi")
    s = pd.Series(np.arange(72.0), index=idx)
    p = persistence(s, 1)
    assert p.iloc[30] == 6.0 and p.iloc[50] == 26.0


def test_solar_forecast_uses_only_the_past(lahore_weather):
    w = lahore_weather.loc["2022-01-02":"2023-03-31"]
    model = SolarForecaster(LAT, LON, ALT, 1).fit(w.loc[:"2022"])
    fc = model.predict_weather(w.loc["2022-12-25":])
    march = fc.loc["2023-03"]
    assert (march["ghi"] >= 0).all()
    assert march["ghi"][march.index.hour == 2].max() == 0
    # changing the weather on a day must not change that same day's forecast
    changed = w.loc["2022-12-25":].copy()
    changed.loc["2023-03-15", "ghi"] = 0.0
    again = model.predict_weather(changed)
    assert np.allclose(again.loc["2023-03-15", "ghi"], fc.loc["2023-03-15", "ghi"])
    assert not np.allclose(again.loc["2023-03-16", "ghi"], fc.loc["2023-03-16", "ghi"])


def test_load_forecast_beats_yesterday(lahore_weather):
    w = lahore_weather.loc["2022-01-02":"2023-12-31"]
    load = simulate(w.index, w["temp"])["base"]
    model = LoadForecaster(1).fit(load.loc["2022"], w["temp"].loc["2022"])
    test = slice("2022-12-20", "2023-12-31")
    fc = model.predict(load.loc[test], w["temp"].loc[test]).loc["2023"]
    actual = load.loc["2023"]
    yesterday = persistence(load.loc[test], 1).loc["2023"]
    assert (fc - actual).abs().mean() < (yesterday - actual).abs().mean()
