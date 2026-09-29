"""A whole year for one house, under every strategy, using only information that
would have been available at the time.

Forecast models are trained on earlier years: solar on everything up to two
years before the test year, household load on the two years before it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .forecast import LoadForecaster, SolarForecaster
from .household import HouseholdSpec, simulate
from .optimizer import BatterySpec
from .pv import PVSystemSpec, pv_output
from .simulate import Inputs, run, summarise
from .tariff import Tariff

STRATEGIES = ("grid_only", "solar_only", "self_use", "planner_battery", "planner", "planner_perfect")


@dataclass
class Site:
    lat: float
    lon: float
    altitude: float
    weather: pd.DataFrame   # hourly history, common format


def build_inputs(site: Site, year: int, pv_spec: PVSystemSpec, battery: BatterySpec,
                 household: HouseholdSpec, tariff: Tariff) -> Inputs:
    w = site.weather.loc[: f"{year}-12-31"]
    w = w.loc[w.index[0].normalize() + pd.Timedelta(days=1):]   # drop the partial first day
    if w.loc[str(year)].empty or w.loc[: str(year - 2)].empty:
        raise ValueError(f"need weather from at least {year - 2} to {year}")
    ctx = w.loc[f"{year - 1}-12-20":]
    test = w.loc[str(year)]
    lat, lon, alt = site.lat, site.lon, site.altitude

    history = w.loc[: str(year - 2)]
    solar1 = SolarForecaster(lat, lon, alt, 1).fit(history)
    solar2 = SolarForecaster(lat, lon, alt, 2).fit(history)
    pv_fc1 = pv_output(solar1.predict_weather(ctx), pv_spec, lat, lon, alt).loc[str(year)]
    pv_fc2 = pv_output(solar2.predict_weather(ctx), pv_spec, lat, lon, alt).loc[str(year)]
    pv = pv_output(test, pv_spec, lat, lon, alt)

    house = simulate(w.index, w["temp"], household)["base"]
    train = slice(str(year - 2), str(year - 1))
    load1 = LoadForecaster(1).fit(house.loc[train], w["temp"].loc[train])
    load2 = LoadForecaster(2).fit(house.loc[train], w["temp"].loc[train])
    base_fc1 = load1.predict(house.loc[ctx.index], w["temp"].loc[ctx.index]).loc[str(year)]
    base_fc2 = load2.predict(house.loc[ctx.index], w["temp"].loc[ctx.index]).loc[str(year)]

    return Inputs(pv=pv, base=house.loc[str(year)], pv_fc1=pv_fc1, pv_fc2=pv_fc2, base_fc1=base_fc1,
                  base_fc2=base_fc2, tasks=household.tasks, tariff=tariff, battery=battery,
                  export_kw=pv_spec.inverter_kw)


def compare(inp: Inputs, strategies=STRATEGIES, progress=None) -> dict:
    """Run each strategy; returns {strategy: {summary..., monthly: {month: Rs}}}."""
    out = {}
    battery = inp.battery if inp.battery and inp.battery.capacity_kwh > 0 else None
    to_run = [s for s in strategies if not (battery is None and s in ("self_use", "planner_battery"))]
    for n, s in enumerate(to_run):
        def sub(frac, n=n, s=s):
            if progress:
                progress((n + frac) / len(to_run), s)
        res = run(inp, s, progress=sub)
        uses_battery = s not in ("grid_only", "solar_only")
        summary = summarise(res, inp.tariff, battery if uses_battery else None)
        price_in = inp.tariff.import_price(res.index)
        energy = res["grid_import"] * price_in - res["grid_export"] * inp.tariff.export_rate
        monthly = energy.groupby(res.index.month).sum()
        summary["monthly_energy_cost"] = {int(k): round(float(v), 0) for k, v in monthly.items()}
        out[s] = summary
        if progress:
            progress((n + 1) / len(to_run), s)
    return out
