"""A day's plan for one home, in plain language.

    config = SiteConfig.for_place("Multan")
    plan = plan_day(config, forecast_weather(config))   # needs internet
    for line in plan.instructions: print(line)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from .household import HouseholdSpec, Task, habit_load, simulate
from .locations import Location, find
from .optimizer import BatterySpec, JobWindow, hour_modes, solve
from .pv import PVSystemSpec, pv_output
from .simulate import step
from .tariff import Tariff, tariff_for
from .weather import fetch_forecast

MODE_TEXT = {
    "use_battery": "Run the house from the battery",
    "hold": "Keep the battery for later, use the grid",
    "charge_solar": "Charge the battery from solar",
    "charge_grid": "Charge the battery from the grid",
    "idle": "Solar covers the house",
    "grid": "Battery is empty, use the grid",
}


@dataclass
class SiteConfig:
    name: str = "Lahore"
    lat: float = 31.549
    lon: float = 74.344
    altitude: float = 0.0           # metres; filled in from the weather data when known
    tz: str = "Asia/Karachi"
    pv: PVSystemSpec = field(default_factory=PVSystemSpec)
    battery: BatterySpec = field(default_factory=BatterySpec)
    household: HouseholdSpec = field(default_factory=HouseholdSpec)
    tariff: Tariff = field(default_factory=Tariff)
    battery_now: float = 0.5        # state of charge right now, fraction
    location: Location | None = None

    @classmethod
    def for_place(cls, place: str | Location, export: str = "net_billing", rate: float | None = None,
                  **kw) -> SiteConfig:
        """Config for a district (by name) or a Location, with its supplier's tariff."""
        loc = place if isinstance(place, Location) else find(place)
        if "tariff" not in kw:
            kw["tariff"] = tariff_for(loc.supplier, export, rate)
        return cls(name=loc.district, lat=loc.lat, lon=loc.lon, location=loc, **kw)


def forecast_weather(config: SiteConfig, start: str = "tomorrow") -> pd.DataFrame:
    """The next 48 hours of forecast weather from Open-Meteo, starting at midnight
    tonight (`start="tomorrow"`) or at the start of the current hour (`start="now"`)."""
    fc = fetch_forecast(config.lat, config.lon, days=3, tz=config.tz)
    now = pd.Timestamp.now(tz=config.tz).floor("h")
    begin = now if start == "now" else (now + pd.Timedelta(days=1)).normalize()
    out = fc.loc[begin:begin + pd.Timedelta(hours=47)]
    out.attrs["elevation"] = fc.attrs.get("elevation", 0.0)
    return out


def expected_load(index: pd.DatetimeIndex, temp: pd.Series, household: HouseholdSpec, samples: int = 16) -> pd.Series:
    """Average of several simulated days: the expected base load for these hours."""
    runs = [simulate(index, temp, household, seed=s)["base"] for s in range(samples)]
    return sum(runs) / samples


