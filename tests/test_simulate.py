import numpy as np
import pandas as pd
import pytest

from solarplan.household import Task
from solarplan.optimizer import BatterySpec
from solarplan.simulate import Inputs, run, step, summarise
from solarplan.tariff import Tariff

B = BatterySpec(capacity_kwh=10)


def test_battery_never_exports_to_the_grid():
    # house needs 1 kW, battery full and allowed to discharge: it covers the house only
    imp, exp, ch, dis, cur, e = step(pv=0.0, load=1.0, energy=9.0, battery=B, allow_discharge=True,
                                     grid_charge=0.0, import_kw=15, export_kw=10)
    assert dis == pytest.approx(1.0) and exp == 0 and imp == 0


def test_hold_means_grid_covers_the_house():
    imp, exp, ch, dis, cur, e = step(0.0, 2.0, 9.0, B, False, 0.0, 15, 10)
    assert dis == 0 and imp == pytest.approx(2.0) and e == 9.0


def test_spare_solar_charges_first_then_exports():
    imp, exp, ch, dis, cur, e = step(8.0, 1.0, 5.0, B, True, 0.0, 15, 10)
    assert ch == pytest.approx(5.0)          # limited by charge power
    assert exp == pytest.approx(2.0)
    assert e == pytest.approx(5.0 + 5.0 * B.eff_charge)


def test_charge_is_limited_by_space_left():
    imp, exp, ch, dis, cur, e = step(8.0, 1.0, 9.9, B, True, 0.0, 15, 10)
    assert e == pytest.approx(B.e_max)
    assert ch < 0.2


def three_days():
    idx = pd.date_range("2024-06-10", periods=72, freq="h", tz="Asia/Karachi")
    h = np.asarray(idx.hour)
    pv = pd.Series(np.clip(6 * np.sin(np.pi * (h - 6) / 12), 0, None), index=idx)
    base = pd.Series(np.where((h >= 19) & (h < 23), 3.0, 1.0), index=idx)
    return idx, pv, base


def make_inputs(noise=0.0):
    idx, pv, base = three_days()
    rng = np.random.default_rng(0)
    fc = lambda s: (s * (1 + noise * rng.normal(size=len(s)))).clip(lower=0)  # noqa: E731
    return Inputs(pv=pv, base=base, pv_fc1=fc(pv), pv_fc2=fc(pv), base_fc1=fc(base), base_fc2=fc(base),
                  tasks=[Task("Pump", 0.75, 1, earliest=6, latest_end=20, habit_start=7)],
                  tariff=Tariff(), battery=B)


def total(inp, strategy):
    res = run(inp, strategy)
    pv = 0 if strategy == "grid_only" else inp.pv   # no panels in that case
    bal = pv + res.grid_import + res.discharge - (res.load + res.charge + res.grid_export + res.curtailed)
    assert bal.abs().max() < 1e-9
    return summarise(res, inp.tariff, None if strategy in ("grid_only", "solar_only") else inp.battery)


def test_planner_beats_standard_mode_with_good_forecasts():
    inp = make_inputs()
    standard = total(inp, "self_use")["total_with_wear"]
    planned = total(inp, "planner")["total_with_wear"]
    assert planned < standard


def test_strategies_rank_sensibly():
    inp = make_inputs(noise=0.1)
    grid = total(inp, "grid_only")["total"]
    solar = total(inp, "solar_only")["total"]
    assert solar < grid
    assert total(inp, "planner_perfect")["total_with_wear"] <= total(inp, "planner")["total_with_wear"] + 1


def test_jobs_run_every_day():
    inp = make_inputs()
    res = run(inp, "planner")
    daily = res["job_load"].groupby(res.index.date).sum()
    assert (daily == 0.75).all()
