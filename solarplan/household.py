"""A simulated household, for when there is no meter data.

Bottom-up: every appliance has a power rating and a usage pattern, and the air
conditioners and fans respond to the actual outdoor temperature of that hour. The
defaults describe a typical 10-marla family house in Lahore with two inverter ACs.

Two kinds of load come out of this:
  * base load: what the family needs when they need it (cooling, lights, fridge...)
  * flexible tasks: jobs that must happen once a day or a few times a week, but
    whose timing is up to us: filling the water tank, washing, ironing, charging.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Task:
    """A job that has to run once, for `hours` hours in a row, inside a time window."""
    name: str
    kw: float
    hours: int
    earliest: int = 6          # first allowed start hour
    latest_end: int = 22       # must be finished by this hour
    days: tuple = (0, 1, 2, 3, 4, 5, 6)   # weekdays it runs on (Mon = 0)
    habit_start: int = 8       # when the family usually runs it

    def runs_on(self, day: pd.Timestamp) -> bool:
        return day.weekday() in self.days

    def energy(self) -> float:
        return self.kw * self.hours


@dataclass
class HouseholdSpec:
    n_ac: int = 2
    ac_kw: float = 1.3            # 1.5-ton inverter AC at full cooling
    ac_start_temp: float = 31.0   # outdoor C above which ACs get switched on
    n_fans: int = 6
    fan_kw: float = 0.075
    fridge_kw: float = 0.10       # average draw at 20 C outside
    lights_kw: float = 0.25       # all LED lights on
    electronics_kw: float = 0.18  # TV, laptops, router, chargers in the evening
    standby_kw: float = 0.05
    heater_kw: float = 0.0        # electric room heater in winter evenings (0 = gas heating)
    tasks: list = field(default_factory=lambda: [
        Task("Water pump", kw=0.75, hours=1, earliest=6, latest_end=20, habit_start=7),
        Task("Washing machine", kw=0.5, hours=2, earliest=8, latest_end=20, days=(0, 3, 5), habit_start=10),
        Task("Iron", kw=1.0, hours=1, earliest=7, latest_end=22, habit_start=7),
    ])


def simulate(index: pd.DatetimeIndex, temp: pd.Series, spec: HouseholdSpec | None = None,
             seed: int = 7) -> pd.DataFrame:
    """Hourly base load (kW) per appliance group for a household.

    `temp` is the outdoor temperature on the same hourly index.
    """
    spec = spec or HouseholdSpec()
    rng = np.random.default_rng(seed)
    n = len(index)
    hour = np.asarray(index.hour)
    weekend = np.asarray(index.weekday) >= 5
    t = temp.reindex(index).interpolate(limit_direction="both").to_numpy()

    # how many people are home: fewer on weekday working hours
    home = np.where((hour >= 9) & (hour < 16) & ~weekend, 0.45, 1.0)
    awake = ~((hour >= 0) & (hour < 6))
    day_noise = rng.normal(1.0, 0.12, size=n // 24 + 2).repeat(24)[:n]
    hour_noise = rng.normal(1.0, 0.15, size=n).clip(0.5, 1.6)

    fridge = spec.fridge_kw * (1 + 0.025 * (t - 20)).clip(0.7, 1.8) * rng.normal(1.0, 0.08, n).clip(0.7, 1.3)

    evening = (hour >= 18) | (hour < 1)
    morning = (hour >= 5) & (hour < 8)
    lights = spec.lights_kw * np.where(evening, 0.8, np.where(morning, 0.35, 0.05)) * hour_noise

    electronics = spec.electronics_kw * np.where(evening, 1.0, np.where(awake, 0.35 * home, 0.1)) * hour_noise

    fans_on = np.clip((t - 22) / 8, 0, 1) * spec.n_fans * np.where(awake, home, 0.8)
    fans = fans_on * spec.fan_kw * hour_noise

    # ACs: bedrooms at night, family room from late morning, more rooms when hotter.
    # An inverter AC holding a cool room runs well below its rating most of the time.
    heat = np.clip((t - spec.ac_start_temp) / 9.0, 0, 1)
    night = (hour >= 23) | (hour < 7)
    day_part = (hour >= 10) & (hour < 23)
    bedrooms = max(1.0, spec.n_ac * 0.75)
    rooms_on = np.where(night, bedrooms * (heat > 0.05),
                        np.where(day_part, (1 + (spec.n_ac - 1) * heat * home) * (heat > 0), 0.0))
    # warm humid monsoon nights: bedroom ACs stay on even below the threshold
    humid_night = night & (t > spec.ac_start_temp - 4)
    rooms_on = np.where(humid_night & (rooms_on == 0), bedrooms * 0.7, rooms_on)
    load_factor = np.where(night, 0.35 + 0.3 * heat, 0.4 + 0.5 * heat)
    use_today = rng.random(n // 24 + 2).repeat(24)[:n] < 0.93  # some days they skip it
    ac = rooms_on * spec.ac_kw * load_factor * use_today * rng.normal(1.0, 0.1, n).clip(0.7, 1.3)

    heater = np.zeros(n)
    if spec.heater_kw > 0:
        cold = np.clip((12 - t) / 8, 0, 1)
        heater = spec.heater_kw * cold * ((hour >= 19) & (hour < 23)) * hour_noise

    standby = np.full(n, spec.standby_kw)
    parts = {
        "ac": ac, "fans": fans, "fridge": fridge, "lights": lights,
        "electronics": electronics, "heater": heater, "standby": standby,
    }
    df = pd.DataFrame({k: np.maximum(v, 0) * day_noise.clip(0.75, 1.25) if k in ("ac", "fans") else np.maximum(v, 0)
                       for k, v in parts.items()}, index=index)
    df["base"] = df.sum(axis=1)
    return df


def habit_load(index: pd.DatetimeIndex, tasks: list[Task]) -> pd.Series:
    """Load from flexible tasks if they run at their usual times (no planning)."""
    load = pd.Series(0.0, index=index)
    for day in pd.DatetimeIndex(sorted(set(index.normalize()))):
        for task in tasks:
            if not task.runs_on(day):
                continue
            for h in range(task.habit_start, task.habit_start + task.hours):
                ts = day + pd.Timedelta(hours=h)
                if ts in load.index:
                    load[ts] += task.kw
    return load
