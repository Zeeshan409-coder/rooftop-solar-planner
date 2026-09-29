import pandas as pd
import pytest

from solarplan.tariff import EXPORT_RATES, Tariff, tariff_for


def hours(day):
    return pd.date_range(day, periods=24, freq="h", tz="Asia/Karachi")


@pytest.mark.parametrize("day,start,end", [
    ("2026-01-10", 17, 21), ("2026-04-10", 18, 22), ("2026-07-10", 19, 23), ("2026-10-10", 18, 22),
])
def test_peak_hours_follow_the_season(day, start, end):
    peak = Tariff().is_peak(hours(day))
    assert [h for h in range(24) if peak[h]] == list(range(start, end))


def test_prices_include_gst():
    t = Tariff()
    price = t.import_price(hours("2026-07-10"))
    assert price.iloc[20] == pytest.approx(46.85 * 1.18)
    assert price.iloc[12] == pytest.approx(34.53 * 1.18)


def test_export_rates():
    assert Tariff().export_rate == EXPORT_RATES["net_billing"] == 8.13
    assert EXPORT_RATES["net_metering_legacy"] == 25.32


def test_bill_adds_up():
    t = Tariff(fixed_per_kw_month=0)
    idx = hours("2026-07-10")
    imp = pd.Series(0.0, index=idx)
    exp = pd.Series(0.0, index=idx)
    imp.iloc[20] = 2.0   # peak
    imp.iloc[3] = 1.0    # off-peak
    exp.iloc[12] = 4.0
    bill = t.bill(imp, exp)
    expected = 2 * 46.85 * 1.18 + 1 * 34.53 * 1.18 - 4 * 8.13
    assert bill["total"] == pytest.approx(expected)
    assert bill["import_kwh"] == 3 and bill["export_kwh"] == 4


def test_fixed_charge_per_month():
    t = Tariff(fixed_per_kw_month=675, sanctioned_kw=7)
    idx = pd.date_range("2026-01-01", "2026-02-28 23:00", freq="h", tz="Asia/Karachi")
    zero = pd.Series(0.0, index=idx)
    assert t.bill(zero, zero)["fixed"] == pytest.approx(2 * 675 * 3.5)


def test_k_electric_peak_starts_at_half_past_six_in_summer():
    t = tariff_for("K-Electric")
    frac = t.peak_fraction(hours("2026-07-10"))
    assert frac[18] == 0.5 and frac[19] == 1 and frac[22] == 0.5 and frac[23] == 0
    price = t.import_price(hours("2026-07-10"))
    assert price.iloc[18] == pytest.approx((34.53 + 46.85) / 2 * 1.18)


def test_state_owned_companies_share_the_national_schedule():
    for code in ("LESCO", "MEPCO", "QESCO", "HAZECO", "TESCO"):
        peak = tariff_for(code).is_peak(hours("2026-01-10"))
        assert [h for h in range(24) if peak[h]] == [17, 18, 19, 20]


def test_flat_rate_suppliers():
    ajk = tariff_for("AJK Power")
    assert ajk.flat and not ajk.is_peak(hours("2026-07-10")).any()
    assert ajk.import_price(hours("2026-07-10")).eq(6.0).all()
    with pytest.raises(ValueError, match="bill"):
        tariff_for("GB Power")
    assert tariff_for("GB Power", rate=12).import_price(hours("2026-07-10")).eq(12).all()
