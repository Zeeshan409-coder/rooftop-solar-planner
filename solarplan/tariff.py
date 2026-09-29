"""Electricity prices for Pakistani residential consumers, by supplier.

Defaults (all changeable):
  * the uniform national time-of-use tariff for homes with a sanctioned load of
    5 kW or more, which is most homes with rooftop solar: Rs 46.85 peak / Rs 34.53
    off-peak per unit (S.R.O. 279(I)/2026, effective 12 Feb 2026). The same rates
    apply to every distribution company, including K-Electric.
  * peak hours: the national schedule (5-9 pm Dec-Feb, 6-10 pm Mar-May and Sep-Nov,
    7-11 pm Jun-Aug) for the eleven state-owned companies; K-Electric uses its own
    windows (6:30-10:30 pm Apr-Oct, 6-10 pm Nov-Mar)
  * net billing for new solar consumers (NEPRA, Feb 2026): imports are billed at
    the full tariff, exports are bought back at Rs 8.13 per unit. Consumers still on
    the old net metering terms get Rs 25.32.
  * 18% GST on imported energy. Other duties and fuel adjustments vary by month;
    fold them into `tax` if you want exact bills.
  * Azad Kashmir and Gilgit-Baltistan buy power in bulk and sell it at their own
    flat rates. AJK's domestic rate for homes above 300 units a month is Rs 6
    (July 2024 notification). GB's rates are not published in a usable form, so
    the user has to enter the rate from their bill.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# month -> (peak start, peak end) in hours of the day; may be fractional (18.5 = 6:30 pm)
NATIONAL_PEAK_HOURS = {
    12: (17, 21), 1: (17, 21), 2: (17, 21),
    3: (18, 22), 4: (18, 22), 5: (18, 22),
    6: (19, 23), 7: (19, 23), 8: (19, 23),
    9: (18, 22), 10: (18, 22), 11: (18, 22),
}
LESCO_PEAK_HOURS = NATIONAL_PEAK_HOURS
KE_PEAK_HOURS = {m: (18.5, 22.5) if 4 <= m <= 10 else (18.0, 22.0) for m in range(1, 13)}

EXPORT_RATES = {"net_billing": 8.13, "net_metering_legacy": 25.32}


@dataclass(frozen=True)
class Supplier:
    code: str
    name: str
    peak_hours: dict | None = None      # None: one flat rate all day
    flat_rate: float | None = None      # Rs per unit, all-in, for flat-rate suppliers
    net_billing: bool = True            # can sell solar back to the grid
    note: str = ""


_STATE_OWNED = {
    "LESCO": "Lahore Electric Supply Company",
    "GEPCO": "Gujranwala Electric Power Company",
    "FESCO": "Faisalabad Electric Supply Company",
    "IESCO": "Islamabad Electric Supply Company",
    "MEPCO": "Multan Electric Power Company",
    "PESCO": "Peshawar Electric Supply Company",
    "HAZECO": "Hazara Electric Supply Company",
    "TESCO": "Tribal Areas Electricity Supply Company",
    "HESCO": "Hyderabad Electric Supply Company",
    "SEPCO": "Sukkur Electric Power Company",
    "QESCO": "Quetta Electric Supply Company",
}
SUPPLIERS: dict[str, Supplier] = {
    code: Supplier(code, name, NATIONAL_PEAK_HOURS) for code, name in _STATE_OWNED.items()
}
SUPPLIERS["K-Electric"] = Supplier("K-Electric", "K-Electric", KE_PEAK_HOURS)
SUPPLIERS["AJK Power"] = Supplier(
    "AJK Power", "AJK Electricity Department", flat_rate=6.0, net_billing=False,
    note="Flat Rs 6 per unit for homes using over 300 units a month; check your bill.")
SUPPLIERS["GB Power"] = Supplier(
    "GB Power", "Gilgit-Baltistan Water and Power Department", flat_rate=None, net_billing=False,
    note="Enter the price per unit from your bill.")


def tariff_for(supplier: str, export: str = "net_billing", rate: float | None = None,
               export_rate: float | None = None) -> Tariff:
    """The default tariff for a supplier. `rate` overrides a flat-rate supplier's price;
    `export_rate` overrides what exported units earn."""
    if supplier not in SUPPLIERS:
        raise KeyError(f"unknown electricity supplier '{supplier}'")
    s = SUPPLIERS[supplier]
    if s.peak_hours is not None:
        t = Tariff(export_rate=EXPORT_RATES[export], peak_hours=dict(s.peak_hours), supplier=s.code)
    else:
        price = rate if rate is not None else s.flat_rate
        if price is None:
            raise ValueError(f"{s.name}: {s.note}")
        t = Tariff(peak_rate=price, offpeak_rate=price, export_rate=0.0, tax=0.0, peak_hours={},
                   fixed_per_kw_month=0.0, supplier=s.code)
    if export_rate is not None:
        t.export_rate = export_rate
    return t


@dataclass
class Tariff:
    peak_rate: float = 46.85
    offpeak_rate: float = 34.53
    export_rate: float = EXPORT_RATES["net_billing"]
    tax: float = 0.18                     # GST on imports
    peak_hours: dict = field(default_factory=lambda: dict(NATIONAL_PEAK_HOURS))
    fixed_per_kw_month: float = 675.0     # on 50% of sanctioned load
    sanctioned_kw: float = 7.0
    supplier: str = "LESCO"

    @property
    def flat(self) -> bool:
        return not self.peak_hours

    def peak_fraction(self, index: pd.DatetimeIndex) -> np.ndarray:
        """Share of each hour [h, h+1) that falls inside the peak window."""
        if self.flat:
            return np.zeros(len(index))
        hours = np.asarray(index.hour, dtype=float)
        months = np.asarray(index.month)
        start = np.array([self.peak_hours[m][0] for m in months], dtype=float)
        end = np.array([self.peak_hours[m][1] for m in months], dtype=float)
        return np.clip(np.minimum(hours + 1, end) - np.maximum(hours, start), 0.0, 1.0)

    def is_peak(self, index: pd.DatetimeIndex) -> np.ndarray:
        return self.peak_fraction(index) >= 0.5

    def import_price(self, index: pd.DatetimeIndex) -> pd.Series:
        """Rs per kWh bought from the grid, tax included."""
        base = self.offpeak_rate + self.peak_fraction(index) * (self.peak_rate - self.offpeak_rate)
        return pd.Series(base * (1 + self.tax), index=index)

    def export_price(self, index: pd.DatetimeIndex) -> pd.Series:
        return pd.Series(self.export_rate, index=index, dtype=float)

    def bill(self, grid_import: pd.Series, grid_export: pd.Series) -> dict:
        """Energy cost over the period in Rs, plus the fixed charges per month."""
        cost_import = float((grid_import * self.import_price(grid_import.index)).sum())
        credit_export = float((grid_export * self.export_price(grid_export.index)).sum())
        months = len(pd.PeriodIndex(grid_import.index.tz_localize(None), freq="M").unique())
        fixed = months * self.fixed_per_kw_month * self.sanctioned_kw * 0.5
        return {
            "import_kwh": float(grid_import.sum()),
            "export_kwh": float(grid_export.sum()),
            "import_cost": cost_import,
            "export_credit": credit_export,
            "fixed": fixed,
            "total": cost_import - credit_export + fixed,
        }
