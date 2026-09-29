"""Cheapest plan for the next day or two: when to run flexible jobs, and what the
battery should do each hour.

Solved exactly as a small mixed-integer linear programme with SciPy's HiGHS
solver. Per hour t the variables are grid import, grid export, battery charge,
battery discharge, battery energy at the end of the hour and curtailed PV. Each
flexible job gets one binary per possible start hour.

    minimise   sum_t  price_in[t] * import[t] - price_out[t] * export[t]
                      + wear * (charge[t] + discharge[t])
               - value of energy left in the battery at the end

    subject to pv - curtailed + import + discharge = base load + jobs + charge + export
               energy[t] = energy[t-1] + eff_c * charge[t] - discharge[t] / eff_d
               every job starts exactly once, inside its window
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp


@dataclass
class BatterySpec:
    capacity_kwh: float = 10.0
    min_soc: float = 0.10          # hardware floor (fraction)
    max_soc: float = 1.00
    reserve: float = 0.0           # extra fraction kept for load-shedding, on top of min_soc
    charge_kw: float = 5.0
    discharge_kw: float = 5.0
    eff_charge: float = 0.95
    eff_discharge: float = 0.95
    wear_cost: float = 4.5         # Rs per kWh through the battery, each way
    grid_charging: bool = True

    @property
    def e_min(self) -> float:
        return self.capacity_kwh * min(self.max_soc, self.min_soc + self.reserve)

    @property
    def e_max(self) -> float:
        return self.capacity_kwh * self.max_soc


@dataclass
class JobWindow:
    """A flexible job for the optimiser: allowed start hours are horizon indices."""
    name: str
    kw: float
    hours: int
    starts: list  # allowed start indices
    day: int = 0  # which horizon day it belongs to (0 = the day being planned)
    usual: int | None = None   # usual start index; moving away from it costs `move_cost` per hour
    move_cost: float = 5.0     # Rs per hour moved, so routines only change when it pays


@dataclass
class Plan:
    grid_import: np.ndarray
    grid_export: np.ndarray
    charge: np.ndarray
    discharge: np.ndarray
    energy: np.ndarray            # kWh in the battery at the end of each hour
    curtailed: np.ndarray
    job_load: np.ndarray
    job_starts: dict              # name -> start index (per job, day 0 and 1)
    cost: float
    status: str


def solve(pv, base, price_in, price_out, battery: BatterySpec | None, energy0: float,
          jobs: list[JobWindow] | None = None, import_kw: float = 15.0, export_kw: float = 10.0,
          end_value: float | None = None) -> Plan:
    """Optimal plan over len(pv) hours. Arrays are per hour; energies in kWh."""
    pv = np.asarray(pv, float)
    base = np.asarray(base, float)
    price_in = np.asarray(price_in, float)
    price_out = np.asarray(price_out, float)
    H = len(pv)
    jobs = jobs or []
    has_batt = battery is not None and battery.capacity_kwh > 0

    # variable layout: [imp, exp, ch, dis, soc, curt] per hour, then job binaries
    IMP, EXP, CH, DIS, SOC, CURT = range(6)
    nv = 6 * H
    job_cols = []
    for job in jobs:
        cols = list(range(nv, nv + len(job.starts)))
        job_cols.append(cols)
        nv += len(job.starts)

    def v(kind, t):
        return kind * H + t

    c = np.zeros(nv)
    c[IMP * H:(IMP + 1) * H] = price_in
    c[EXP * H:(EXP + 1) * H] = -price_out
    if has_batt:
        # tiny preference for doing things earlier: when several hours cost the same the
        # plan uses the battery in one block instead of switching back and forth
        tie = 1e-3 * np.arange(H)
        c[CH * H:(CH + 1) * H] = battery.wear_cost + tie
        c[DIS * H:(DIS + 1) * H] = battery.wear_cost + tie
        if end_value is None:
            end_value = 0.0
        c[v(SOC, H - 1)] = -end_value

    lb = np.zeros(nv)
    ub = np.full(nv, np.inf)
    ub[IMP * H:(IMP + 1) * H] = import_kw
    ub[EXP * H:(EXP + 1) * H] = export_kw
    ub[CURT * H:(CURT + 1) * H] = pv
    if has_batt:
        ub[CH * H:(CH + 1) * H] = battery.charge_kw
        ub[DIS * H:(DIS + 1) * H] = battery.discharge_kw
        lb[SOC * H:(SOC + 1) * H] = battery.e_min
        ub[SOC * H:(SOC + 1) * H] = battery.e_max
    else:
        ub[CH * H:(DIS + 1) * H] = 0.0
        ub[SOC * H:(SOC + 1) * H] = 0.0
    for job, cols in zip(jobs, job_cols, strict=False):
        ub[cols] = 1.0
        if job.usual is not None:
            c[cols] += job.move_cost * np.abs(np.array(job.starts) - job.usual)

    rows, cols_, vals, lo, hi = [], [], [], [], []

    def add(row, col, val):
        rows.append(row)
        cols_.append(col)
        vals.append(val)

    def row_bounds(low, high):
        lo.append(low)
        hi.append(high)
        return len(lo)  # index of the next row

    r = 0
    # energy balance: imp + dis - ch - exp - curt - jobs = base - pv
    job_terms = [[] for _ in range(H)]
    for job, cols in zip(jobs, job_cols, strict=True):
        for col, s in zip(cols, job.starts, strict=True):
            for t in range(s, min(H, s + job.hours)):
                job_terms[t].append((col, job.kw))
    for t in range(H):
        for kind, coef in ((IMP, 1), (DIS, 1), (CH, -1), (EXP, -1), (CURT, -1)):
            add(r, v(kind, t), coef)
        for col, kw in job_terms[t]:
            add(r, col, -kw)
        r = row_bounds(base[t] - pv[t], base[t] - pv[t])
    # battery energy
    if has_batt:
        for t in range(H):
            add(r, v(SOC, t), 1.0)
            add(r, v(CH, t), -battery.eff_charge)
            add(r, v(DIS, t), 1.0 / battery.eff_discharge)
            if t > 0:
                add(r, v(SOC, t - 1), -1.0)
            rhs = energy0 if t == 0 else 0.0
            r = row_bounds(rhs, rhs)
        if not battery.grid_charging:
            # charge only from PV: charge + curtailed <= pv
            for t in range(H):
                add(r, v(CH, t), 1.0)
                add(r, v(CURT, t), 1.0)
                r = row_bounds(-np.inf, pv[t])
    # each job starts exactly once
    for cols in job_cols:
        for col in cols:
            add(r, col, 1.0)
        r = row_bounds(1.0, 1.0)

    A = sparse.csr_matrix((vals, (rows, cols_)), shape=(r, nv))
    integrality = np.zeros(nv)
    for cols in job_cols:
        integrality[cols] = 1
    res = milp(c, constraints=LinearConstraint(A, lo, hi), bounds=Bounds(lb, ub), integrality=integrality,
               options={"time_limit": 20})
    if res.x is None:
        raise RuntimeError(f"optimiser failed: {res.message}")
    x = res.x

    def seg(kind):
        return x[kind * H:(kind + 1) * H].copy()

    job_load = np.zeros(H)
    starts = {}
    for job, cols in zip(jobs, job_cols, strict=False):
        chosen = job.starts[int(np.argmax(x[cols]))]
        starts[(job.name, job.day)] = chosen
        job_load[chosen:chosen + job.hours] += job.kw
    return Plan(seg(IMP), seg(EXP), seg(CH), seg(DIS), seg(SOC), seg(CURT), job_load, starts,
                float(res.fun), res.message)


def hour_modes(plan: Plan, pv, base, battery: BatterySpec | None = None, energy0: float = 0.0) -> list[str]:
    """Translate the battery schedule into instructions a hybrid inverter understands."""
    modes = []
    for t in range(len(pv)):
        surplus = pv[t] - base[t] - plan.job_load[t]
        from_grid = plan.charge[t] - max(0.0, surplus)
        start = energy0 if t == 0 else plan.energy[t - 1]
        empty = battery is None or battery.capacity_kwh <= 0 or start <= battery.e_min + 0.05
        if from_grid > 0.05:
            modes.append("charge_grid")
        elif plan.discharge[t] > 0.05:
            modes.append("use_battery")
        elif plan.charge[t] > 0.05:
            modes.append("charge_solar")
        elif surplus < -0.05:
            modes.append("grid" if empty else "hold")
        else:
            modes.append("idle")
    return modes