def load_from_history(history: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Forecast from the household's own meter readings: median of the last two weeks
    for each hour of the day, separately for weekdays and weekends."""
    recent = history.loc[history.index >= history.index.max() - pd.Timedelta(days=14)]
    key = pd.MultiIndex.from_arrays([recent.index.weekday >= 5, recent.index.hour])
    prof = recent.groupby(key).median()
    out = [prof.get((ts.weekday() >= 5, ts.hour), recent.median()) for ts in index]
    return pd.Series(out, index=index, dtype=float)


@dataclass
class DayPlan:
    date: str
    start: str                  # clock time of the first planned hour
    hours: list                 # per-hour rows for the planned day
    jobs: list                  # flexible jobs with planned and usual times
    instructions: list          # plain-language lines
    cost_plan: float            # expected cost of the day, Rs: energy plus battery wear
    cost_usual: float           # same hours with the usual routine and standard battery mode
    solar_kwh: float
    self_use_pct: float
    peak_hours: tuple | None    # None for a flat-rate supplier

    def to_dict(self) -> dict:
        return asdict(self)


def _clock(hours: float) -> str:
    h = int(hours) % 24
    return f"{h:02d}:{round((hours - int(hours)) * 60):02d}"


def plan_day(config: SiteConfig, weather: pd.DataFrame, base: pd.Series | None = None) -> DayPlan:
    """Plan the first 24 hours of `weather` (which should cover 24 to 48 hours).

    The weather may start at midnight (a plan for the whole day) or at any hour (a
    plan for the rest of today and tonight). Flexible jobs whose usual time today
    has already passed are taken as done."""
    idx = weather.index
    altitude = weather.attrs.get("elevation", config.altitude)
    pv = pv_output(weather, config.pv, config.lat, config.lon, altitude)
    if base is None:
        base = expected_load(idx, weather["temp"], config.household)
    base = base.reindex(idx).interpolate(limit_direction="both")
    t = config.tariff
    price_in = t.import_price(idx)
    price_out = t.export_price(idx)
    b = config.battery
    energy0 = b.capacity_kwh * config.battery_now

    def at(h: int) -> str:              # hour offset -> clock time
        return (idx[0] + pd.Timedelta(hours=h)).strftime("%H:%M")

    tasks: list[Task] = config.household.tasks
    day0 = idx[0].normalize()
    first = idx[0].hour                  # offset of the first hour from midnight
    jobs = []
    for d in range(3):
        day = day0 + pd.Timedelta(days=d)
        for task in tasks:
            if not task.runs_on(day) or (d == 0 and task.habit_start < first):
                continue
            starts = [24 * d + h - first for h in range(task.earliest, task.latest_end - task.hours + 1)]
            starts = [s for s in starts if s >= 0 and s + task.hours <= len(idx)]
            if starts:
                jobs.append(JobWindow(task.name, task.kw, task.hours, starts, day=d,
                                      usual=24 * d + task.habit_start - first))
    end_value = float(price_in.min()) * b.eff_discharge - b.wear_cost
    plan = solve(pv.to_numpy(), base.to_numpy(), price_in.to_numpy(), price_out.to_numpy(), b, energy0,
                 jobs, export_kw=config.pv.inverter_kw, end_value=end_value)
    modes = hour_modes(plan, pv.to_numpy(), base.to_numpy(), b, energy0)

    n = min(24, len(idx))
    peak = t.is_peak(idx[:n])
    hours = []
    for h in range(n):
        hours.append({
            "hour": h,
            "time": idx[h].strftime("%H:%M"),
            "date": idx[h].strftime("%Y-%m-%d"),
            "solar": round(float(pv.iat[h]), 3),
            "load": round(float(base.iat[h]), 3),
            "jobs": round(float(plan.job_load[h]), 3),
            "charge": round(float(plan.charge[h]), 3),
            "discharge": round(float(plan.discharge[h]), 3),
            "grid_import": round(float(plan.grid_import[h]), 3),
            "grid_export": round(float(plan.grid_export[h]), 3),
            "battery_pct": round(100 * float(plan.energy[h]) / b.capacity_kwh, 1) if b.capacity_kwh else 0.0,
            "price": round(float(price_in.iat[h]), 2),
            "peak": bool(peak[h]),
            "mode": modes[h],
        })

    # what each move is worth: re-plan with that one job pinned to its usual time
    job_rows = []
    for job in jobs:
        s = plan.job_starts.get((job.name, job.day))
        if s is None or s >= n:
            continue
        saving = 0.0
        if s != job.usual and job.usual in job.starts:
            pinned = [JobWindow(j.name, j.kw, j.hours, [j.usual], j.day, j.usual, j.move_cost) if j is job else j
                      for j in jobs]
            alt = solve(pv.to_numpy(), base.to_numpy(), price_in.to_numpy(), price_out.to_numpy(), b, energy0,
                        pinned, export_kw=config.pv.inverter_kw, end_value=end_value)
            # add back this job's own moving penalty: it isn't money
            saving = max(0.0, alt.cost - plan.cost + abs(s - job.usual) * job.move_cost)
        job_rows.append({
            "name": job.name, "kw": job.kw, "hours": job.hours, "offset": s,
            "start": at(s), "end": at(s + job.hours), "usual_start": at(job.usual),
            "saving": round(saving, 0),
        })
    job_rows.sort(key=lambda j: j["offset"])

    # Energy still in the battery after 24 hours is worth what it will replace
    # during the next day's dearest hours, so both costs are net of the change in
    # stored energy, valued that way.
    later = price_in.to_numpy()[n:]
    kwh_value = max(float(later.max()) * b.eff_discharge - b.wear_cost if len(later) else end_value, 0.0)
    energy_end = float(plan.energy[n - 1]) if b.capacity_kwh > 0 else 0.0
    # Battery wear counts too: the optimiser weighs it, so the comparison must as well.
    wear = b.wear_cost if b.capacity_kwh > 0 else 0.0
    cost_plan = float((plan.grid_import[:n] * price_in.to_numpy()[:n]).sum()
                      - (plan.grid_export[:n] * price_out.to_numpy()[:n]).sum()
                      + wear * (plan.charge[:n] + plan.discharge[:n]).sum()) - (energy_end - energy0) * kwh_value
    cost_usual = _usual_cost(config, pv.iloc[:n], base.iloc[:n], price_in.iloc[:n], price_out.iloc[:n], energy0,
                             kwh_value, tasks, first)

    lines = []
    for job in job_rows:
        text = f"{job['start']}-{job['end']}  {job['name']}"
        if job["start"] != job["usual_start"] and job["saving"] > 0:
            text += f" (instead of {job['usual_start']}, saves about Rs {job['saving']:.0f})"
        lines.append((job["offset"], 1, text))
    run_start = 0
    for h in range(1, n + 1):
        if h == n or modes[h] != modes[run_start]:
            lines.append((run_start, 0, f"{at(run_start)}-{at(h)}  {MODE_TEXT[modes[run_start]]}"))
            run_start = h
    instructions = [text for _, _, text in sorted(lines)]

    solar = float(pv.iloc[:n].sum())
    exported = float(plan.grid_export[:n].sum())
    if t.flat:
        peak_hours = None
    else:
        ps, pe = t.peak_hours[idx[0].month]
        peak_hours = (_clock(ps), _clock(pe))
    return DayPlan(
        date=idx[0].strftime("%Y-%m-%d"), start=idx[0].strftime("%H:%M"), hours=hours, jobs=job_rows,
        instructions=instructions, cost_plan=round(cost_plan, 0), cost_usual=round(cost_usual, 0),
        solar_kwh=round(solar, 1), self_use_pct=round(100 * (solar - exported) / solar, 1) if solar > 0 else 0.0,
        peak_hours=peak_hours,
    )


def _usual_cost(config, pv, base, price_in, price_out, energy0, kwh_value=0.0, tasks=None, first=0) -> float:
    """The same hours with jobs at their usual times and the battery in standard mode."""
    tasks = config.household.tasks if tasks is None else tasks
    b = config.battery
    wear = b.wear_cost if b is not None and b.capacity_kwh > 0 else 0.0
    habits = habit_load(pv.index, tasks)
    if first:
        # a job that started before the first hour is already running or done
        today = habits.index.normalize() == pv.index[0].normalize()
        later = habit_load(pv.index, [task for task in tasks if task.habit_start >= first])
        habits[today] = later[today]
    energy, cost = energy0, 0.0
    for t in range(len(pv)):
        imp, exp, ch, dis, _, energy = step(pv.iat[t], base.iat[t] + habits.iat[t], energy, config.battery, True,
                                            0.0, 15.0, config.pv.inverter_kw)
        cost += imp * price_in.iat[t] - exp * price_out.iat[t] + wear * (ch + dis)
    return cost - (energy - energy0) * kwh_value
