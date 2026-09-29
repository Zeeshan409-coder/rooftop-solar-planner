from solarplan.optimizer import BatterySpec
from solarplan.planner import SiteConfig, load_from_history, plan_day
from solarplan.pv import PVSystemSpec


def test_plan_for_a_hot_june_day(june_day):
    plan = plan_day(SiteConfig.for_place("lahore"), june_day)
    assert plan.date == "2024-06-15"
    assert len(plan.hours) == 24
    assert plan.peak_hours == ("19:00", "23:00")
    assert plan.cost_plan <= plan.cost_usual + 1
    assert plan.instructions and all("-" in line[:11] for line in plan.instructions)
    assert {j["name"] for j in plan.jobs} >= {"Water pump", "Iron"}
    peak = [h for h in plan.hours if h["peak"]]
    assert any(h["mode"] == "use_battery" for h in peak)


def test_cloudy_day_still_beats_the_usual_routine(lahore_weather):
    # little sun: the plan tops the battery up from the grid late at night for
    # tomorrow's peak, which must count in its favour, not against it
    cfg = SiteConfig.for_place("lahore", pv=PVSystemSpec(kwp=6, inverter_kw=6),
                              battery=BatterySpec(capacity_kwh=5))
    cfg.battery_now = 0.4
    plan = plan_day(cfg, lahore_weather.loc["2025-06-15":].iloc[:48])
    assert plan.solar_kwh < 20
    assert plan.cost_plan <= plan.cost_usual + 1


def test_plan_without_a_battery(june_day):
    cfg = SiteConfig.for_place("lahore", battery=BatterySpec(capacity_kwh=0))
    plan = plan_day(cfg, june_day)
    assert all(h["charge"] == 0 and h["discharge"] == 0 for h in plan.hours)
    # every job runs on solar: nothing is bought from the grid while it runs
    for job in plan.jobs:
        start = int(job["start"][:2])
        for h in plan.hours[start:start + job["hours"]]:
            assert h["grid_import"] < 1e-6


def test_load_from_meter_history(lahore_weather):
    import pandas as pd
    idx = pd.date_range("2024-05-01", periods=24 * 21, freq="h", tz="Asia/Karachi")
    history = pd.Series([1.0 + (ts.hour == 20) for ts in idx], index=idx)
    future = pd.date_range("2024-05-22", periods=24, freq="h", tz="Asia/Karachi")
    fc = load_from_history(history, future)
    assert fc.iloc[20] == 2.0 and fc.iloc[3] == 1.0


def test_command_line_with_own_meter_readings(tmp_path, capsys):
    import json

    import numpy as np
    import pandas as pd

    from solarplan.cli import main

    idx = pd.date_range("2025-05-20", "2025-06-01 23:00", freq="h")
    kw = np.where((idx.hour >= 13) & (idx.hour <= 23), 2.5, 0.8)
    csv = tmp_path / "usage.csv"
    pd.Series(kw, index=idx).to_csv(csv, index_label="time")

    main(["plan", "--date", "2025-06-02", "--pv-kw", "6", "--battery-kwh", "5", "--load-csv", str(csv), "--json"])
    plan = json.loads(capsys.readouterr().out)
    loads = [h["load"] for h in plan["hours"]]
    assert loads[3] == 0.8 and loads[20] == 2.5


def test_plan_from_the_middle_of_the_day(lahore_weather):
    weather = lahore_weather.loc["2024-06-15 15:00":].iloc[:48]
    plan = plan_day(SiteConfig.for_place("lahore"), weather)
    assert plan.start == "15:00" and plan.hours[0]["time"] == "15:00" and plan.hours[-1]["time"] == "14:00"
    # today's 07:00-10:00 jobs are taken as done; tomorrow's are planned within the 24 hours
    for job in plan.jobs:
        assert 9 <= job["offset"] <= 23, job
    # instructions run in time order from 15:00 through the night
    offsets = [(int(line[:2]) - 15) % 24 for line in plan.instructions]
    assert offsets[0] == 0 and offsets == sorted(offsets)
    assert plan.cost_plan <= plan.cost_usual + 1


def test_flat_rate_home_still_uses_its_solar(lahore_weather):
    from solarplan.tariff import tariff_for

    cfg = SiteConfig.for_place("muzaffarabad")
    assert cfg.tariff.flat and cfg.tariff.supplier == "AJK Power"
    plan = plan_day(cfg, lahore_weather.loc["2024-06-15":].iloc[:48])
    assert plan.peak_hours is None
    assert not any(h["peak"] for h in plan.hours)
    assert plan.cost_plan <= plan.cost_usual + 1
    assert tariff_for("GB Power", rate=10).flat
