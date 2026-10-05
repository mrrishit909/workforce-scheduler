"""Reads and writes shared by the API and the worker. Converts between database rows (timestamps) and the solver's world (day, half-hour slot)."""
import datetime
import uuid

import numpy as np

from core import db

from . import rules, world


def slot_time(week, day, slot):
    return datetime.datetime.combine(week + datetime.timedelta(days=day), datetime.time(world.DAY_START_H), datetime.UTC) + datetime.timedelta(minutes=30 * slot)


def to_slot(week, ts):
    """timestamp -> (day of the week being scheduled, half-hour slot); the slot can fall outside 0..32 for availability that spans the night."""
    delta = ts - slot_time(week, 0, 0)
    day = delta.days
    return day, int((delta - datetime.timedelta(days=day)).total_seconds() // 1800)


def workers(c, location_id, week):
    """Active workers of a store in the shape the solver and the rules use."""
    rows = c.execute("""SELECT id, employee_number, name, employment_type AS type, hourly_cost::float AS cost, max_weekly_hours::float AS max_hours, target_weekly_hours::float AS target_hours, recent_weekend_shifts AS recent_weekends
                          FROM workers WHERE home_location_id = %s AND active ORDER BY employee_number""", [location_id]).fetchall()
    by = {r["id"]: {**r, "skills": {}, "expires": {}, "unavail": [], "prefer": [], "avoid": []} for r in rows}
    for r in c.execute("SELECT ws.worker_id, s.name, ws.proficiency, ws.expires_on FROM worker_skills ws JOIN skills s ON s.id = ws.skill_id WHERE ws.worker_id = ANY(%s)", [list(by)]):
        by[r["worker_id"]]["skills"][r["name"]] = r["proficiency"]
        if r["expires_on"]:
            by[r["worker_id"]]["expires"][r["name"]] = r["expires_on"]
    lo, hi = slot_time(week, 0, 0), slot_time(week, 7, 0)
    for r in c.execute("SELECT worker_id, starts_at, ends_at, availability_type, preference_weight::float AS w FROM availability WHERE worker_id = ANY(%s) AND starts_at < %s AND ends_at > %s", [list(by), hi, lo]):
        (d, a), (d2, b) = to_slot(week, r["starts_at"]), to_slot(week, r["ends_at"])
        b = b if d2 == d else world.SLOTS
        by[r["worker_id"]][{"unavailable": "unavail", "preferred": "prefer", "avoid": "avoid"}[r["availability_type"]]].append((d, max(0, a), min(world.SLOTS, b), r["w"]))
    return list(by.values())


def demand(c, location_id, week):
    """{(day, slot, skill): workers} from the latest forecast for the week, or {} if there is none."""
    out = {}
    for r in c.execute("""SELECT s.name, d.starts_at, d.required_workers FROM demand_intervals d JOIN skills s ON s.id = d.skill_id
                           WHERE d.location_id = %s AND d.starts_at >= %s AND d.starts_at < %s AND d.required_workers > 0""", [location_id, slot_time(week, 0, 0), slot_time(week, 7, 0)]):
        day, slot = to_slot(week, r["starts_at"])
        out[(day, slot, r["name"])] = int(r["required_workers"])
    return out


def history(c, location_id, before):
    """-> (tx [days, slots], promo, rain, weekday) for every day with actuals before `before`, oldest first."""
    rows = c.execute("SELECT day, transactions, promotion, rain FROM demand_days WHERE location_id = %s AND day < %s AND transactions IS NOT NULL ORDER BY day", [location_id, before]).fetchall()
    return (np.array([r["transactions"] for r in rows]), np.array([r["promotion"] for r in rows]), np.array([r["rain"] for r in rows]), np.array([r["day"].weekday() for r in rows]), rows[-1]["day"] if rows else None)


def shifts(c, run):
    """Currently assigned shifts of a schedule, solver-shaped, each with its row id."""
    week = run["horizon_start"]
    out = []
    for r in c.execute("""SELECT sh.id, a.worker_id, sh.starts_at, sh.ends_at, s.name AS skill, sh.source FROM shifts sh JOIN assignments a ON a.shift_id = sh.id JOIN skills s ON s.id = sh.required_skill_id
                           WHERE sh.schedule_run_id = %s AND sh.status = 'assigned' ORDER BY sh.starts_at, a.worker_id""", [run["id"]]):
        day, start = to_slot(week, r["starts_at"])
        out.append({"id": r["id"], "worker": r["worker_id"], "day": day, "start": start, "length": int((r["ends_at"] - r["starts_at"]).total_seconds() // 1800), "skill": r["skill"], "source": r["source"]})
    return out


def insert_shifts(c, run, new, by_worker, skill_ids, source):
    """Write solver shifts as shift + assignment rows. Cost is the straight-time pay for the shift; overtime is accounted per week in the run summary."""
    week = run["horizon_start"]
    rows = [(uuid.uuid7(), s) for s in new]
    db.many(c, "INSERT INTO shifts (id, location_id, starts_at, ends_at, required_skill_id, status, source, schedule_run_id) VALUES (%s,%s,%s,%s,%s,'assigned',%s,%s)",
            [(sid, run["location_id"], slot_time(week, s["day"], s["start"]), slot_time(week, s["day"], s["start"] + s["length"]), skill_ids[s["skill"]], source, run["id"]) for sid, s in rows])
    db.many(c, "INSERT INTO assignments (shift_id, worker_id, assignment_cost) VALUES (%s,%s,%s)", [(sid, s["worker"], round(by_worker[s["worker"]]["cost"] * rules.paid_slots(s) / 2, 2)) for sid, s in rows])
    return [sid for sid, _ in rows]


def hhmm(slot):
    return f"{world.DAY_START_H + slot // 2:02d}:{30 * (slot % 2):02d}"


def describe(s, by_worker):
    """A shift as the UI shows it."""
    w = by_worker.get(s["worker"], {})
    b = rules.break_slot(s)
    return {"shift_id": s.get("id"), "worker_id": s["worker"], "worker": w.get("name"), "employee_number": w.get("employee_number"), "day": s["day"], "start": hhmm(s["start"]), "end": hhmm(s["start"] + s["length"]),
            "start_slot": s["start"], "length_slots": s["length"], "skill": s["skill"], "paid_hours": rules.paid_slots(s) / 2, "break": hhmm(b) if b is not None else None, "source": s.get("source")}
