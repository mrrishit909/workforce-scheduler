"""schedule-optimizer and reoptimization-service (CP-SAT), plus the greedy baseline a manager with a spreadsheet would produce.

Hard: one shift a day, qualification and valid certificate, availability, 11 h rest, weekly maximum, a day off, break inside long shifts.
Soft: understaffing (heavily), labor cost with overtime premium, working at times the worker asked to avoid, falling short of
      contracted hours, weekend shifts for people who have just worked several.
"""
import time

from ortools.sat.python import cp_model

from . import rules
from .world import SKILLS, SLOTS

VERSION = "cpsat-schedule-1.0.0"
W_SHORT, W_AVOID, W_PREFER, W_TARGET, W_WEEKEND, W_BOTH, W_CHANGE = 6000, 150, 40, 400, 600, 600, 3000      # cents per half-hour (short, avoid, prefer, target); per shift (weekend, change)
STARTS = range(0, SLOTS - 7, 2)                                                                     # shifts start on the hour


def options(worker, day, frozen=()):
    """Legal (start, length, skill) for a worker on a day, given shifts already fixed on other days."""
    from . import world
    out = []
    for start in STARTS:
        for length in rules.LENGTHS:
            if start + length > SLOTS:
                continue
            for skill in world.valid_skills(worker):
                s = {"worker": worker["id"], "day": day, "start": start, "length": length, "skill": skill}
                if not rules.overlap(s, worker["unavail"]) and not (frozen and rules.can_take(worker, s, frozen)):
                    out.append(s)
    return out


def _solve(model, limit, hint_workers=8):
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = limit
    solver.parameters.num_workers = hint_workers
    solver.parameters.random_seed = 9
    t = time.perf_counter()
    status = solver.Solve(model)
    return solver, solver.StatusName(status), int(1000 * (time.perf_counter() - t))


def optimize(workers, demand, days=range(7), time_limit=20.0):
    """-> {shifts, status, runtime_ms, objective, bound}. workers: world-shaped dicts with an `id`."""
    m = cp_model.CpModel()
    x, cover, cost_terms = {}, {}, []
    for w in workers:
        per_day, half_hours, weekend = {}, [], []
        for d in days:
            opts = options(w, d)
            vs = []
            for o in opts:
                v = m.NewBoolVar("")
                x[(w["id"], d, o["start"], o["length"], o["skill"])] = v
                vs.append((o, v))
                for slot in rules.covers(o):
                    cover.setdefault((d, slot, o["skill"]), []).append(v)
                paid = rules.paid_slots(o)
                half_hours.append(paid * v)
                cost_terms.append(v * int(round(w["cost"] * 50 * paid + W_AVOID * sum(a[3] * max(0, min(o["start"] + o["length"], a[2]) - max(o["start"], a[1])) for a in w["avoid"] if a[0] == d)
                                               - W_PREFER * rules.overlap(o, w["prefer"]) + (W_WEEKEND * w["recent_weekends"] if d >= 5 else 0))))
            if vs:
                m.AddAtMostOne(v for _, v in vs)
            per_day[d] = vs
        for d in days:                                                        # 11 hours of rest: a late finish forbids an early start the next day
            if d + 1 in per_day:
                for end in sorted({o["start"] + o["length"] for o, _ in per_day[d]}):
                    early = [v for o, v in per_day[d + 1] if (rules.SLOTS_PER_DAY - end) + o["start"] < rules.REST_SLOTS]
                    if early:
                        m.AddAtMostOne([v for o, v in per_day[d] if o["start"] + o["length"] >= end] + early) if end == max(o["start"] + o["length"] for o, _ in per_day[d]) else \
                            m.Add(sum(v for o, v in per_day[d] if o["start"] + o["length"] >= end) + sum(early) <= 1)
        total = sum(half_hours)
        m.Add(total <= int(2 * w["max_hours"]))
        worked = [m.NewBoolVar("") for _ in days]
        for b, d in zip(worked, days):
            m.Add(sum(v for _, v in per_day[d]) == b)
        m.Add(sum(worked) <= rules.MAX_DAYS)
        if 5 in days and 6 in days:                                           # a whole weekend worked is worth avoiding when someone else can take a day of it
            both = m.NewBoolVar("")
            m.Add(both >= worked[list(days).index(5)] + worked[list(days).index(6)] - 1)
            cost_terms.append(both * W_BOTH)
        ot = m.NewIntVar(0, 2 * 12, "")
        m.Add(ot >= total - 2 * rules.OVERTIME_AFTER_H)
        short = m.NewIntVar(0, int(2 * w["target_hours"]), "")
        m.Add(short >= int(2 * w["target_hours"]) - total)
        deep = m.NewIntVar(0, int(w["target_hours"]), "")                    # falling below half of contracted hours costs three times as much per hour: spread the shortfall
        m.Add(deep >= int(w["target_hours"]) - total)
        cost_terms += [ot * int(round(w["cost"] * 50 * rules.OVERTIME_PREMIUM)), short * W_TARGET, deep * 2 * W_TARGET]
    for key, need in demand.items():
        if key[0] not in days:
            continue
        u = m.NewIntVar(0, need, "")
        m.Add(sum(cover.get(key, [])) + u >= need)
        cost_terms.append(u * W_SHORT)
    m.Minimize(sum(cost_terms))
    solver, status, ms = _solve(m, time_limit)
    shifts = [{"worker": k[0], "day": k[1], "start": k[2], "length": k[3], "skill": k[4]} for k, v in x.items() if status in ("OPTIMAL", "FEASIBLE") and solver.Value(v)]
    return {"shifts": shifts, "status": status, "runtime_ms": ms, "objective": solver.ObjectiveValue() if shifts else None, "bound": solver.BestObjectiveBound() if shifts else None, "variables": len(x), "solver": VERSION}


