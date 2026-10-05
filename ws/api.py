"""Public API (modular monolith). Blueprint services map to: workforce-master (ws/store.py), demand-forecast (ws/forecast.py),
rules-service (ws/rules.py), schedule-optimizer + reoptimization-service (ws/solver.py), shift-marketplace and fairness-service (this file).

    uvicorn ws.api:app          python -m core.jobs ws.api      # the worker runs the schedule solver
"""
import datetime
import uuid

import numpy as np
from fastapi import Depends, Header
from fastapi.encoders import jsonable_encoder
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from core import audit, db, jobs
from core.app import Ctx, Problem, create_app, run

from . import forecast, rules, solver, store, world

READ = {"schedules:read", "jobs:read"}
WRITE = READ | {"forecast:run", "schedules:write", "callouts:write", "market:write"}
PERMISSIONS = {"viewer": READ, "scheduler": WRITE, "admin": WRITE | {"audit:read"}}
app = create_app("workforce-scheduler", PERMISSIONS)
auth = app.state.auth
IdemKey = Header(None, alias="Idempotency-Key")
KEEP = ("labor_cost", "paid_hours", "overtime_hours", "workers_with_overtime", "shifts", "workers_scheduled", "demand_hours", "understaffed_intervals", "understaffed_hours", "overstaffed_hours", "fill_rate",
        "hours_in_avoided_times", "share_of_hours_in_preferred_times", "preference_score", "weekend_shifts", "workers_on_both_weekend_days", "weekend_shifts_to_people_who_just_worked_four",
        "workers_below_contracted_hours", "hours_below_contract")


def system_ctx(job):
    return Ctx(job["tenant_id"], uuid.UUID(job["payload"]["actor_id"]), "worker", "system")


def summary(workers, shifts, demand):
    m = rules.metrics(workers, shifts, demand)
    return {**{k: m[k] for k in KEEP}, "hard_violations": len(m["hard_violations"])}, m


def get_location(c, location_id):
    loc = c.execute("SELECT id, name, timezone, size, rule_pack FROM locations WHERE id = %s", [location_id]).fetchone()
    if not loc:
        raise Problem(404, "location_not_found")
    return loc


def get_run(c, schedule_id, lock=False):
    r = c.execute("SELECT * FROM schedule_runs WHERE id = %s" + (" FOR UPDATE" if lock else ""), [schedule_id]).fetchone()
    if not r:
        raise Problem(404, "schedule_not_found")
    return r


def get_shift(c, shift_id, lock=False):
    """A shift with its schedule run; the run is under row-level security, so another tenant's shift is simply not found."""
    s = c.execute("""SELECT sh.id, sh.status, sh.schedule_run_id, sh.starts_at, sh.ends_at, sk.name AS skill, a.worker_id FROM shifts sh JOIN schedule_runs r ON r.id = sh.schedule_run_id
                       JOIN skills sk ON sk.id = sh.required_skill_id LEFT JOIN assignments a ON a.shift_id = sh.id WHERE sh.id = %s""" + (" FOR UPDATE OF sh" if lock else ""), [shift_id]).fetchone()
    if not s:
        raise Problem(404, "shift_not_found")
    return s


def state(c, run):
    """Everything the rules need about one schedule: (workers, by id, demand, current shifts)."""
    ws = store.workers(c, run["location_id"], run["horizon_start"])
    return ws, {w["id"]: w for w in ws}, store.demand(c, run["location_id"], run["horizon_start"]), store.shifts(c, run)


