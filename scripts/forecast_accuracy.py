"""Day-ahead forecast accuracy on a held-out year, against simple baselines.

    python scripts/forecast_accuracy.py --year 2024

Solar: model trained on every year up to two years before the test year.
Load: trained on the two years before it (simulated household, see household.py).
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solarplan.forecast import LoadForecaster, SolarForecaster, decompose, persistence  # noqa: E402
from solarplan.household import HouseholdSpec, simulate  # noqa: E402
from solarplan.pv import PVSystemSpec, clear_sky, pv_output  # noqa: E402
from solarplan.weather import fill_gaps, location_from_nasa, read_nasa_power  # noqa: E402

DEFAULT = Path(__file__).resolve().parent.parent / "data" / "nasa_power_lahore_2018_2025.csv.gz"


def solar_scores(pred: pd.Series, actual: pd.Series) -> tuple[float, float]:
    """Hourly RMSE (kW per kWp, daylight hours) and mean absolute daily energy error (%)."""
    day = actual > 0.01
    rmse = float(np.sqrt(((pred - actual)[day] ** 2).mean()))
    d_pred = pred.groupby(pred.index.normalize()).sum()
    d_act = actual.groupby(actual.index.normalize()).sum()
    return rmse, float((d_pred - d_act).abs().mean() / d_act.mean() * 100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--history", default=str(DEFAULT))
    args = ap.parse_args()
    year = args.year

    lat, lon, alt = location_from_nasa(args.history)
    w = fill_gaps(read_nasa_power(args.history)).loc[: f"{year}-12-31"]
    w = w.loc[w.index[0].normalize() + pd.Timedelta(days=1):]
    test = str(year)
    spec = PVSystemSpec(kwp=1, inverter_kw=1)
    actual = pv_output(w.loc[test], spec, lat, lon, alt)

    model = SolarForecaster(lat, lon, alt, 1).fit(w.loc[: str(year - 2)])
    ctx = w.loc[f"{year - 1}-12-20":]
    ml = pv_output(model.predict_weather(ctx), spec, lat, lon, alt).loc[test]

    pers = pv_output(w.shift(24).loc[f"{year - 1}-12-31":], spec, lat, lon, alt).loc[test]

    # climatology: average clear-sky index for that calendar month and hour in the training years
    cs = clear_sky(w.index, lat, lon, alt)["ghi"]
    kt = (w["ghi"] / cs.where(cs > 20)).clip(0, 1.2)
    train = kt.loc[: str(year - 2)]
    clim_kt = train.groupby([train.index.month, train.index.hour]).mean()
    idx = w.loc[test].index
    kt_clim = pd.Series(clim_kt.reindex(list(zip(idx.month, idx.hour, strict=True))).to_numpy(), index=idx).fillna(0)
    clim_w = decompose(kt_clim * cs.loc[test], w.loc[test], lat, lon, alt, 1)
    clim = pv_output(clim_w, spec, lat, lon, alt)

    print(f"Solar, next day, {year} (kW per kW of panels; daily error as % of the average day)")
    print(f"  {'':24s}{'hourly RMSE':>12s}{'daily error':>13s}")
    for name, p in [("Model", ml), ("Tomorrow = today", pers), ("Monthly average", clim)]:
        rmse, derr = solar_scores(p, actual)
        print(f"  {name:24s}{rmse:12.3f}{derr:12.1f}%")

    house = simulate(w.index, w["temp"], HouseholdSpec())["base"]
    tr = slice(str(year - 2), str(year - 1))
    load = LoadForecaster(1).fit(house.loc[tr], w["temp"].loc[tr])
    l_ml = load.predict(house.loc[ctx.index], w["temp"].loc[ctx.index]).loc[test]
    l_y = persistence(house, 1).loc[test]
    l_7 = sum(persistence(house, k) for k in range(1, 8)).loc[test] / 7
    real = house.loc[test]

    print(f"\nHousehold load, next day, {year} (mean absolute error as % of average load)")
    for name, p in [("Model", l_ml), ("Tomorrow = today", l_y), ("Last 7 days average", l_7)]:
        print(f"  {name:24s}{(p - real).abs().mean() / real.mean() * 100:12.1f}%")


if __name__ == "__main__":
    main()
