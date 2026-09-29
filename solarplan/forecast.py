"""Day-ahead forecasts of solar irradiance and household load.

In the app, tomorrow's weather comes from a numerical weather forecast
(Open-Meteo). For backtesting on history there is no archive of past weather
forecasts, so the solar model here predicts tomorrow from observations up to
tonight only. It is a deliberately modest forecast, which makes backtested
savings a conservative estimate.

Both models are gradient-boosted trees (scikit-learn), compared against the
usual baselines: persistence (tomorrow = today) and climatology.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pvlib
from sklearn.ensemble import HistGradientBoostingRegressor

from .pv import clear_sky


def _daily_block(s: pd.Series) -> pd.DataFrame:
    """Reshape an hourly series into days x 24 hours (complete days only)."""
    df = s.to_frame("v")
    df["day"] = df.index.normalize()
    df["hour"] = df.index.hour
    return df.pivot_table(index="day", columns="hour", values="v", aggfunc="mean")


@dataclass
class SolarForecaster:
    lat: float
    lon: float
    altitude: float = 0.0
    lead_days: int = 1

    def _features(self, weather: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
        """Rows = (target day, hour); features only use data from days before target."""
        cs = clear_sky(weather.index, self.lat, self.lon, self.altitude)["ghi"]
        kt = (weather["ghi"] / cs.where(cs > 20)).clip(0, 1.2)
        daily_k = (weather["ghi"].groupby(weather.index.normalize()).sum()
                   / cs.groupby(cs.index.normalize()).sum()).clip(0, 1.2)
        daily_rh = weather["rh"].groupby(weather.index.normalize()).mean()
        daily_rain = weather["rain"].groupby(weather.index.normalize()).sum()
        daily_tmax = weather["temp"].groupby(weather.index.normalize()).max()
        if "pressure" in weather:
            daily_p = weather["pressure"].groupby(weather.index.normalize()).mean()
        else:
            daily_p = daily_rh * 0

        L = self.lead_days
        day = weather.index.normalize()
        # previous-day values aligned to each target hour
        def lag_daily(series, k):
            shifted = series.copy()
            shifted.index = shifted.index + pd.Timedelta(days=k)
            return shifted.reindex(day).to_numpy()

        kt_same_hour = kt.copy()
        kt_same_hour.index = kt_same_hour.index + pd.Timedelta(days=L)
        doy = np.asarray(weather.index.dayofyear)
        X = pd.DataFrame({
            "hour": np.asarray(weather.index.hour),
            "doy_sin": np.sin(2 * np.pi * doy / 365.25),
            "doy_cos": np.cos(2 * np.pi * doy / 365.25),
            "clear_ghi": cs.to_numpy(),
            "k_prev": lag_daily(daily_k, L),
            "k_prev2": lag_daily(daily_k, L + 1),
            "k_prev3": lag_daily(daily_k, L + 2),
            "k_prev_hour": kt_same_hour.reindex(weather.index).to_numpy(),
            "rh_prev": lag_daily(daily_rh, L),
            "rain_prev": lag_daily(daily_rain, L),
            "tmax_prev": lag_daily(daily_tmax, L),
            "dp_prev": lag_daily(daily_p, L) - lag_daily(daily_p, L + 1),
        }, index=weather.index)
        return X, kt

    def fit(self, weather: pd.DataFrame) -> SolarForecaster:
        X, kt = self._features(weather)
        ok = (X["clear_ghi"] > 20) & kt.notna() & X["k_prev"].notna()
        self.model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                                   l2_regularization=1.0, random_state=0)
        self.model.fit(X[ok], kt[ok])
        return self

    def predict_kt(self, weather: pd.DataFrame) -> pd.Series:
        X, _ = self._features(weather)
        pred = pd.Series(self.model.predict(X.fillna(X.median(numeric_only=True))), index=X.index)
        return pred.where(X["clear_ghi"] > 20, 0.0).clip(0, 1.1)

    def predict_weather(self, weather: pd.DataFrame) -> pd.DataFrame:
        """Forecast weather frame (ghi, dni, dhi, temp, wind) for every hour of `weather`,
        each made `lead_days` before, using only earlier data."""
        cs = clear_sky(weather.index, self.lat, self.lon, self.altitude)
        ghi = self.predict_kt(weather) * cs["ghi"]
        return decompose(ghi, weather, self.lat, self.lon, self.altitude, self.lead_days)


def decompose(ghi: pd.Series, weather: pd.DataFrame, lat, lon, altitude, lead_days: int) -> pd.DataFrame:
    """Split forecast GHI into direct and diffuse (Erbs), with persistence temperature."""
    mid = ghi.index + pd.Timedelta(minutes=30)
    pos = pvlib.solarposition.get_solarposition(mid, lat, lon, altitude=altitude)
    split = pvlib.irradiance.erbs(ghi.to_numpy(), pos["zenith"].to_numpy(), ghi.index.dayofyear)
    temp = persistence(weather["temp"], lead_days).reindex(ghi.index)
    wind = (persistence(weather["wind"], lead_days).reindex(ghi.index) if "wind" in weather
            else pd.Series(1.0, index=ghi.index))
    return pd.DataFrame({"ghi": ghi.to_numpy(), "dni": np.asarray(split["dni"]), "dhi": np.asarray(split["dhi"]),
                         "temp": temp.to_numpy(), "wind": wind.to_numpy()}, index=ghi.index).fillna(0)


def persistence(series: pd.Series, lead_days: int = 1) -> pd.Series:
    """Same hour `lead_days` earlier."""
    s = series.copy()
    s.index = s.index + pd.Timedelta(days=lead_days)
    return s.reindex(series.index).interpolate(limit_direction="both")


@dataclass
class LoadForecaster:
    lead_days: int = 1

    def _features(self, load: pd.Series, temp: pd.Series) -> pd.DataFrame:
        L = self.lead_days
        idx = load.index
        day = idx.normalize()
        daily = load.groupby(day).mean()
        daily_t = temp.groupby(temp.index.normalize()).max()

        def lag_h(s, days):
            x = s.copy()
            x.index = x.index + pd.Timedelta(days=days)
            return x.reindex(idx).to_numpy()

        def lag_d(s, days):
            x = s.copy()
            x.index = x.index + pd.Timedelta(days=days)
            return x.reindex(day).to_numpy()

        return pd.DataFrame({
            "hour": np.asarray(idx.hour),
            "weekday": np.asarray(idx.weekday),
            "doy_sin": np.sin(2 * np.pi * np.asarray(idx.dayofyear) / 365.25),
            "doy_cos": np.cos(2 * np.pi * np.asarray(idx.dayofyear) / 365.25),
            "lag_day": lag_h(load, L),
            "lag_day2": lag_h(load, L + 1),
            "lag_week": lag_h(load, 7),
            "mean_prev": lag_d(daily, L),
            # temperature forecast: same hour on the last known day (a weather forecast
            # would be better; this keeps the backtest honest)
            "temp_fc": lag_h(temp, L),
            "tmax_fc": lag_d(daily_t, L),
        }, index=idx)

    def fit(self, load: pd.Series, temp: pd.Series) -> LoadForecaster:
        X = self._features(load, temp)
        ok = X.notna().all(axis=1)
        self.model = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                                   l2_regularization=1.0, random_state=0)
        self.model.fit(X[ok], load[ok])
        return self

    def predict(self, load: pd.Series, temp: pd.Series) -> pd.Series:
        X = self._features(load, temp)
        X = X.fillna(X.median(numeric_only=True))
        return pd.Series(self.model.predict(X), index=load.index).clip(lower=0)
