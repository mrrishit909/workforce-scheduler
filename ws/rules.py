"""rules-service: the labor rule pack, as one independent checker. The solver, the repair, replacement ranking and the shift
marketplace all answer "is this legal?" by calling this file, and schedules the solver produces are re-checked with it.

A shift is {worker, day, start, length, skill} in half-hour slots (see world.py). Shifts of 8.5 hours carry a 30-minute unpaid break.
"""
from .world import SLOTS

LENGTHS = (8, 12, 17)             # 4 h, 6 h, 8.5 h on site (8 h paid)
BREAK_AFTER = 8                   # the break is the ninth half-hour of a long shift
REST_SLOTS = 22                   # 11 hours between the end of one shift and the start of the next
OVERTIME_AFTER_H, OVERTIME_PREMIUM = 40, 0.5
MAX_DAYS = 6
VERSION = "retail-us-generic-1.0"
RULES = {"UNQUALIFIED": "does not hold the skill", "CERT_EXPIRED": "certificate for the skill has lapsed", "UNAVAILABLE": "overlaps a time the worker is unavailable",
         "DOUBLE_BOOKED": "already has a shift that day", "REST_11H": "less than 11 hours of rest between shifts", "MAX_WEEKLY_HOURS": "would exceed the worker's weekly maximum",
         "MAX_DAYS": "would work all seven days", "SHIFT_SHAPE": "not an allowed shift length or outside opening hours"}


def break_slot(shift):
    return shift["start"] + BREAK_AFTER if shift["length"] > 12 else None


def paid_slots(shift):
    return shift["length"] - (1 if shift["length"] > 12 else 0)


def covers(shift):
    b = break_slot(shift)
    return [s for s in range(shift["start"], shift["start"] + shift["length"]) if s != b]


def overlap(shift, windows):
    """Half-hours of the shift inside any (day, first, end, ...) window."""
    return sum(max(0, min(shift["start"] + shift["length"], w[2]) - max(shift["start"], w[1])) for w in windows if w[0] == shift["day"])


def check(worker, shifts, on=None):
    """Every rule broken by this worker's set of shifts -> [(code, detail)]. Empty means legal."""
    from . import world
    out = []
    valid = world.valid_skills(worker, on or world.WEEK0)
    by_day = {}
    for s in shifts:
        if s["length"] not in LENGTHS or s["start"] < 0 or s["start"] + s["length"] > SLOTS:
            out.append(("SHIFT_SHAPE", {"day": s["day"], "start": s["start"], "length": s["length"]}))
        if s["skill"] not in worker["skills"]:
            out.append(("UNQUALIFIED", {"skill": s["skill"]}))
        elif s["skill"] not in valid:
            out.append(("CERT_EXPIRED", {"skill": s["skill"], "expired_on": str(worker["expires"][s["skill"]])}))
        if overlap(s, worker["unavail"]):
            out.append(("UNAVAILABLE", {"day": s["day"]}))
        if s["day"] in by_day:
            out.append(("DOUBLE_BOOKED", {"day": s["day"]}))
        by_day[s["day"]] = s
    for d, s in by_day.items():
        nxt = by_day.get(d + 1)
        if nxt and (SLOTS_PER_DAY - (s["start"] + s["length"])) + nxt["start"] < REST_SLOTS:
            out.append(("REST_11H", {"days": [d, d + 1], "rest_hours": ((SLOTS_PER_DAY - (s["start"] + s["length"])) + nxt["start"]) / 2}))
    hours = sum(paid_slots(s) for s in shifts) / 2
    if hours > worker["max_hours"]:
        out.append(("MAX_WEEKLY_HOURS", {"hours": hours, "max": worker["max_hours"]}))
    if len(by_day) > MAX_DAYS:
        out.append(("MAX_DAYS", {"days": len(by_day)}))
    return out


SLOTS_PER_DAY = 48                # a calendar day in half-hours: slot 0 is 06:00, so the next day's slot 0 is 48 slots later


def can_take(worker, shift, current, on=None):
    """Rules that adding `shift` to the worker's `current` shifts would break."""
    return check(worker, list(current) + [shift], on)


