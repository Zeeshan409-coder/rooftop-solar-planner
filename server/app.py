"""Web dashboard for the planner.

    uvicorn server.app:app --port 8000
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from solarplan.backtest import Site, build_inputs, compare
from solarplan.history import available, full_years, load_history
from solarplan.household import HouseholdSpec, Task
from solarplan.locations import Location, custom, find, tree
from solarplan.optimizer import BatterySpec
from solarplan.planner import SiteConfig, forecast_weather, plan_day
from solarplan.pv import PVSystemSpec
from solarplan.tariff import EXPORT_RATES, SUPPLIERS, tariff_for

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Rooftop Solar Planner")


class TaskIn(BaseModel):
    name: str
    kw: float = Field(gt=0, le=10)
    hours: int = Field(ge=1, le=8)
    earliest: int = Field(ge=0, le=23)
    latest_end: int = Field(ge=1, le=24)
    habit_start: int = Field(ge=0, le=23)
    days: list[int] = [0, 1, 2, 3, 4, 5, 6]


class ConfigIn(BaseModel):
    place: str = "lahore"                 # district id or name
    lat: float | None = None              # a custom point instead of a district
    lon: float | None = None
    supplier: str | None = None           # defaults to the one serving the place
    rate: float | None = Field(None, gt=0, le=200)   # flat price per unit (AJK, GB)
    export: str = "net_billing"
    pv_kw: float = Field(10.0, gt=0, le=100)
    tilt: float = Field(25.0, ge=0, le=90)
    azimuth: float = Field(180.0, ge=0, le=360)
    inverter_kw: float | None = None
    battery_kwh: float = Field(10.0, ge=0, le=200)
    battery_now: float = Field(50.0, ge=0, le=100)
    reserve: float = Field(0.0, ge=0, le=80)
    acs: int = Field(2, ge=0, le=10)
    tasks: list[TaskIn] | None = None


class PlanRequest(BaseModel):
    config: ConfigIn = ConfigIn()
    mode: str | None = None               # "tomorrow" or "now" (live forecast), or "replay"
    date: str | None = None               # for replay: a past day


class SavingsRequest(BaseModel):
    config: ConfigIn = ConfigIn()
    year: int = 2024


def to_location(c: ConfigIn) -> Location:
    try:
        if c.lat is not None and c.lon is not None:
            return custom(c.lat, c.lon)
        return find(c.place)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e


def to_site_config(c: ConfigIn) -> SiteConfig:
    loc = to_location(c)
    if c.export not in EXPORT_RATES:
        raise HTTPException(400, f"unknown export option {c.export}")
    try:
        tariff = tariff_for(c.supplier or loc.supplier, c.export, c.rate)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e).strip("'\"")) from e
    household = HouseholdSpec(n_ac=c.acs)
    if c.tasks is not None:
        household.tasks = []
        for t in c.tasks:
            if t.latest_end - t.earliest < t.hours:
                raise HTTPException(400, f"'{t.name}' does not fit in its time window")
            household.tasks.append(Task(t.name, t.kw, t.hours, t.earliest, t.latest_end, tuple(t.days),
                                        min(max(t.habit_start, 0), 24 - t.hours)))
    cfg = SiteConfig.for_place(
        loc,
        pv=PVSystemSpec(kwp=c.pv_kw, tilt=c.tilt, azimuth=c.azimuth, inverter_kw=c.inverter_kw or max(c.pv_kw, 5.0)),
        battery=BatterySpec(capacity_kwh=c.battery_kwh, reserve=c.reserve / 100),
        household=household,
        tariff=tariff,
    )
    cfg.battery_now = c.battery_now / 100
    return cfg


def history(loc: Location):
    try:
        return load_history(loc)
    except OSError as e:
        raise HTTPException(503, f"could not download weather history for {loc.district}: {e}") from e


@app.get("/api/places")
def places():
    return tree()


@app.get("/api/suppliers")
def suppliers():
    return [{"code": s.code, "name": s.name, "flat": s.peak_hours is None, "rate": s.flat_rate,
             "net_billing": s.net_billing, "note": s.note} for s in SUPPLIERS.values()]


@app.get("/api/history-range")
def history_range(place: str = "lahore", lat: float | None = None, lon: float | None = None):
    loc = to_location(ConfigIn(place=place, lat=lat, lon=lon))
    w, _, source = history(loc)
    ok = w["ghi"].notna().groupby(w.index.normalize()).all()
    days = ok[ok].index
    return {"first": days[1].strftime("%Y-%m-%d"), "last": days[-2].strftime("%Y-%m-%d"),
            "years": full_years(w), "source": source}


@app.get("/api/history-status")
def history_status(place: str = "lahore"):
    return {"downloaded": available(to_location(ConfigIn(place=place)))}


@app.post("/api/plan")
def make_plan(req: PlanRequest):
    cfg = to_site_config(req.config)
    mode = req.mode or ("replay" if req.date else "tomorrow")
    if mode == "replay":
        if not req.date:
            raise HTTPException(400, "pick a day to replay")
        try:
            start = pd.Timestamp(req.date).tz_localize(cfg.tz)
        except ValueError as e:
            raise HTTPException(400, "date must be YYYY-MM-DD") from e
        w, elevation, hist_source = history(cfg.location)
        weather = w.loc[start:].iloc[:48]
        if len(weather) < 24 or weather["ghi"].iloc[:24].isna().any():
            raise HTTPException(400, f"no complete weather for {req.date}")
        weather.attrs["elevation"] = elevation
        source = {"kind": "replay",
                  "label": f"Replay of {start:%d %b %Y}, using the weather that actually happened ({hist_source})"}
    elif mode in ("now", "tomorrow"):
        try:
            weather = forecast_weather(cfg, mode)
        except (OSError, ValueError) as e:
            raise HTTPException(503, f"could not reach the weather forecast service ({e}). "
                                     "Pick a past day to replay instead.") from e
        if len(weather) < 24:
            raise HTTPException(503, "the weather forecast came back incomplete, try again in a minute")
        when = "the next 24 hours" if mode == "now" else "tomorrow"
        source = {"kind": "forecast", "label": f"Live Open-Meteo forecast for {when}"}
    else:
        raise HTTPException(400, f"unknown mode {mode}")
    plan = plan_day(cfg, weather)
    supplier = SUPPLIERS[cfg.tariff.supplier]
    return {**plan.to_dict(), "source": source, "place": cfg.location.label,
            "supplier": {"code": supplier.code, "name": supplier.name, "flat": cfg.tariff.flat,
                         "rate": cfg.tariff.offpeak_rate if cfg.tariff.flat else None},
            "battery_kwh": cfg.battery.capacity_kwh, "pv_kw": cfg.pv.kwp}


_jobs: dict[str, dict] = {}


def _run_savings(job_id: str, req: SavingsRequest):
    job = _jobs[job_id]
    try:
        cfg = to_site_config(req.config)
        w, elevation, _ = history(cfg.location)
        job.update(stage="training forecasters")
        inp = build_inputs(Site(cfg.lat, cfg.lon, elevation, w), req.year, cfg.pv, cfg.battery, cfg.household,
                           cfg.tariff)
        strategies = ["grid_only", "solar_only", "self_use", "planner", "planner_perfect"]

        def progress(frac, name):
            job.update(progress=round(frac, 3), stage=name)

        job["result"] = compare(inp, strategies, progress)
        job["status"] = "done"
    except HTTPException as e:
        job.update(status="failed", error=e.detail)
    except Exception as e:  # show any failure in the UI
        job.update(status="failed", error=str(e))
    job["finished"] = time.time()


@app.post("/api/savings", status_code=202)
def start_savings(req: SavingsRequest):
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "status": "running", "progress": 0.0, "stage": "loading weather",
                     "year": req.year, "started": time.time()}
    threading.Thread(target=_run_savings, args=(job_id, req), daemon=True).start()
    return {"id": job_id}


@app.get("/api/savings/{job_id}")
def get_savings(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "unknown job")
    return _jobs[job_id]


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")
