import numpy as np
import pytest

from solarplan.optimizer import BatterySpec, JobWindow, hour_modes, solve

H = 24
PRICE = np.where((np.arange(H) >= 19) & (np.arange(H) < 23), 55.0, 40.0)
EXPORT = np.full(H, 8.13)


def sunny():
    pv = np.clip(6 * np.sin(np.pi * (np.arange(H) - 6) / 12), 0, None)
    base = np.full(H, 1.0)
    base[19:23] = 3.0
    return pv, base


def check_balance(plan, pv, base):
    lhs = pv - plan.curtailed + plan.grid_import + plan.discharge
    rhs = base + plan.job_load + plan.charge + plan.grid_export
    assert np.allclose(lhs, rhs, atol=1e-6)


def test_energy_balance_and_battery_limits():
    pv, base = sunny()
    b = BatterySpec(capacity_kwh=10)
    plan = solve(pv, base, PRICE, EXPORT, b, energy0=5.0)
    check_balance(plan, pv, base)
    assert plan.energy.min() >= b.e_min - 1e-6 and plan.energy.max() <= b.e_max + 1e-6
    assert plan.charge.max() <= b.charge_kw + 1e-6 and plan.discharge.max() <= b.discharge_kw + 1e-6


def test_battery_is_saved_for_the_evening_peak():
    pv, base = sunny()
    plan = solve(pv, base, PRICE, EXPORT, BatterySpec(capacity_kwh=10), energy0=1.0)
    assert plan.charge[8:16].sum() > 5              # fills up from midday solar
    assert plan.discharge[19:23].sum() > 5          # and spends it in the peak
    assert plan.grid_import[19:23].sum() < plan.grid_import[0:6].sum()


def test_each_job_runs_once_inside_its_window():
    pv, base = sunny()
    jobs = [JobWindow("Pump", 0.75, 1, list(range(6, 20))), JobWindow("Wash", 0.5, 2, list(range(8, 19)))]
    plan = solve(pv, base, PRICE, EXPORT, None, 0.0, jobs)
    check_balance(plan, pv, base)
    assert plan.job_load.sum() == pytest.approx(0.75 + 1.0)
    pump = plan.job_starts[("Pump", 0)]
    assert 6 <= pump < 20
    assert 9 <= pump <= 15      # without a battery, the sunny hours are the cheap ones


def test_job_stays_at_its_usual_time_when_moving_saves_nothing():
    pv = np.zeros(H)                 # no sun at all: every off-peak hour costs the same
    base = np.ones(H)
    job = JobWindow("Pump", 0.75, 1, list(range(6, 18)), usual=7)
    plan = solve(pv, base, PRICE, EXPORT, None, 0.0, [job])
    assert plan.job_starts[("Pump", 0)] == 7


def test_no_grid_charging_when_disabled():
    pv = np.zeros(H)
    base = np.ones(H)
    b = BatterySpec(capacity_kwh=10, grid_charging=False, wear_cost=0.0)
    plan = solve(pv, base, PRICE, EXPORT, b, energy0=b.e_min)
    assert plan.charge.sum() == pytest.approx(0, abs=1e-6)


def test_reserve_is_kept_for_load_shedding():
    pv, base = sunny()
    b = BatterySpec(capacity_kwh=10, reserve=0.3)
    plan = solve(pv, base, PRICE, EXPORT, b, energy0=8.0)
    assert plan.energy.min() >= 10 * (0.1 + 0.3) - 1e-6


def test_modes_read_like_inverter_settings():
    pv, base = sunny()
    b = BatterySpec(capacity_kwh=10)
    plan = solve(pv, base, PRICE, EXPORT, b, energy0=1.0)
    modes = hour_modes(plan, pv, base, b, 1.0)
    assert modes[20] == "use_battery"
    assert "charge_solar" in modes[8:16]
    assert set(modes) <= {"use_battery", "hold", "charge_solar", "charge_grid", "idle", "grid"}
