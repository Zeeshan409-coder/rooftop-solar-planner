"""Rooftop PV output from weather, using pvlib.

Solar position is taken at the middle of each hour, because the weather values are
hourly averages. Losses follow the PVWatts breakdown; soiling is set higher than
the usual default because dust and smog are a real problem on Pakistani roofs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pvlib


@dataclass
class PVSystemSpec:
    kwp: float = 10.0            # panel nameplate, kW DC
    tilt: float = 25.0           # degrees from horizontal
    azimuth: float = 180.0       # degrees, 180 = facing south
    inverter_kw: float = 10.0    # AC limit
    gamma: float = -0.0035       # power temperature coefficient per C (mono PERC)
    inverter_eff: float = 0.97
    losses: dict = field(default_factory=lambda: {
        "soiling": 4.0, "shading": 2.0, "mismatch": 2.0, "wiring": 2.0,
        "connections": 0.5, "lid": 1.5, "nameplate_rating": 1.0, "availability": 1.0,
    })

    def loss_fraction(self) -> float:
        return float(pvlib.pvsystem.pvwatts_losses(**self.losses)) / 100.0


def solar_position(index: pd.DatetimeIndex, lat: float, lon: float, altitude: float = 0.0) -> pd.DataFrame:
    mid = index + pd.Timedelta(minutes=30)
    pos = pvlib.solarposition.get_solarposition(mid, lat, lon, altitude=altitude)
    pos.index = index
    return pos


def clear_sky(index: pd.DatetimeIndex, lat: float, lon: float, altitude: float = 0.0) -> pd.DataFrame:
    """Clear-sky GHI/DNI/DHI (Ineichen, climatological turbidity) for hour-start index."""
    loc = pvlib.location.Location(lat, lon, altitude=altitude)
    mid = index + pd.Timedelta(minutes=30)
    cs = loc.get_clearsky(mid, model="ineichen")
    cs.index = index
    return cs


def pv_output(weather: pd.DataFrame, spec: PVSystemSpec, lat: float, lon: float,
              altitude: float = 0.0) -> pd.Series:
    """AC output in kW (average over each hour, so also kWh per hour)."""
    pos = solar_position(weather.index, lat, lon, altitude)
    dni_extra = pvlib.irradiance.get_extra_radiation(weather.index)
    airmass = pvlib.atmosphere.get_relative_airmass(pos["apparent_zenith"])
    poa = pvlib.irradiance.get_total_irradiance(
        spec.tilt, spec.azimuth, pos["apparent_zenith"], pos["azimuth"],
        weather["dni"].fillna(0), weather["ghi"].fillna(0), weather["dhi"].fillna(0),
        dni_extra=dni_extra, airmass=airmass, model="perez",
    )
    poa_global = poa["poa_global"].fillna(0).clip(lower=0)
    # reflection losses at steep angles
    aoi = pvlib.irradiance.aoi(spec.tilt, spec.azimuth, pos["apparent_zenith"], pos["azimuth"])
    iam = pvlib.iam.physical(aoi).fillna(0)
    effective = poa["poa_direct"].fillna(0).clip(lower=0) * iam + poa["poa_diffuse"].fillna(0).clip(lower=0)

    params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"]["open_rack_glass_polymer"]
    cell_temp = pvlib.temperature.sapm_cell(poa_global, weather["temp"], weather.get("wind", 1.0), **params)

    pdc0 = spec.kwp * 1000.0
    dc = pvlib.pvsystem.pvwatts_dc(effective, cell_temp, pdc0, spec.gamma)
    dc = dc * (1 - spec.loss_fraction())
    # pvlib wants the inverter's DC input rating: AC rating / efficiency
    ac = pvlib.inverter.pvwatts(dc, spec.inverter_kw * 1000.0 / spec.inverter_eff, eta_inv_nom=spec.inverter_eff)
    ac = pd.Series(np.clip(np.nan_to_num(ac), 0, spec.inverter_kw * 1000.0), index=weather.index)
    return ac / 1000.0
