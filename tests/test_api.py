"""Integration, end-to-end and security tests against a real Postgres. The demo scenario runs the real solver (about 20 s)."""
import pytest

from core import db, jobs, scenario
from ws import rules, store

from .conftest import bearer

SCHED, VIEWER, ADMIN, OTHER = bearer("acme", "scheduler"), bearer("acme", "viewer"), bearer("acme", "admin"), bearer("other", "scheduler")


@pytest.fixture(scope="module")
def demo(client):
    results, _ = scenario.run(client, drain=jobs.drain)
    return results


def test_demo_journey(demo, seeded):
    fc = demo["fc"]
    sat = fc["days"][5]
    assert sat["promotion"] and sat["transactions"] > 1.15 * sat["same_day_last_week"] and fc["backtest"]["model_daily_wape"] < fc["backtest"]["baseline_daily_wape"]
    actual = seeded["acme"]["world"]["locations"][0]["tx"][-2].sum()
    assert abs(sat["transactions"] - actual) < abs(sat["same_day_last_week"] - actual)
    opt = demo["opt"]["result"]
    pub, base = opt["published"], opt["baseline"]
    assert opt["solver_status"] in ("OPTIMAL", "FEASIBLE") and pub["hard_violations"] == 0 and demo["sched"]["rule_violations"] == []
    assert pub["labor_cost"] < 0.95 * base["labor_cost"] and pub["fill_rate"] >= 0.99 and pub["fill_rate"] >= base["fill_rate"] and pub["preference_score"] > base["preference_score"] and pub["overtime_hours"] <= base["overtime_hours"]
    d = demo["sched"]["day"]
    assert len(d["shifts"]) > 30 and all((s["break"] is not None) == (s["length_slots"] > 12) for s in d["shifts"])
    assert demo["co1"]["understaffed_hours_now"] > demo["co1"]["understaffed_hours_before"] and demo["co2"]["understaffed_hours_now"] > demo["co2"]["understaffed_hours_before"]
    r1 = demo["rep1"]
    assert r1["eligible"] == len(r1["candidates"]) or len(r1["candidates"]) == 6
    assert r1["eligible"] + r1["not_eligible"] == 61 - 2                    # everyone at the store except the two who called out
    fix = demo["fix"]
    h = fix["understaffed_hours"]
    assert fix["callouts_repaired"] == 2 and 1 <= fix["assignments_changed"] <= 6 and h["after_repair"] < h["after_callouts"] and fix["hard_violations_after_repair"] == 0
    out = {demo["co1"]["shift"]["worker"], demo["co2"]["shift"]["worker"]}
    assert not out & {c["worker"] for c in fix["changes"]}
    assert demo["bad"]["code"] == "claim_rejected" and "CERT_EXPIRED" in demo["bad"]["detail"]
    assert demo["ok"]["status"] == "claimed" and demo["ok"]["to"] == demo["cand"]["candidates"][0]["worker"] and demo["ok"]["hard_violations"] == 0
    fair = demo["fair"]
    assert fair["shifts_by_source"]["marketplace"] == 1 and fair["shifts_by_source"].get("repair", 0) >= 1 and fair["current"]["hard_violations"] == 0
    assert sum(x["workers"] for x in fair["hours_vs_contract"]["histogram"]) == 61
    acts = {e["action"] for e in demo["audit"]["events"]}
    assert demo["audit"]["chain_valid"] and {"demand.forecast", "schedule.published", "callout.recorded", "schedule.repaired", "market.offered", "market.claim_refused", "market.claimed"} <= acts


def test_the_stored_schedule_is_legal_when_rechecked_from_the_database(demo, seeded):
    """Independent of the solver and of the API responses: load rows, run the rule pack."""
    with db.tx(seeded["acme"]["tenant"]) as c:
        run = c.execute("SELECT * FROM schedule_runs WHERE id = %s", [demo["opt"]["result"]["schedule_id"]]).fetchone()
        ws, shifts, demand = store.workers(c, run["location_id"], run["horizon_start"]), store.shifts(c, run), store.demand(c, run["location_id"], run["horizon_start"])
        gone = {r["worker_id"] for r in c.execute("SELECT worker_id FROM callouts")}
    m = rules.metrics(ws, shifts, demand)
    assert m["hard_violations"] == [] and m["fill_rate"] > 0.98 and len(shifts) == demo["fair"]["current"]["shifts"]
    assert not any(s["worker"] in gone and s["day"] == 5 for s in shifts)
    assert all(s["length"] in rules.LENGTHS for s in shifts)


def test_conflicts_and_validation(client, demo):
    sid = demo["opt"]["result"]["schedule_id"]
    loc = demo["loc"]["items"][0]["id"]
    assert client.post("/v1/callouts", headers=SCHED, json={"shift_id": demo["co1"]["shift"]["shift_id"]}).json()["code"] == "shift_not_assigned"
    assert client.post(f"/v1/schedules/{sid}:repair", headers=SCHED, json={"day": 2}).json()["code"] == "nothing_to_repair"
    assert client.post(f"/v1/shift-market/orders/{demo['ord']['id']}:claim", headers=SCHED, json={"worker_id": demo["cand"]["candidates"][1]["worker_id"]}).json()["code"] == "order_closed"
    assert client.post("/v1/demand:forecast", headers=SCHED, json={"location_id": loc, "horizon_start": "2026-10-13"}).json()["code"] == "horizon_not_monday"
    assert client.post("/v1/demand:forecast", headers=SCHED, json={"location_id": loc, "horizon_start": "2026-11-02"}).json()["code"] == "insufficient_history"
    other_store = client.get("/v1/locations?q=%2302", headers=VIEWER).json()["items"][0]["id"]
    assert client.post("/v1/schedules:optimize", headers=SCHED, json={"location_id": other_store, "horizon_start": "2026-10-12"}).json()["code"] == "no_forecast"
    assert client.get(f"/v1/schedules/{sid}/shifts?at=six", headers=VIEWER).status_code == 422
    assert client.post("/v1/schedules:optimize", headers=SCHED, json={"location_id": loc, "horizon_start": "2026-10-12", "time_limit_seconds": 900}).status_code == 422