def metrics(workers, shifts, demand):
    """Cost, coverage and preference numbers for a set of shifts against {(day, slot, skill): required}."""
    by_id = {w["id"]: w for w in workers}
    staffed, per = {}, {}
    for s in shifts:
        per.setdefault(s["worker"], []).append(s)
        for slot in covers(s):
            staffed[(s["day"], slot, s["skill"])] = staffed.get((s["day"], slot, s["skill"]), 0) + 1
    short = {k: v - staffed.get(k, 0) for k, v in demand.items() if staffed.get(k, 0) < v}
    over = sum(max(0, n - demand.get(k, 0)) for k, n in staffed.items())
    cost = paid = ot = avoid = prefer = 0.0
    violations = []
    for wid, ss in per.items():
        w = by_id[wid]
        h = sum(paid_slots(s) for s in ss) / 2
        o = max(0.0, h - OVERTIME_AFTER_H)
        paid, ot = paid + h, ot + o
        cost += w["cost"] * (h + OVERTIME_PREMIUM * o)
        avoid += sum(overlap(s, w["avoid"]) for s in ss) / 2
        prefer += sum(overlap(s, w["prefer"]) for s in ss) / 2
        violations += [{"worker": wid, "rule_code": c, "detail": d} for c, d in check(w, ss)]
    need = sum(demand.values())
    on_site = sum(s["length"] for s in shifts) / 2
    weekend = [sum(s["day"] >= 5 for s in per.get(w["id"], [])) for w in workers]
    hrs = {w["id"]: sum(paid_slots(s) for s in per.get(w["id"], [])) / 2 for w in workers}
    return {"labor_cost": round(cost, 2), "paid_hours": paid, "overtime_hours": ot, "shifts": len(shifts), "workers_scheduled": len(per), "demand_hours": need / 2,
            "understaffed_intervals": len(short), "understaffed_hours": sum(short.values()) / 2, "overstaffed_hours": over / 2, "fill_rate": round(1 - sum(short.values()) / max(1, need), 4),
            "hours_in_avoided_times": avoid, "share_of_hours_in_preferred_times": round(prefer / max(on_site, 1e-9), 4), "preference_score": round(100 * (1 - avoid / max(on_site, 1e-9)), 1),
            "workers_on_both_weekend_days": sum(x >= 2 for x in weekend),
            "weekend_shifts": sum(weekend), "weekend_shifts_to_people_who_just_worked_four": sum(x for x, w in zip(weekend, workers) if w["recent_weekends"] >= 4),
            "workers_below_contracted_hours": sum(hrs[w["id"]] < w["target_hours"] for w in workers), "hours_below_contract": sum(max(0.0, w["target_hours"] - hrs[w["id"]]) for w in workers),
            "workers_with_overtime": sum(h > OVERTIME_AFTER_H for h in hrs.values()), "hard_violations": violations, "short": short}


def candidates(workers, shifts, shift):
    """Who could legally take `shift` (a dict without its current worker), best first, and who cannot and why.
    Ranking: added pay including any overtime premium, then how much the worker would dislike it, then recent weekend load."""
    per = {}
    for s in shifts:
        per.setdefault(s["worker"], []).append(s)
    ok, blocked = [], []
    for w in workers:
        cand = {**shift, "worker": w["id"]}
        mine = per.get(w["id"], [])
        broken = can_take(w, cand, mine)
        if broken:
            blocked.append({"worker": w["id"], "rules": sorted({c for c, _ in broken}), "detail": broken[0][1]})
            continue
        before = sum(paid_slots(s) for s in mine) / 2
        after = before + paid_slots(cand) / 2
        ot = max(0.0, after - OVERTIME_AFTER_H) - max(0.0, before - OVERTIME_AFTER_H)
        dislike = sum(a[3] * max(0, min(cand["start"] + cand["length"], a[2]) - max(cand["start"], a[1])) for a in w["avoid"] if a[0] == cand["day"]) / 2
        ok.append({"worker": w["id"], "added_cost": round(w["cost"] * (paid_slots(cand) / 2 + OVERTIME_PREMIUM * ot), 2), "overtime_hours": ot, "hours_before": before, "hours_after": after,
                   "below_contract_by": max(0.0, w["target_hours"] - before), "avoid_weight_hours": dislike, "recent_weekends": w["recent_weekends"], "proficiency": w["skills"][cand["skill"]],
                   "wants_it": overlap(cand, w["prefer"]) > 0})
    ok.sort(key=lambda c: (c["added_cost"] + 15 * c["avoid_weight_hours"] + (3 * c["recent_weekends"] if cand["day"] >= 5 else 0) - 2 * min(c["below_contract_by"], 8), str(c["worker"])))
    return ok, blocked
