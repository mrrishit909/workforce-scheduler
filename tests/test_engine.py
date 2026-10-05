"""Unit tests for the rule pack, the solver, the repair, the baseline, the forecast and the synthetic world."""
import datetime

import pytest

from ws import forecast, rules, solver, store, world

H = world.HISTORY_DAYS


def person(i=0, **kw):
    return {"id": i, "name": f"P{i}", "type": "full_time", "cost": 20.0, "max_hours": 40, "target_hours": 0, "skills": {"cashier": 3}, "expires": {}, "unavail": [], "prefer": [], "avoid": [], "recent_weekends": 0, **kw}


def sh(day, start, length=12, skill="cashier", worker=0):
    return {"worker": worker, "day": day, "start": start, "length": length, "skill": skill}


@pytest.mark.parametrize("worker,shifts,code", [
    (person(), [sh(0, 4, skill="stockroom")], "UNQUALIFIED"),
    (person(skills={"service_desk": 3}, expires={"service_desk": world.WEEK0 - datetime.timedelta(days=1)}), [sh(0, 4, skill="service_desk")], "CERT_EXPIRED"),
    (person(unavail=[(0, 0, 10)]), [sh(0, 8)], "UNAVAILABLE"),
    (person(), [sh(0, 0, 8), sh(0, 20, 8)], "DOUBLE_BOOKED"),
    (person(), [sh(0, 20, 12), sh(1, 0, 8)], "REST_11H"),                 # ends 22:00, starts 06:00: 8 hours of rest
    (person(max_hours=20), [sh(0, 4, 17), sh(1, 4, 17), sh(2, 4, 17)], "MAX_WEEKLY_HOURS"),
    (person(max_hours=60), [sh(d, 8, 8) for d in range(7)], "MAX_DAYS"),
    (person(), [sh(0, 4, 10)], "SHIFT_SHAPE"),
    (person(), [sh(0, 24, 12)], "SHIFT_SHAPE"),                           # would run past closing
])
def test_each_rule_fires(worker, shifts, code):
    assert code in {c for c, _ in rules.check(worker, shifts)}


def test_a_legal_week_passes_and_boundaries_are_inclusive():
    w = person(skills={"service_desk": 3}, expires={"service_desk": world.WEEK0}, unavail=[(0, 0, 8)], max_hours=40)
    week = [sh(0, 8, 17, "service_desk"), sh(1, 5, 17, "service_desk"), sh(2, 8, 17, "service_desk"), sh(3, 8, 17, "service_desk"), sh(4, 8, 17, "service_desk")]      # exactly 40 paid hours, certificate valid through its last day
    assert rules.check(w, week) == []
    late = sh(0, 12, 17, "service_desk")                                    # 12:00 to 20:30
    assert rules.check(w, [late, sh(1, 3, 8, "service_desk")]) == []        # 07:30 next day: exactly 11 h
    assert "REST_11H" in {c for c, _ in rules.check(w, [late, sh(1, 2, 8, "service_desk")])}      # 07:00: 10.5 h


def test_break_is_unpaid_and_not_counted_as_coverage():
    long, short = sh(0, 4, 17), sh(0, 4, 12)
    assert rules.paid_slots(long) == 16 and rules.break_slot(long) == 12 and 12 not in rules.covers(long) and len(rules.covers(long)) == 16
    assert rules.paid_slots(short) == 12 and rules.break_slot(short) is None


def small():
    ws = [person(i, cost=18 + i, skills={"cashier": 3, **({"stockroom": 2} if i % 2 else {})}, target_hours=16, max_hours=30, avoid=[(d, 24, 32, 2) for d in range(7)] if i < 3 else []) for i in range(9)]
    ws[0]["unavail"] = [(0, 0, 32), (1, 0, 32)]
    demand = {(d, s, "cashier"): 2 for d in range(7) for s in range(4, 28)} | {(d, s, "stockroom"): 1 for d in range(7) for s in range(0, 8)}
    return ws, demand


def test_solver_covers_demand_legally_and_beats_first_fit():
    ws, demand = small()
    res, base = solver.optimize(ws, demand, time_limit=8), solver.greedy(ws, demand)
    m, mb = rules.metrics(ws, res["shifts"], demand), rules.metrics(ws, base["shifts"], demand)
    assert res["status"] in ("OPTIMAL", "FEASIBLE") and m["hard_violations"] == [] and mb["hard_violations"] == []
    assert m["understaffed_hours"] == 0 and m["labor_cost"] <= mb["labor_cost"] and m["overtime_hours"] == 0
    assert not any(s["worker"] == 0 and s["day"] in (0, 1) for s in res["shifts"])