def repair(workers, demand, shifts, day, absent, time_limit=5.0, change_weight=W_CHANGE):
    """After call-outs on `day`: keep every other day frozen and change as few of that day's assignments as possible.
    absent: worker ids who cannot work that day. -> {shifts (whole week), changes, status, runtime_ms}."""
    m = cp_model.CpModel()
    today = {s["worker"]: s for s in shifts if s["day"] == day}
    other = {}
    for s in shifts:
        if s["day"] != day:
            other.setdefault(s["worker"], []).append(s)
    x, cover, terms, changed = {}, {}, [], []
    for w in workers:
        if w["id"] in absent:
            continue
        cur = today.get(w["id"])
        hours_other = sum(rules.paid_slots(s) for s in other.get(w["id"], [])) / 2
        vs = []
        for o in options(w, day, other.get(w["id"], [])):
            v = m.NewBoolVar("")
            x[(w["id"], o["start"], o["length"], o["skill"])] = v
            vs.append((o, v))
            for slot in rules.covers(o):
                cover.setdefault((day, slot, o["skill"]), []).append(v)
            h = hours_other + rules.paid_slots(o) / 2
            pay = w["cost"] * (rules.paid_slots(o) / 2 + rules.OVERTIME_PREMIUM * max(0.0, h - max(rules.OVERTIME_AFTER_H, hours_other)))
            terms.append(v * int(round(100 * pay + W_AVOID * sum(a[3] * max(0, min(o["start"] + o["length"], a[2]) - max(o["start"], a[1])) for a in w["avoid"] if a[0] == day))))
        if vs:
            m.AddAtMostOne(v for _, v in vs)
        same = [v for o, v in vs if cur and (o["start"], o["length"], o["skill"]) == (cur["start"], cur["length"], cur["skill"])]
        c = m.NewBoolVar("")                                                   # 1 if this worker's day differs from the published schedule
        if cur and same:
            m.Add(same[0] + c == 1)
        elif cur:
            m.Add(c == 1)
        else:
            m.Add(sum(v for _, v in vs) == c)
        changed.append(c)
    shorts = []
    for key, need in demand.items():
        if key[0] == day:
            u = m.NewIntVar(0, need, "")
            m.Add(sum(cover.get(key, [])) + u >= need)
            shorts.append(u)
    m.Minimize(W_SHORT * sum(shorts) + change_weight * sum(changed) + sum(terms))
    solver, status, ms = _solve(m, time_limit)
    new_today = [{"worker": k[0], "day": day, "start": k[1], "length": k[2], "skill": k[3]} for k, v in x.items() if solver.Value(v)]
    key = lambda s: (s["start"], s["length"], s["skill"])      # noqa: E731
    now = {s["worker"]: s for s in new_today}
    changes = [{"worker": wid, "before": today.get(wid), "after": now.get(wid)} for wid in sorted(set(today) | set(now), key=str)
               if wid not in absent and (wid in today) + (wid in now) and (wid not in today or wid not in now or key(today[wid]) != key(now[wid]))]
    return {"shifts": [s for s in shifts if s["day"] != day] + new_today, "changes": changes, "status": status, "runtime_ms": ms, "solver": VERSION}


def greedy(workers, demand, days=range(7)):
    """Baseline, the way a careful manager fills a spreadsheet: walk the day slot by slot; wherever a skill is short, start a shift
    there (long if the gap lasts, short if not) and give it to the legal worker furthest below their contracted hours.
    Legal by construction, but it never revisits a choice."""
    shifts, mine, staffed = [], {w["id"]: [] for w in workers}, {}
    hours = {w["id"]: 0.0 for w in workers}
    for d in days:
        for skill, _ in SKILLS:
            for slot in range(SLOTS):
                while staffed.get((d, slot, skill), 0) < demand.get((d, slot, skill), 0):
                    best = None
                    for length in sorted(rules.LENGTHS, reverse=True):
                        start = min(slot - slot % 2, SLOTS - length)
                        gap = sum(staffed.get((d, s, skill), 0) < demand.get((d, s, skill), 0) for s in range(start, start + length))
                        if gap >= 0.7 * length or length == rules.LENGTHS[0]:
                            for w in sorted(workers, key=lambda w: hours[w["id"]] - w["target_hours"]):
                                s = {"worker": w["id"], "day": d, "start": start, "length": length, "skill": skill}
                                if slot in rules.covers(s) and not rules.can_take(w, s, mine[w["id"]]):
                                    best = s
                                    break
                        if best:
                            break
                    if not best:
                        break                                                  # nobody legal is left for this slot: it stays short
                    mine[best["worker"]].append(best)
                    hours[best["worker"]] += rules.paid_slots(best) / 2
                    shifts.append(best)
                    for s in rules.covers(best):
                        staffed[(d, s, skill)] = staffed.get((d, s, skill), 0) + 1
    return {"shifts": shifts, "status": "GREEDY", "runtime_ms": 0, "solver": "greedy-first-fit"}
