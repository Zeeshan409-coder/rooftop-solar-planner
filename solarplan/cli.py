"""Command line.

    python -m solarplan plan --place multan --pv-kw 10 --battery-kwh 10 --battery-now 60
    python -m solarplan plan --place "dera ismail khan" --start now    # rest of today, live
    python -m solarplan plan --lat 35.92 --lon 74.31 --rate 12         # any point, own price
    python -m solarplan plan --date 2024-06-15                         # replay a past day
    python -m solarplan plan --load-csv my_usage.csv                   # your own meter readings
    python -m solarplan backtest --place sukkur --year 2024 --pv-kw 6 --battery-kwh 5
    python -m solarplan places sindh                                   # list districts
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from .backtest import STRATEGIES, Site, build_inputs, compare
from .history import load_history
from .household import HouseholdSpec
from .locations import all_locations, custom, find
from .optimizer import BatterySpec
from .planner import SiteConfig, forecast_weather, load_from_history, plan_day
from .pv import PVSystemSpec
from .tariff import EXPORT_RATES, SUPPLIERS, tariff_for

LABELS = {
    "grid_only": "No solar",
    "solar_only": "Solar, no battery",
    "self_use": "Solar + battery, standard mode",
    "planner_battery": "Planner, battery only",
    "planner": "Planner",
    "planner_perfect": "Planner with perfect forecasts",
}


def _system_args(ap):
    where = ap.add_argument_group("where")
    where.add_argument("--place", default="lahore", help="district or town, e.g. multan, 'tando allahyar', skardu")
    where.add_argument("--lat", type=float, help="any point instead of a district (with --lon)")
    where.add_argument("--lon", type=float)
    where.add_argument("--supplier", choices=sorted(SUPPLIERS), help="defaults to the one serving the place")
    where.add_argument("--rate", type=float, help="flat price per unit, for AJK / Gilgit-Baltistan")
    where.add_argument("--export", choices=list(EXPORT_RATES), default="net_billing")
    system = ap.add_argument_group("system")
    system.add_argument("--pv-kw", type=float, default=10.0, help="panel size, kW")
    system.add_argument("--inverter-kw", type=float, help="defaults to the panel size (min 5)")
    system.add_argument("--tilt", type=float, default=25.0)
    system.add_argument("--azimuth", type=float, default=180.0, help="180 = south")
    system.add_argument("--battery-kwh", type=float, default=10.0, help="0 for no battery")
    system.add_argument("--reserve", type=float, default=0.0, help="battery %% kept for load-shedding")
    system.add_argument("--acs", type=int, default=2, help="number of air conditioners")


def _config(args) -> SiteConfig:
    try:
        if args.lat is not None and args.lon is not None:
            loc = custom(args.lat, args.lon)
        else:
            loc = find(args.place)
        supplier = args.supplier or loc.supplier
        tariff = tariff_for(supplier, args.export, args.rate)
    except (KeyError, ValueError) as e:
        sys.exit(str(e).strip("'\""))
    pv = PVSystemSpec(kwp=args.pv_kw, tilt=args.tilt, azimuth=args.azimuth,
                      inverter_kw=args.inverter_kw or max(args.pv_kw, 5.0))
    battery = BatterySpec(capacity_kwh=args.battery_kwh, reserve=args.reserve / 100)
    return SiteConfig.for_place(loc, pv=pv, battery=battery, household=HouseholdSpec(n_ac=args.acs), tariff=tariff)


def _history(cfg: SiteConfig):
    print(f"loading weather history for {cfg.name}...", file=sys.stderr)
    try:
        return load_history(cfg.location)
    except OSError as e:
        sys.exit(str(e))


def cmd_plan(args):
    cfg = _config(args)
    cfg.battery_now = args.battery_now / 100
    if args.date:
        weather, elevation, hist_source = _history(cfg)
        start = pd.Timestamp(args.date).tz_localize(cfg.tz)
        weather = weather.loc[start:].iloc[:48]
        if len(weather) < 24 or weather["ghi"].iloc[:24].isna().any():
            sys.exit(f"no complete weather for {args.date}")
        weather.attrs["elevation"] = elevation
        source = f"replay of {args.date}, actual weather from {hist_source}"
    else:
        try:
            weather = forecast_weather(cfg, args.start)
        except (OSError, ValueError) as e:
            sys.exit(f"could not fetch the weather forecast ({e}); try --date YYYY-MM-DD to replay a past day")
        source = "Open-Meteo forecast"
    base = None
    if args.load_csv:
        meter = pd.read_csv(args.load_csv, index_col=0, parse_dates=True).iloc[:, 0]
        if meter.index.tz is None:
            meter.index = meter.index.tz_localize(cfg.tz)
        meter = meter.tz_convert(cfg.tz).resample("h").mean().dropna()
        meter = meter.loc[: weather.index[0] - pd.Timedelta(hours=1)]
        if meter.empty:
            sys.exit(f"{args.load_csv} has no readings before {weather.index[0]:%Y-%m-%d}")
        base = load_from_history(meter, weather.index)
    plan = plan_day(cfg, weather, base)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return
    loc = cfg.location
    print(f"{loc.label}, {plan.date} from {plan.start}  ({source})")
    print(f"electricity: {SUPPLIERS[cfg.tariff.supplier].name}")
    peak = f"peak hours {plan.peak_hours[0]}-{plan.peak_hours[1]}" if plan.peak_hours else "flat rate, no peak hours"
    print(f"solar {plan.solar_kwh} kWh, {plan.self_use_pct}% used at home; {peak}")
    print(f"expected energy cost Rs {plan.cost_plan:,.0f}  (usual routine: Rs {plan.cost_usual:,.0f})\n")
    for line in plan.instructions:
        print("  " + line)


def cmd_backtest(args):
    cfg = _config(args)
    weather, elevation, source = _history(cfg)
    site = Site(cfg.lat, cfg.lon, elevation, weather)
    try:
        inp = build_inputs(site, args.year, cfg.pv, cfg.battery, cfg.household, cfg.tariff)
    except ValueError as e:
        sys.exit(str(e))

    def progress(frac, name):
        print(f"\r  {name:16s} {frac * 100:5.1f}%", end="", file=sys.stderr, flush=True)

    res = compare(inp, [s for s in STRATEGIES if s in args.strategies.split(",")], progress)
    print(file=sys.stderr)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)
    print(f"{cfg.location.label}, {args.year} ({source} weather), {args.pv_kw:g} kW panels, "
          f"{args.battery_kwh:g} kWh battery, {SUPPLIERS[cfg.tariff.supplier].name}\n")
    print(f"{'':32s}{'bill':>12s}{'battery wear':>14s}{'total':>12s}")
    for s, r in res.items():
        print(f"{LABELS[s]:32s}{r['total']:>12,.0f}{r['wear_cost']:>14,.0f}{r['total_with_wear']:>12,.0f}")


def cmd_places(args):
    q = (args.filter or "").lower()
    for loc in all_locations():
        if q in f"{loc.province} {loc.division} {loc.district} {loc.town} {loc.supplier}".lower():
            print(f"{loc.province:28s} {loc.division:20s} {loc.district:24s} {loc.supplier}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="solarplan", description="Rooftop solar and battery planner for Pakistan")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="plan the next day (or replay a past one)")
    p.add_argument("--start", choices=["tomorrow", "now"], default="tomorrow",
                   help="plan all of tomorrow, or the next 24 hours from now")
    p.add_argument("--battery-now", type=float, default=50.0, help="battery charge now, %%")
    p.add_argument("--date", help="replay a past day from weather history instead of the live forecast")
    p.add_argument("--load-csv", help="your own hourly usage: CSV of timestamp, kW (excluding the flexible jobs)")
    p.add_argument("--json", action="store_true")
    _system_args(p)
    p.set_defaults(func=cmd_plan)

    b = sub.add_parser("backtest", help="simulate a whole year under every strategy")
    b.add_argument("--year", type=int, default=2024)
    b.add_argument("--strategies", default=",".join(STRATEGIES))
    b.add_argument("--out")
    _system_args(b)
    b.set_defaults(func=cmd_backtest)

    ls = sub.add_parser("places", help="list districts, divisions and their electricity suppliers")
    ls.add_argument("filter", nargs="?", help="e.g. a province, division or supplier name")
    ls.set_defaults(func=cmd_places)

    args = ap.parse_args(argv)
    args.func(args)
