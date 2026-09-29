import numpy as np
import pandas as pd

from solarplan.household import HouseholdSpec, Task, habit_load, simulate


def week(temp):
    idx = pd.date_range("2024-06-03", periods=24 * 7, freq="h", tz="Asia/Karachi")
    return idx, pd.Series(float(temp), index=idx)


def test_hotter_weather_means_more_ac():
    idx, cool = week(22)
    _, hot = week(42)
    assert simulate(idx, cool)["ac"].sum() == 0
    assert simulate(idx, hot)["ac"].sum() > 100


def test_more_acs_more_load():
    idx, hot = week(40)
    two = simulate(idx, hot, HouseholdSpec(n_ac=2))["base"].sum()
    four = simulate(idx, hot, HouseholdSpec(n_ac=4))["base"].sum()
    assert four > two


def test_same_seed_same_house():
    idx, t = week(35)
    assert np.allclose(simulate(idx, t, seed=3)["base"], simulate(idx, t, seed=3)["base"])


def test_habit_load_places_jobs_on_their_days():
    idx, _ = week(30)
    tasks = [Task("Pump", kw=1.0, hours=2, habit_start=7), Task("Wash", kw=0.5, hours=1, days=(0,), habit_start=10)]
    load = habit_load(idx, tasks)
    assert load.sum() == 7 * 2 * 1.0 + 1 * 0.5
    monday = load.loc["2024-06-03"]
    assert monday.iloc[7] == 1.0 and monday.iloc[8] == 1.0 and monday.iloc[10] == 0.5