def test_marketplace_rules(client, demo):
    sid = demo["opt"]["result"]["schedule_id"]
    shift = client.get(f"/v1/schedules/{sid}/shifts?day=2&skill=cashier", headers=VIEWER).json()["items"][0]
    order = client.post("/v1/shift-market/orders", headers=SCHED, json={"shift_id": shift["shift_id"]}).json()
    assert client.post("/v1/shift-market/orders", headers=SCHED, json={"shift_id": shift["shift_id"]}).json()["code"] == "already_offered"
    assert client.post(f"/v1/shift-market/orders/{order['id']}:claim", headers=SCHED, json={"worker_id": shift["worker_id"]}).json()["code"] == "own_shift"
    cands = client.get(f"/v1/shift-market/orders/{order['id']}/candidates", headers=VIEWER).json()
    assert cands["eligible"] + cands["not_eligible"] == 60
    same_day = next(s for s in client.get(f"/v1/schedules/{sid}/shifts?day=2", headers=VIEWER).json()["items"] if s["worker_id"] != shift["worker_id"])
    refused = client.post(f"/v1/shift-market/orders/{order['id']}:claim", headers=SCHED, json={"worker_id": same_day["worker_id"]})
    assert refused.status_code == 409 and "DOUBLE_BOOKED" in refused.json()["detail"]
    assert client.get(f"/v1/schedules/{sid}/shifts?day=2&skill=cashier", headers=VIEWER).json()["items"][0]["worker_id"] == shift["worker_id"]      # a refused claim moves nothing


def test_callout_is_idempotent_with_a_key(client, demo):
    sid = demo["opt"]["result"]["schedule_id"]
    shift = client.get(f"/v1/schedules/{sid}/shifts?day=1&skill=sales_floor", headers=VIEWER).json()["items"][0]
    h = {**SCHED, "Idempotency-Key": "callout-once"}
    a = client.post("/v1/callouts", headers=h, json={"shift_id": shift["shift_id"]})
    b = client.post("/v1/callouts", headers=h, json={"shift_id": shift["shift_id"]})
    assert a.status_code == 201 and a.json() == b.json()
    with db.tx() as c:
        assert c.execute("SELECT count(*) AS n FROM callouts WHERE shift_id = %s", [shift["shift_id"]]).fetchone()["n"] == 1
    fix = client.post(f"/v1/schedules/{sid}:repair", headers=SCHED, json={"day": 1}).json()
    assert fix["hard_violations_after_repair"] == 0 and fix["understaffed_hours"]["after_repair"] <= fix["understaffed_hours"]["after_callouts"]


@pytest.mark.parametrize("headers,method,path,body,expected", [
    ({}, "GET", "/v1/locations", None, 401),
    (VIEWER, "POST", "/v1/demand:forecast", {}, 403),
    (VIEWER, "POST", "/v1/schedules:optimize", {}, 403),
    (VIEWER, "POST", "/v1/callouts", {}, 403),
    (VIEWER, "POST", "/v1/shift-market/orders", {}, 403),
    (VIEWER, "GET", "/v1/audit", None, 403),
    (SCHED, "GET", "/v1/audit", None, 403),
    (VIEWER, "GET", "/v1/locations", None, 200),
    (ADMIN, "GET", "/v1/audit", None, 200),
])
def test_authorization(client, demo, headers, method, path, body, expected):
    assert client.request(method, path, headers=headers, json=body).status_code == expected


def test_tenant_isolation(client, demo):
    sid, loc = demo["opt"]["result"]["schedule_id"], demo["loc"]["items"][0]["id"]
    assert client.get("/v1/locations", headers=OTHER).json()["locations"] == 2 and client.get("/v1/locations", headers=VIEWER).json()["workers"] == 5000
    for path in (f"/v1/schedules/{sid}", f"/v1/schedules/{sid}/shifts", f"/v1/fairness?schedule_id={sid}", f"/v1/callouts/{demo['co1']['id']}/replacements", f"/v1/shift-market/orders/{demo['ord']['id']}/candidates"):
        assert client.get(path, headers=OTHER).status_code == 404, path
    shift = client.get(f"/v1/schedules/{sid}/shifts?day=3", headers=VIEWER).json()["items"][0]
    assert client.post("/v1/callouts", headers=OTHER, json={"shift_id": shift["shift_id"]}).status_code == 404
    assert client.post("/v1/shift-market/orders", headers=OTHER, json={"shift_id": shift["shift_id"]}).status_code == 404
    assert client.post(f"/v1/schedules/{sid}:repair", headers=OTHER, json={"day": 5}).status_code == 404
    assert client.post("/v1/demand:forecast", headers=OTHER, json={"location_id": loc, "horizon_start": "2026-10-12"}).status_code == 404
    assert client.post("/v1/schedules:optimize", headers=OTHER, json={"location_id": loc, "horizon_start": "2026-10-12"}).status_code == 404
    assert client.get(f"/v1/schedules/{sid}/shifts?day=3", headers=VIEWER).json()["items"][0]["shift_id"] == shift["shift_id"]
