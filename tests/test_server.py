import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import server.app as appmod
from solarplan import planner


@pytest.fixture()
def client():
    return TestClient(appmod.app)


def test_places_and_history(client):
    provinces = client.get("/api/places").json()
    punjab = next(p for p in provinces if p["province"] == "Punjab")
    lahore_div = next(d for d in punjab["divisions"] if d["division"] == "Lahore")
    lahore = next(d for d in lahore_div["districts"] if d["id"] == "lahore")
    assert lahore["supplier"] == "LESCO" and abs(lahore["lat"] - 31.55) < 0.1
    assert {s["code"] for s in client.get("/api/suppliers").json()} >= {"LESCO", "K-Electric", "HAZECO", "AJK Power"}
    assert client.get("/api/history-status?place=lahore").json()["downloaded"] is True
    r = client.get("/api/history-range?place=lahore").json()
    assert r["first"].startswith("2018") and 2024 in r["years"] and r["source"] == "NASA POWER"


def test_plan_replay(client):
    r = client.post("/api/plan", json={"date": "2024-06-15", "config": {"pv_kw": 6, "battery_kwh": 5}})
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["kind"] == "replay" and body["pv_kw"] == 6 and len(body["hours"]) == 24


def test_plan_rejects_bad_input(client):
    assert client.post("/api/plan", json={"date": "not-a-date"}).status_code == 400
    assert client.post("/api/plan", json={"date": "2030-01-01"}).status_code == 400
    assert client.post("/api/plan", json={"config": {"place": "atlantis"}, "date": "2024-06-15"}).status_code == 400
    assert client.post("/api/plan", json={"config": {"lat": 51.5, "lon": 0}, "date": "2024-06-15"}).status_code == 400
    assert client.post("/api/plan", json={"mode": "replay"}).status_code == 400
    bad_job = {"name": "Pump", "kw": 1, "hours": 3, "earliest": 10, "latest_end": 11, "habit_start": 10}
    r = client.post("/api/plan", json={"date": "2024-06-15", "config": {"tasks": [bad_job]}})
    assert r.status_code == 400 and "window" in r.json()["detail"]


def test_live_forecast_plan(client, monkeypatch, open_meteo_payload):
    from solarplan import weather

    payload = open_meteo_payload("2024-06-14", "2024-06-17 22:00")
    monkeypatch.setattr(weather, "_get", lambda url, params, timeout: json.dumps(payload).encode())

    class Frozen(pd.Timestamp):
        @classmethod
        def now(cls, tz=None):
            return pd.Timestamp("2024-06-14 15:20", tz=tz)

    monkeypatch.setattr(planner.pd, "Timestamp", Frozen)
    tomorrow = client.post("/api/plan", json={"mode": "tomorrow"}).json()
    assert tomorrow["source"]["kind"] == "forecast"
    assert tomorrow["date"] == "2024-06-15" and tomorrow["start"] == "00:00"
    now = client.post("/api/plan", json={"mode": "now"}).json()
    assert now["date"] == "2024-06-14" and now["start"] == "15:00" and len(now["hours"]) == 24
    assert now["hours"][-1]["time"] == "14:00"


def test_live_forecast_offline(client, monkeypatch):
    from solarplan import weather

    def offline(*a, **k):
        raise OSError("no internet")

    monkeypatch.setattr(weather, "_get", offline)
    r = client.post("/api/plan", json={"mode": "tomorrow"})
    assert r.status_code == 503 and "replay" in r.json()["detail"]


def test_supplier_follows_the_place(client):
    ke = client.post("/api/plan", json={"date": "2024-06-15", "config": {"place": "lahore", "supplier": "K-Electric"}})
    assert ke.json()["peak_hours"] == ["18:30", "22:30"] and ke.json()["supplier"]["code"] == "K-Electric"
    ajk = client.post("/api/plan", json={"date": "2024-06-15", "config": {"place": "lahore", "supplier": "AJK Power"}})
    assert ajk.json()["peak_hours"] is None and ajk.json()["supplier"]["rate"] == 6.0
    gb = client.post("/api/plan", json={"date": "2024-06-15", "config": {"place": "lahore", "supplier": "GB Power"}})
    assert gb.status_code == 400 and "bill" in gb.json()["detail"]
    gb = client.post("/api/plan", json={"date": "2024-06-15",
                                        "config": {"place": "lahore", "supplier": "GB Power", "rate": 9}})
    assert gb.status_code == 200 and gb.json()["supplier"]["rate"] == 9


def test_custom_jobs(client):
    job = {"name": "EV charger", "kw": 3.0, "hours": 2, "earliest": 8, "latest_end": 18, "habit_start": 18}
    r = client.post("/api/plan", json={"date": "2024-06-15", "config": {"tasks": [job]}}).json()
    assert [j["name"] for j in r["jobs"]] == ["EV charger"]


def test_savings_job(client, monkeypatch):
    def fake_compare(inp, strategies, progress):
        progress(1.0, "planner")
        return {"planner": {"total": 1.0}}

    monkeypatch.setattr(appmod, "compare", fake_compare)
    monkeypatch.setattr(appmod, "build_inputs", lambda *a, **k: object())
    job_id = client.post("/api/savings", json={"year": 2024}).json()["id"]
    for _ in range(50):
        job = client.get(f"/api/savings/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["result"]["planner"]["total"] == 1.0
    assert client.get("/api/savings/nope").status_code == 404


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200 and "Rooftop Solar Planner" in r.text