def test_solver_reports_the_gap_when_demand_cannot_be_met():
    ws, demand = small()
    res = solver.optimize(ws[:2], demand, time_limit=4)
    m = rules.metrics(ws[:2], res["shifts"], demand)
    assert m["understaffed_hours"] > 50 and m["hard_violations"] == [] and all(h <= 30 for h in [sum(rules.paid_slots(s) for s in res["shifts"] if s["worker"] == w["id"]) / 2 for w in ws[:2]])


def test_repair_freezes_other_days_keeps_the_absent_off_and_changes_little():
    ws, demand = small()
    plan = solver.optimize(ws, demand, time_limit=8)["shifts"]
    gone = next(s for s in plan if s["day"] == 3)
    left = [s for s in plan if s is not gone]
    rep = solver.repair(ws, demand, left, 3, {gone["worker"]}, time_limit=4)
    key = lambda s: (s["worker"], s["day"], s["start"], s["length"], s["skill"])      # noqa: E731
    assert sorted(key(s) for s in rep["shifts"] if s["day"] != 3) == sorted(key(s) for s in left if s["day"] != 3)
    assert not any(s["worker"] == gone["worker"] and s["day"] == 3 for s in rep["shifts"])
    m = rules.metrics(ws, rep["shifts"], demand)
    assert m["hard_violations"] == [] and 1 <= len(rep["changes"]) <= 3
    free = solver.repair(ws, demand, left, 3, {gone["worker"]}, time_limit=4, change_weight=0)
    assert len(rep["changes"]) <= len(free["changes"])


def test_repair_with_nobody_absent_changes_nothing():
    ws, demand = small()
    plan = solver.optimize(ws, demand, time_limit=8)["shifts"]
    if rules.metrics(ws, plan, demand)["understaffed_hours"] == 0:
        assert solver.repair(ws, demand, plan, 2, set(), time_limit=4)["changes"] == []


def test_candidates_are_legal_and_the_blocked_carry_a_reason():
    ws, demand = small()
    plan = solver.optimize(ws, demand, time_limit=8)["shifts"]
    target = sh(5, 4, 12)
    ok, blocked = rules.candidates(ws, plan, {k: target[k] for k in ("day", "start", "length", "skill")})
    assert len(ok) + len(blocked) == len(ws) and all(b["rules"] for b in blocked)
    by = {w["id"]: w for w in ws}
    for c in ok:
        assert rules.can_take(by[c["worker"]], {**target, "worker": c["worker"]}, [s for s in plan if s["worker"] == c["worker"]]) == []
    assert [c["added_cost"] for c in ok[:2]] == sorted(c["added_cost"] for c in ok)[:2] or any(c["avoid_weight_hours"] for c in ok)


def test_world_is_deterministic_and_sized_as_specified():
    w = world.build()
    assert len(w["locations"]) == 50 and sum(len(x["workers"]) for x in w["locations"]) == 5000 and w["locations"][0]["tx"].shape == (H + 7, world.SLOTS)
    assert w["locations"][0]["tx"][:, :world.OPEN_SLOT].sum() == 0
    world.build.cache_clear()
    again = world.build()
    assert (again["locations"][3]["tx"] == w["locations"][3]["tx"]).all() and again["locations"][3]["workers"][5] == w["locations"][3]["workers"][5]


def test_forecast_learns_the_promotion_and_beats_copying_last_week():
    w = world.build()
    loc = w["locations"][0]
    f, d = forecast.forecast(loc["tx"][:H], loc["promo"][:H], loc["rain"][:H], w["weekday"][:H], loc["promo"][H:], loc["rain"][H:], w["weekday"][H:])
    assert abs(d["promotion_uplift"] - loc["uplift"]) < 0.08 and abs(d["rain_effect"] - loc["damp"]) < 0.05
    actual, last = loc["tx"][H + 5].sum(), loc["tx"][H - 2].sum()
    assert abs(f[5].sum() - actual) < abs(last - actual)                    # the promotion Saturday
    bt = [forecast.backtest(x["tx"][:H], x["promo"][:H], x["rain"][:H], w["weekday"][:H], 6) for x in w["locations"][:10]]
    assert sum(b["model_daily_wape"] < b["baseline_daily_wape"] for b in bt) >= 9


def test_staffing_standard_and_time_conversion():
    req = world.required([[0] * 4 + [40] * 28], 1.0)
    assert req["cashier"][0, 0] == 0 and req["cashier"][0, 10] == 3 and req["service_desk"][0, 10] == 1 and req["stockroom"][0, 0] == 3
    t = store.slot_time(world.WEEK0, 5, 9)
    assert store.to_slot(world.WEEK0, t) == (5, 9) and t.hour == 10 and t.minute == 30 and store.hhmm(9) == "10:30"
