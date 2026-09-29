"""Hour-by-hour simulation of a home over a period, under different strategies.

Strategies:
  grid_only     no solar at all (reference bill)
  solar_only    solar panels, no battery, appliances at their usual times
  self_use      solar + battery in the standard inverter mode: store spare solar,
                use the battery whenever the house needs power, never charge from grid
  planner       day-ahead schedule for flexible jobs from forecasts, then the battery
                is re-planned every hour (model predictive control) using forecasts only
  planner_perfect  the same planner given the actual future, an upper bound
  planner_battery  the planner, but flexible jobs stay at their usual times (so the
                   difference to `planner` is what moving the jobs is worth)

The planner never sees the actual values when deciding. Its decisions are turned
into inverter modes (use battery / hold / charge from grid) and those modes are
then applied to what really happens that hour, so forecast errors cost money the
way they would in real life.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .household import Task, habit_load
from .optimizer import BatterySpec, JobWindow, solve
from .tariff import Tariff


@dataclass
class Inputs:
    pv: pd.Series             # actual PV, kW
    base: pd.Series           # actual base load, kW
    pv_fc1: pd.Series         # PV forecast made the night before
    pv_fc2: pd.Series         # PV forecast made two nights before
    base_fc1: pd.Series
    base_fc2: pd.Series
    tasks: list
    tariff: Tariff
    battery: BatterySpec
    import_kw: float = 15.0
    export_kw: float = 10.0


def step(pv: float, load: float, energy: float, battery: BatterySpec, allow_discharge: bool,
         grid_charge: float, import_kw: float, export_kw: float):
    """Apply one hour. Returns (import, export, charge, discharge, curtailed, new energy)."""
    b = battery
    has = b is not None and b.capacity_kwh > 0
    max_in = min(b.charge_kw, max(0.0, (b.e_max - energy) / b.eff_charge)) if has else 0.0
    max_out = min(b.discharge_kw, max(0.0, (energy - b.e_min) * b.eff_discharge)) if has else 0.0
    surplus = pv - load
    charge = discharge = 0.0
    if surplus >= 0:
        from_pv = min(max_in, surplus)
        from_grid = min(max_in - from_pv, max(0.0, grid_charge))
        charge = from_pv + from_grid
        spill = surplus - from_pv
        export = min(export_kw, spill)
        curtailed = spill - export
        grid_import = from_grid
    else:
        need = -surplus
        if allow_discharge:
            discharge = min(max_out, need)
        from_grid = min(max_in, max(0.0, grid_charge))
        charge = from_grid
        grid_import = need - discharge + from_grid
        export = curtailed = 0.0
    if grid_import > import_kw:  # keep it physical; shortfall would be load-shedding
        grid_import = import_kw
    energy = energy + charge * (b.eff_charge if has else 0) - discharge / (b.eff_discharge if has else 1)
    return grid_import, export, charge, discharge, curtailed, energy


def _job_windows(day: pd.Timestamp, offset: int, tasks: list[Task], day_no: int) -> list[JobWindow]:
    out = []
    for task in tasks:
        if not task.runs_on(day):
            continue
        starts = [offset + h for h in range(task.earliest, task.latest_end - task.hours + 1)]
        out.append(JobWindow(task.name, task.kw, task.hours, starts, day=day_no, usual=offset + task.habit_start))
    return out


def run(inp: Inputs, strategy: str, energy0: float | None = None, progress=None) -> pd.DataFrame:
    idx = inp.pv.index
    b = inp.battery
    tariff = inp.tariff
    price_in = tariff.import_price(idx)
    price_out = tariff.export_price(idx)
    energy = energy0 if energy0 is not None else (b.e_min + b.e_max) / 2 if b else 0.0
    habits = habit_load(idx, inp.tasks)
    rows = []

    if strategy in ("grid_only", "solar_only", "self_use"):
        for t, _ts in enumerate(idx):
            load = inp.base.iat[t] + habits.iat[t]
            if strategy == "grid_only":
                rows.append((load, 0.0, 0.0, 0.0, 0.0, energy, load, habits.iat[t], "grid"))
                continue
            batt = b if strategy == "self_use" else None
            imp, exp, ch, dis, cur, energy_new = step(inp.pv.iat[t], load, energy, batt, True, 0.0,
                                                      inp.import_kw, inp.export_kw)
            if batt:
                energy = energy_new
            rows.append((imp, exp, ch, dis, cur, energy, load, habits.iat[t], "self_use" if batt else "solar"))
        return _frame(rows, idx)

    perfect = strategy == "planner_perfect"
    move_jobs = strategy != "planner_battery"
    end_value = float(price_in.min()) * b.eff_discharge - b.wear_cost
    days = pd.DatetimeIndex(sorted(set(idx.normalize())))
    pos = {ts: i for i, ts in enumerate(idx)}
    for d_no, day in enumerate(days):
        if progress:
            progress(d_no / len(days))
        hours = pd.date_range(day, periods=48, freq="h")
        present = [h for h in hours if h in pos]
        n1 = sum(1 for h in present if h.normalize() == day)
        if n1 == 0:
            continue
        sel = [pos[h] for h in present]
        is_day1 = np.array([h.normalize() == day for h in present])
        if perfect:
            pv_fc = inp.pv.iloc[sel].to_numpy()
            base_fc = inp.base.iloc[sel].to_numpy()
        else:
            pv_fc = np.where(is_day1, inp.pv_fc1.iloc[sel], inp.pv_fc2.iloc[sel])
            base_fc = np.where(is_day1, inp.base_fc1.iloc[sel], inp.base_fc2.iloc[sel])
        pin = price_in.iloc[sel].to_numpy()
        pout = price_out.iloc[sel].to_numpy()

        jobs = _job_windows(day, 0, inp.tasks, 0)
        if len(present) > 24:
            jobs += _job_windows(day + pd.Timedelta(days=1), 24, inp.tasks, 1)
        jobs = [JobWindow(j.name, j.kw, j.hours, [s for s in j.starts if s + j.hours <= len(present)], j.day,
                          j.usual) for j in jobs]
        jobs = [j for j in jobs if j.starts]
        if not move_jobs:  # pin every job to its usual start hour
            habit = {t.name: t.habit_start for t in inp.tasks}
            jobs = [JobWindow(j.name, j.kw, j.hours, [24 * j.day + habit[j.name]], j.day, j.usual) for j in jobs
                    if 24 * j.day + habit[j.name] + j.hours <= len(present)]
        ahead = solve(pv_fc, base_fc, pin, pout, b, energy, jobs, inp.import_kw, inp.export_kw, end_value)
        job_load = ahead.job_load  # fixed for the rest of the day

        for t in range(n1):
            if t == 0:
                plan = ahead
            else:
                plan = solve(pv_fc[t:], base_fc[t:] + job_load[t:], pin[t:], pout[t:], b, energy, None,
                             inp.import_kw, inp.export_kw, end_value)
            # the first hour of each plan is the decision for this hour
            load_fc = base_fc[t] + job_load[t]
            grid_charge = max(0.0, plan.charge[0] - max(0.0, pv_fc[t] - load_fc))
            allow = plan.discharge[0] > 0.01
            i = sel[t]
            load = inp.base.iat[i] + job_load[t]
            imp, exp, ch, dis, cur, energy = step(inp.pv.iat[i], load, energy, b, allow, grid_charge,
                                                  inp.import_kw, inp.export_kw)
            mode = "charge_grid" if grid_charge > 0.05 else ("use_battery" if allow else "hold")
            rows.append((imp, exp, ch, dis, cur, energy, load, job_load[t], mode))
    return _frame(rows, idx)


def _frame(rows, idx) -> pd.DataFrame:
    return pd.DataFrame(rows, index=idx[:len(rows)], columns=[
        "grid_import", "grid_export", "charge", "discharge", "curtailed", "battery_kwh", "load", "job_load", "mode"])


def summarise(result: pd.DataFrame, tariff: Tariff, battery: BatterySpec | None) -> dict:
    bill = tariff.bill(result["grid_import"], result["grid_export"])
    throughput = float(result["charge"].sum() + result["discharge"].sum())
    wear = throughput * (battery.wear_cost if battery else 0.0)
    return {**bill, "battery_throughput_kwh": throughput, "wear_cost": wear,
            "total_with_wear": bill["total"] + wear}