def as_solver(row, run):
    day, start = store.to_slot(run["horizon_start"], row["starts_at"])
    return {"id": row["id"], "worker": row["worker_id"], "day": day, "start": start, "length": int((row["ends_at"] - row["starts_at"]).total_seconds() // 1800), "skill": row["skill"]}


def day_short(demand, shifts, day):
    staffed = {}
    for s in shifts:
        if s["day"] == day:
            for slot in rules.covers(s):
                staffed[(slot, s["skill"])] = staffed.get((slot, s["skill"]), 0) + 1
    gaps = [{"time": store.hhmm(slot), "skill": k, "required": v, "staffed": staffed.get((slot, k), 0)} for (d, slot, k), v in sorted(demand.items()) if d == day and staffed.get((slot, k), 0) < v]
    return {"understaffed_hours": sum(g["required"] - g["staffed"] for g in gaps) / 2, "understaffed_intervals": len(gaps), "gaps": gaps}


# ---------------------------------------------------------------- locations + forecast

@app.get("/v1/locations", tags=["workforce"], summary="Stores, with head-count")
def locations(q: str = "", limit: int = 20, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        rows = c.execute("""SELECT l.id, l.name, l.timezone, l.rule_pack, count(w.id) AS workers FROM locations l LEFT JOIN workers w ON w.home_location_id = l.id AND w.active
                             WHERE l.name ILIKE %s GROUP BY l.id ORDER BY l.name LIMIT %s""", [f"%{q}%", max(1, min(limit, 100))]).fetchall()
        total = c.execute("SELECT (SELECT count(*) FROM locations) AS locations, (SELECT count(*) FROM workers WHERE active) AS workers").fetchone()
    return jsonable_encoder({**total, "items": rows})


class ForecastIn(BaseModel):
    location_id: uuid.UUID
    horizon_start: datetime.date = Field(description="A Monday; the forecast covers that week")
    focus_day: int = Field(5, ge=0, le=6, description="Day of the week to return in half-hour detail (0 = Monday)")


@app.post("/v1/demand:forecast", tags=["forecast"], summary="Forecast half-hourly demand for a store's week and turn it into required workers per skill")
def demand_forecast(body: ForecastIn, ctx: Ctx = Depends(auth("forecast:run")), idem: str | None = IdemKey):
    def work(c):
        if body.horizon_start.weekday() != 0:
            raise Problem(422, "horizon_not_monday", "horizon_start must be a Monday")
        loc = get_location(c, body.location_id)
        tx, promo, rain, wd, last = store.history(c, loc["id"], body.horizon_start)
        if len(tx) < 120 or (body.horizon_start - last).days != 1:
            raise Problem(409, "insufficient_history", "need at least 120 days of actuals ending the day before the horizon")
        future = c.execute("SELECT day, promotion, rain FROM demand_days WHERE location_id = %s AND day >= %s AND day < %s ORDER BY day", [loc["id"], body.horizon_start, body.horizon_start + datetime.timedelta(days=7)]).fetchall()
        if len(future) != 7:
            raise Problem(409, "drivers_missing", "the promotion calendar and weather outlook for that week have not been loaded")
        fc, detail = forecast.forecast(tx, promo, rain, wd, [f["promotion"] for f in future], [f["rain"] for f in future], [f["day"].weekday() for f in future])
        base = forecast.last_week(tx, 7)
        req, req_base = world.required(fc, loc["size"]), world.required(base, loc["size"])
        skills = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM skills")}
        c.execute("DELETE FROM demand_intervals WHERE location_id = %s AND starts_at >= %s AND starts_at < %s", [loc["id"], store.slot_time(body.horizon_start, 0, 0), store.slot_time(body.horizon_start, 7, 0)])
        db.copy(c, "demand_intervals", ["location_id", "skill_id", "starts_at", "ends_at", "required_workers", "forecast_version"],
                [(loc["id"], skills[k], store.slot_time(body.horizon_start, d, s), store.slot_time(body.horizon_start, d, s + 1), int(req[k][d, s]), forecast.VERSION) for k in req for d in range(7) for s in range(world.SLOTS)])
        bt = forecast.backtest(tx, promo, rain, wd, 8)
        f = body.focus_day
        audit.record(c, ctx, "demand.forecast", "location", loc["id"], {"week": body.horizon_start, "model": forecast.VERSION, "backtest_wape": bt["model_wape"]})
        return 200, {"location": loc, "horizon_start": body.horizon_start, "forecast_version": forecast.VERSION, "learned": detail, "backtest": bt, "intervals_written": 7 * world.SLOTS * len(req),
                     "days": [{"date": x["day"], "promotion": x["promotion"], "rain": x["rain"], "transactions": round(float(fc[d].sum())), "same_day_last_week": int(base[d].sum()),
                               "required_hours": {k: float(req[k][d].sum()) / 2 for k in req}} for d, x in enumerate(future)],
                     "focus": {"date": future[f]["day"], "promotion": future[f]["promotion"], "times": [store.hhmm(s) for s in range(world.SLOTS)], "transactions": [round(float(v), 1) for v in fc[f]],
                               "same_day_last_week": [int(v) for v in base[f]], "required": {k: [int(v) for v in req[k][f]] for k in req},
                               "required_total": [int(sum(req[k][f, s] for k in req)) for s in range(world.SLOTS)], "required_total_if_copying_last_week": [int(sum(req_base[k][f, s] for k in req)) for s in range(world.SLOTS)]}}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- schedules

class OptimizeIn(BaseModel):
    location_id: uuid.UUID
    horizon_start: datetime.date
    time_limit_seconds: float = Field(20, ge=2, le=60)


@app.post("/v1/schedules:optimize", status_code=202, tags=["schedules"], summary="Solve a store's week against its forecast and the labor rule pack (job)")
def optimize(body: OptimizeIn, ctx: Ctx = Depends(auth("schedules:write")), idem: str | None = IdemKey):
    def work(c):
        loc = get_location(c, body.location_id)
        if not store.demand(c, loc["id"], body.horizon_start):
            raise Problem(409, "no_forecast", "forecast the week's demand first")
        return 202, jobs.enqueue(c, ctx, "schedule.optimize", body.model_dump(mode="json"))
    return run(ctx, idem, body, work)


@jobs.handler("schedule.optimize")
def optimize_job(c, job):
    p = job["payload"]
    week, loc = datetime.date.fromisoformat(p["horizon_start"]), uuid.UUID(p["location_id"])
    ws, demand = store.workers(c, loc, week), store.demand(c, loc, week)
    by = {w["id"]: w for w in ws}
    base = solver.greedy(ws, demand)
    res = solver.optimize(ws, demand, time_limit=p["time_limit_seconds"])
    if not res["shifts"]:
        raise RuntimeError(f"solver returned {res['status']}")
    s_opt, m_opt = summary(ws, res["shifts"], demand)
    s_base, _ = summary(ws, base["shifts"], demand)
    run_id = uuid.uuid7()
    row = {"id": run_id, "location_id": loc, "horizon_start": week}
    c.execute("""INSERT INTO schedule_runs (id, tenant_id, horizon_start, horizon_end, objective_value, solver_status, runtime_ms, parameters, location_id, summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              [run_id, job["tenant_id"], week, week + datetime.timedelta(days=6), res["objective"], res["status"], res["runtime_ms"],
               Jsonb({"solver": solver.VERSION, "rule_pack": rules.VERSION, "time_limit_seconds": p["time_limit_seconds"], "variables": res["variables"], "best_bound": res["bound"],
                      "weights_cents": {"understaffed_half_hour": solver.W_SHORT, "avoided_half_hour": solver.W_AVOID, "contract_shortfall_half_hour": solver.W_TARGET, "weekend_shift_per_recent_weekend": solver.W_WEEKEND}}),
               loc, Jsonb({"published": s_opt, "baseline": s_base})])
    store.insert_shifts(c, row, res["shifts"], by, {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM skills")}, "optimizer")
    db.many(c, "INSERT INTO rule_violations (id, schedule_run_id, worker_id, rule_code, severity, detail) VALUES (%s,%s,%s,%s,'hard',%s)",      # the independent checker's verdict on the solver's output
            [(uuid.uuid7(), run_id, v["worker"], v["rule_code"], Jsonb(v["detail"])) for v in m_opt["hard_violations"]])
    gap = (res["objective"] - res["bound"]) / max(1.0, res["objective"])
    audit.record(c, system_ctx(job), "schedule.published", "schedule_run", run_id, {"solver": solver.VERSION, "status": res["status"], "labor_cost": s_opt["labor_cost"], "rule_pack": rules.VERSION, "hard_violations": s_opt["hard_violations"]})
    return {"schedule_id": run_id, "solver_status": res["status"], "runtime_ms": res["runtime_ms"], "variables": res["variables"], "optimality_gap": round(gap, 5), "published": s_opt, "baseline": s_base}


@app.get("/v1/schedules/{schedule_id}", tags=["schedules"], summary="Schedule, costs, coverage and violations; one day in half-hour detail")
def schedule(schedule_id: uuid.UUID, day: int = 5, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        r = get_run(c, schedule_id)
        ws, by, demand, shifts = state(c, r)
        loc = get_location(c, r["location_id"])
        violations = c.execute("SELECT worker_id, rule_code, detail FROM rule_violations WHERE schedule_run_id = %s", [schedule_id]).fetchall()
    now, _ = summary(ws, shifts, demand)
    staffed = {}
    for s in shifts:
        if s["day"] == day:
            for slot in rules.covers(s):
                staffed[(slot, s["skill"])] = staffed.get((slot, s["skill"]), 0) + 1
    skills = [k for k, _ in world.SKILLS]
    on_break = [sum(1 for s in shifts if s["day"] == day and rules.break_slot(s) == slot) for slot in range(world.SLOTS)]
    return jsonable_encoder({"id": r["id"], "location": loc, "horizon_start": r["horizon_start"], "horizon_end": r["horizon_end"], "solver_status": r["solver_status"], "runtime_ms": r["runtime_ms"], "parameters": r["parameters"],
                             "published": r["summary"]["published"], "baseline": r["summary"]["baseline"], "current": now, "rule_violations": violations, "rules_checked": sorted(rules.RULES),
                             "by_day": [{"day": d, "date": r["horizon_start"] + datetime.timedelta(days=d), "shifts": sum(s["day"] == d for s in shifts), "paid_hours": sum(rules.paid_slots(s) for s in shifts if s["day"] == d) / 2,
                                         "required_hours": sum(v for (dd, _, _), v in demand.items() if dd == d) / 2} for d in range(7)],
                             "day": {"day": day, "date": r["horizon_start"] + datetime.timedelta(days=day), "times": [store.hhmm(s) for s in range(world.SLOTS)],
                                     "required": {k: [demand.get((day, s, k), 0) for s in range(world.SLOTS)] for k in skills}, "staffed": {k: [staffed.get((s, k), 0) for s in range(world.SLOTS)] for k in skills},
                                     "on_break": on_break, "shifts": [store.describe(s, by) for s in sorted(shifts, key=lambda s: (skills.index(s["skill"]), s["start"], s["length"])) if s["day"] == day]}})


@app.get("/v1/schedules/{schedule_id}/shifts", tags=["schedules"], summary="Assigned shifts, filtered by day, skill and a time they must cover")
def list_shifts(schedule_id: uuid.UUID, day: int | None = None, skill: str | None = None, at: str | None = None, limit: int = 50, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        r = get_run(c, schedule_id)
        _, by, _, shifts = state(c, r)
    slot = None
    if at:
        try:
            h, m = at.split(":")
            slot = (int(h) - world.DAY_START_H) * 2 + int(m) // 30
        except ValueError as e:
            raise Problem(422, "bad_time", "at must look like 18:00") from e
    out = [store.describe(s, by) for s in shifts if (day is None or s["day"] == day) and (skill is None or s["skill"] == skill) and (slot is None or slot in rules.covers(s))]
    return jsonable_encoder({"count": len(out), "items": out[:max(1, min(limit, 500))]})


# ---------------------------------------------------------------- call-outs + repair

class CalloutIn(BaseModel):
    shift_id: uuid.UUID
    reason: str = Field("sick", max_length=200)


@app.post("/v1/callouts", status_code=201, tags=["callouts"], summary="Record that a worker cannot work an assigned shift; shows the coverage gap it opens")
def callout(body: CalloutIn, ctx: Ctx = Depends(auth("callouts:write")), idem: str | None = IdemKey):
    def work(c):
        sh = get_shift(c, body.shift_id, lock=True)
        if sh["status"] != "assigned":
            raise Problem(409, "shift_not_assigned", f"shift is {sh['status']}")
        r = get_run(c, sh["schedule_run_id"])
        ws, by, demand, shifts = state(c, r)
        s = as_solver(sh, r)
        before = day_short(demand, shifts, s["day"])
        cid = uuid.uuid7()
        c.execute("UPDATE shifts SET status = 'called_out' WHERE id = %s", [sh["id"]])
        c.execute("INSERT INTO callouts (id, shift_id, worker_id, reason, status) VALUES (%s,%s,%s,%s,'open')", [cid, sh["id"], sh["worker_id"], body.reason])
        after = day_short(demand, [x for x in shifts if x["id"] != sh["id"]], s["day"])
        audit.record(c, ctx, "callout.recorded", "callout", cid, {"shift_id": sh["id"], "worker": by[sh["worker_id"]]["employee_number"], "reason": body.reason})
        return 201, {"id": cid, "schedule_id": r["id"], "shift": store.describe(s, by), "reason": body.reason, "status": "open", "understaffed_hours_before": before["understaffed_hours"],
                     "understaffed_hours_now": after["understaffed_hours"], "gaps": after["gaps"][:12]}
    return run(ctx, idem, body, work)


def get_callout(c, callout_id):
    co = c.execute("SELECT * FROM callouts WHERE id = %s", [callout_id]).fetchone()
    if not co:
        raise Problem(404, "callout_not_found")
    return co, get_shift(c, co["shift_id"])      # raises 404 for another tenant's call-out


def rank(ws, by, shifts, target, exclude):
    ok, blocked = rules.candidates([w for w in ws if w["id"] not in exclude], shifts, {k: target[k] for k in ("day", "start", "length", "skill")})
    order = ["CERT_EXPIRED", "REST_11H", "MAX_WEEKLY_HOURS", "MAX_DAYS", "UNAVAILABLE", "DOUBLE_BOOKED", "UNQUALIFIED"]
    blocked.sort(key=lambda b: (min(order.index(r) for r in b["rules"]), by[b["worker"]]["employee_number"]))
    counts = {}
    for b in blocked:
        first = min(b["rules"], key=order.index)
        counts[first] = counts.get(first, 0) + 1
    name = lambda x: {"worker_id": x["worker"], "worker": by[x["worker"]]["name"], "employee_number": by[x["worker"]]["employee_number"], "employment_type": by[x["worker"]]["type"]}      # noqa: E731
    return ([{**name(x), **{k: v for k, v in x.items() if k != "worker"}, "rank": i + 1} for i, x in enumerate(ok)],
            [{**name(b), "rules": b["rules"], "why": "; ".join(rules.RULES[r] for r in b["rules"]), "detail": b["detail"]} for b in blocked], counts)


@app.get("/v1/callouts/{callout_id}/replacements", tags=["callouts"], summary="Rank the workers who can legally take the shift, and count who cannot and why")
def replacements(callout_id: uuid.UUID, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        co, sh = get_callout(c, callout_id)
        r = get_run(c, sh["schedule_run_id"])
        ws, by, _, shifts = state(c, r)
        absent = {x["worker_id"] for x in c.execute("SELECT worker_id FROM callouts k JOIN shifts s ON s.id = k.shift_id WHERE s.schedule_run_id = %s", [r["id"]])}
    target = as_solver(sh, r)
    ok, blocked, counts = rank(ws, by, shifts, target, absent)
    return jsonable_encoder({"callout_id": callout_id, "shift": store.describe(target, by), "eligible": len(ok), "candidates": ok[:6], "not_eligible": len(blocked), "blocked_by_rule": counts,
                             "blocked_examples": [b for b in blocked if b["rules"] != ["UNQUALIFIED"] and b["rules"] != ["DOUBLE_BOOKED"]][:4], "rule_pack": rules.VERSION,
                             "ranking": "added pay including overtime, then the worker's stated dislike of the time, recent weekend load, and how far below contracted hours they are"})


class RepairIn(BaseModel):
    day: int = Field(ge=0, le=6)
    time_limit_seconds: float = Field(5, ge=1, le=30)


@app.post("/v1/schedules/{schedule_id}:repair", tags=["callouts"], summary="Cover the day's open call-outs while changing as few published assignments as possible")
def repair(schedule_id: uuid.UUID, body: RepairIn, ctx: Ctx = Depends(auth("callouts:write")), idem: str | None = IdemKey):
    def work(c):
        r = get_run(c, schedule_id, lock=True)
        ws, by, demand, shifts = state(c, r)
        lo, hi = store.slot_time(r["horizon_start"], body.day, 0), store.slot_time(r["horizon_start"], body.day + 1, 0)
        outs = c.execute("SELECT k.id, k.worker_id FROM callouts k JOIN shifts s ON s.id = k.shift_id WHERE s.schedule_run_id = %s AND k.status = 'open' AND s.starts_at >= %s AND s.starts_at < %s", [r["id"], lo, hi]).fetchall()
        if not outs:
            raise Problem(409, "nothing_to_repair", "no open call-outs on that day")
        absent = {o["worker_id"] for o in outs}
        before = day_short(demand, shifts, body.day)
        cost_before, _ = summary(ws, shifts, demand)
        res = solver.repair(ws, demand, [{k: v for k, v in s.items()} for s in shifts], body.day, absent, body.time_limit_seconds)
        ids = {(s["worker"], s["day"]): s["id"] for s in shifts}
        skills = {x["name"]: x["id"] for x in c.execute("SELECT id, name FROM skills")}
        changes = []
        for ch in res["changes"]:
            b, a = ch["before"], ch["after"]
            if b and a:
                c.execute("UPDATE shifts SET starts_at = %s, ends_at = %s, required_skill_id = %s, source = 'repair' WHERE id = %s",
                          [store.slot_time(r["horizon_start"], a["day"], a["start"]), store.slot_time(r["horizon_start"], a["day"], a["start"] + a["length"]), skills[a["skill"]], ids[(b["worker"], b["day"])]])
            elif a:
                store.insert_shifts(c, r, [a], by, skills, "repair")
            else:
                c.execute("UPDATE shifts SET status = 'cancelled', source = 'repair' WHERE id = %s", [ids[(b["worker"], b["day"])]])
            changes.append({"worker": by[ch["worker"]]["name"], "employee_number": by[ch["worker"]]["employee_number"], "change": "called in" if not b else "released" if not a else "shift changed",
                            "before": store.describe(b, by) if b else None, "after": store.describe(a, by) if a else None})
        new = store.shifts(c, r)
        after = day_short(demand, new, body.day)
        cost_after, m_after = summary(ws, new, demand)
        published = r["summary"]["published"]
        gone = [as_solver(x, r) for x in c.execute("""SELECT sh.id, sh.starts_at, sh.ends_at, sk.name AS skill, k.worker_id FROM callouts k JOIN shifts sh ON sh.id = k.shift_id JOIN skills sk ON sk.id = sh.required_skill_id
                                                       WHERE k.id = ANY(%s)""", [[o["id"] for o in outs]])]
        as_published = day_short(demand, shifts + gone, body.day)                  # the day's gaps had nobody called out
        done = "covered" if after["understaffed_hours"] <= as_published["understaffed_hours"] else "partly_covered"
        c.execute("UPDATE callouts SET status = %s WHERE id = ANY(%s)", [done, [o["id"] for o in outs]])
        audit.record(c, ctx, "schedule.repaired", "schedule_run", r["id"], {"day": body.day, "callouts": len(outs), "assignments_changed": len(changes), "solver": solver.VERSION, "status": res["status"], "hard_violations": cost_after["hard_violations"]})
        return 200, {"schedule_id": r["id"], "day": body.day, "callouts_repaired": len(outs), "solver_status": res["status"], "runtime_ms": res["runtime_ms"], "assignments_on_the_day": sum(s["day"] == body.day for s in new),
                     "assignments_changed": len(changes), "assignments_untouched": sum(s["day"] == body.day for s in new) - sum(1 for ch in changes if ch["after"]), "changes": changes,
                     "understaffed_hours": {"as_published": as_published["understaffed_hours"], "after_callouts": before["understaffed_hours"], "after_repair": after["understaffed_hours"]}, "remaining_gaps": after["gaps"][:8],
                     "labor_cost": {"published": published["labor_cost"], "after_callouts": cost_before["labor_cost"], "after_repair": cost_after["labor_cost"]}, "overtime_hours_after_repair": cost_after["overtime_hours"],
                     "hard_violations_after_repair": cost_after["hard_violations"], "callout_status": done}
    return run(ctx, idem, body, work)


# ---------------------------------------------------------------- shift marketplace

class OrderIn(BaseModel):
    shift_id: uuid.UUID
    expires_in_hours: int = Field(48, ge=1, le=336)


def get_order(c, order_id, lock=False):
    o = c.execute("SELECT * FROM shift_market_orders WHERE id = %s" + (" FOR UPDATE" if lock else ""), [order_id]).fetchone()
    if not o:
        raise Problem(404, "order_not_found")
    return o, get_shift(c, o["shift_id"])


@app.post("/v1/shift-market/orders", status_code=201, tags=["marketplace"], summary="Offer an assigned shift for a colleague to pick up")
def offer(body: OrderIn, ctx: Ctx = Depends(auth("market:write")), idem: str | None = IdemKey):
    def work(c):
        sh = get_shift(c, body.shift_id, lock=True)
        if sh["status"] != "assigned":
            raise Problem(409, "shift_not_assigned", f"shift is {sh['status']}")
        if c.execute("SELECT 1 FROM shift_market_orders WHERE shift_id = %s AND status = 'open'", [sh["id"]]).fetchone():
            raise Problem(409, "already_offered", "this shift already has an open offer")
        r = get_run(c, sh["schedule_run_id"])
        _, by, _, _ = state(c, r)
        oid = uuid.uuid7()
        c.execute("INSERT INTO shift_market_orders (id, shift_id, offered_by, status, expires_at) VALUES (%s,%s,%s,'open', now() + make_interval(hours => %s))", [oid, sh["id"], sh["worker_id"], body.expires_in_hours])
        audit.record(c, ctx, "market.offered", "shift_market_order", oid, {"shift_id": sh["id"], "offered_by": by[sh["worker_id"]]["employee_number"]})
        return 201, {"id": oid, "status": "open", "shift": store.describe(as_solver(sh, r), by), "offered_by": by[sh["worker_id"]]["name"]}
    return run(ctx, idem, body, work)


@app.get("/v1/shift-market/orders/{order_id}/candidates", tags=["marketplace"], summary="Who may pick the offered shift up under the rule pack, and who may not")
def order_candidates(order_id: uuid.UUID, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        o, sh = get_order(c, order_id)
        r = get_run(c, sh["schedule_run_id"])
        ws, by, _, shifts = state(c, r)
    target = as_solver(sh, r)
    ok, blocked, counts = rank(ws, by, [s for s in shifts if s["id"] != sh["id"]], target, {o["offered_by"]})
    return jsonable_encoder({"order_id": order_id, "status": o["status"], "shift": store.describe(target, by), "eligible": len(ok), "candidates": ok[:5], "not_eligible": len(blocked), "blocked_by_rule": counts,
                             "blocked_examples": [b for b in blocked if b["rules"] != ["UNQUALIFIED"] and b["rules"] != ["DOUBLE_BOOKED"]][:4]})


class ClaimIn(BaseModel):
    worker_id: uuid.UUID


@app.post("/v1/shift-market/orders/{order_id}:claim", tags=["marketplace"], summary="Claim an offered shift; refused with the rule codes if it would break the rule pack")
def claim(order_id: uuid.UUID, body: ClaimIn, ctx: Ctx = Depends(auth("market:write")), idem: str | None = IdemKey):
    refused = None

    def work(c):
        nonlocal refused
        o, sh = get_order(c, order_id, lock=True)
        if o["status"] != "open" or sh["status"] != "assigned":
            raise Problem(409, "order_closed", f"order is {o['status']}")
        r = get_run(c, sh["schedule_run_id"])
        ws, by, demand, shifts = state(c, r)
        if body.worker_id not in by:
            raise Problem(404, "worker_not_found", "no such active worker at this store")
        if body.worker_id == o["offered_by"]:
            raise Problem(409, "own_shift", "the offering worker cannot claim their own shift")
        target = as_solver(sh, r)
        w = by[body.worker_id]
        broken = rules.can_take(w, {**target, "worker": w["id"]}, [s for s in shifts if s["worker"] == w["id"]])
        if broken:
            refused = {"order_id": order_id, "worker": w["employee_number"], "rules": sorted({code for code, _ in broken})}
            raise Problem(409, "claim_rejected", f"{w['name']} cannot take this shift: " + "; ".join(f"{code} ({rules.RULES[code]})" for code in sorted({code for code, _ in broken})))
        c.execute("DELETE FROM assignments WHERE shift_id = %s", [sh["id"]])
        c.execute("INSERT INTO assignments (shift_id, worker_id, assignment_cost) VALUES (%s,%s,%s)", [sh["id"], w["id"], round(w["cost"] * rules.paid_slots(target) / 2, 2)])
        c.execute("UPDATE shifts SET source = 'marketplace' WHERE id = %s", [sh["id"]])
        c.execute("UPDATE shift_market_orders SET status = 'claimed', claimed_by = %s, decided_at = now() WHERE id = %s", [w["id"], order_id])
        now, _ = summary(ws, store.shifts(c, r), demand)
        audit.record(c, ctx, "market.claimed", "shift_market_order", order_id, {"shift_id": sh["id"], "from": by[o["offered_by"]]["employee_number"], "to": w["employee_number"], "rules_checked": len(rules.RULES), "rule_pack": rules.VERSION})
        return 200, {"order_id": order_id, "status": "claimed", "shift": store.describe({**target, "worker": w["id"]}, by), "from": by[o["offered_by"]]["name"], "to": w["name"], "rules_checked": sorted(rules.RULES),
                     "hard_violations": now["hard_violations"], "labor_cost_now": now["labor_cost"]}
    try:
        return run(ctx, idem, body, work)
    except Problem:
        if refused:      # the refusal itself is a decision worth keeping; its transaction rolled back, so it is recorded on its own
            with db.tx(ctx.tenant_id) as c:
                audit.record(c, ctx, "market.claim_refused", "shift_market_order", order_id, refused)
        raise


# ---------------------------------------------------------------- fairness

@app.get("/v1/fairness", tags=["fairness"], summary="Hours, weekend and overtime distribution: the greedy baseline, the schedule as published, and as it stands now")
def fairness(schedule_id: uuid.UUID, ctx: Ctx = Depends(auth("schedules:read"))):
    with db.tx(ctx.tenant_id) as c:
        r = get_run(c, schedule_id)
        ws, by, demand, shifts = state(c, r)
        events = c.execute("SELECT source, count(*) AS n FROM shifts WHERE schedule_run_id = %s AND status = 'assigned' GROUP BY 1", [schedule_id]).fetchall()
    now, _ = summary(ws, shifts, demand)
    hours = {w["id"]: 0.0 for w in ws}
    weekend = {w["id"]: 0 for w in ws}
    for s in shifts:
        hours[s["worker"]] += rules.paid_slots(s) / 2
        weekend[s["worker"]] += s["day"] >= 5
    ratio = [hours[w["id"]] / w["target_hours"] for w in ws]
    groups = []
    for t in ("full_time", "part_time"):
        g = [w for w in ws if w["type"] == t]
        groups.append({"employment_type": t, "workers": len(g), "mean_hours": round(float(np.mean([hours[w["id"]] for w in g])), 1), "mean_contracted": round(float(np.mean([w["target_hours"] for w in g])), 1),
                       "max_hours": max(hours[w["id"]] for w in g), "weekend_shifts": sum(weekend[w["id"]] for w in g)})
    heavy = [w for w in ws if w["recent_weekends"] >= 4]
    return jsonable_encoder({"schedule_id": schedule_id, "baseline": r["summary"]["baseline"], "published": r["summary"]["published"], "current": now, "shifts_by_source": {e["source"]: e["n"] for e in events},
                             "hours_vs_contract": {"histogram": [{"range": f"{lo}% to {hi}%", "workers": sum(lo / 100 <= x < hi / 100 for x in ratio)} for lo, hi in ((0, 50), (50, 90), (90, 110), (110, 150), (150, 1000))],
                                                   "spread_std": round(float(np.std(ratio)), 3)},
                             "by_employment_type": groups,
                             "weekend": {"workers_who_worked_4_or_more_of_the_last_8_weekend_days": len(heavy), "their_weekend_shifts_this_week": sum(weekend[w["id"]] for w in heavy),
                                         "their_share_of_weekend_shifts": round(sum(weekend[w["id"]] for w in heavy) / max(1, sum(weekend.values())), 3), "their_share_of_head_count": round(len(heavy) / len(ws), 3)}})
