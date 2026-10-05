"""Tenants, 50 stores, 5,000 workers with skills and availability for the week to schedule, and two years of half-hourly demand.

    python -m ws.seed [--if-empty]
"""
import datetime
import hashlib
import sys
import uuid

from psycopg.types.json import Jsonb

from core import db

from . import forecast, rules, solver, store, world

TENANTS = {"acme": ("Acme Retail", 9, world.N_LOCATIONS), "other": ("Second Tenant Stores", 10, 2)}      # name, world seed, stores loaded
ROLES = ["scheduler", "viewer", "admin"]
TYPES = {"unavail": "unavailable", "prefer": "preferred", "avoid": "avoid"}


def token(tenant, role):
    """Demo credentials for local use only."""
    return f"{tenant}-{role}-demo"


def main(if_empty=False):
    db.migrate()
    with db.tx() as c:
        if if_empty and c.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            return print("already seeded")
    out = {}
    for key, (name, seed, n_loc) in TENANTS.items():
        w = world.build(seed)
        t = uuid.uuid5(uuid.NAMESPACE_DNS, f"{key}.workforce.example")
        with db.tx() as c:
            c.execute("INSERT INTO tenants (id, name) VALUES (%s,%s)", [t, name])
            for role in ROLES:
                c.execute("INSERT INTO api_tokens VALUES (%s,%s,%s,%s,%s)", [hashlib.sha256(token(key, role).encode()).hexdigest(), t, uuid.uuid5(t, role), f"{role} ({name})", role])
        locs = w["locations"][:n_loc]
        lid = {x["i"]: uuid.uuid5(t, f"loc-{x['i']}") for x in locs}
        sid = {k: uuid.uuid5(t, f"skill-{k}") for k, _ in world.SKILLS}
        day0 = world.WEEK0 - datetime.timedelta(days=world.HISTORY_DAYS)
        with db.tx(t) as c:
            db.load(c, "skills", ["id", "tenant_id", "name", "certification_required"], [(sid[k], t, k, cert) for k, cert in world.SKILLS])
            db.load(c, "locations", ["id", "tenant_id", "name", "timezone", "size", "rule_pack"], [(lid[x["i"]], t, x["name"], x["timezone"], x["size"], rules.VERSION) for x in locs])
            people = [(uuid.uuid5(t, p["employee_number"]), p) for x in locs for p in x["workers"]]
            db.load(c, "workers", ["id", "tenant_id", "employee_number", "home_location_id", "employment_type", "hourly_cost", "max_weekly_hours", "name", "target_weekly_hours", "recent_weekend_shifts"],
                    [(wid, t, p["employee_number"], lid[p["location"]], p["type"], p["cost"], p["max_hours"], p["name"], p["target_hours"], p["recent_weekends"]) for wid, p in people])
            db.copy(c, "worker_skills", ["worker_id", "skill_id", "proficiency", "expires_on"], [(wid, sid[k], prof, p["expires"].get(k)) for wid, p in people for k, prof in p["skills"].items()])
            db.copy(c, "availability", ["id", "worker_id", "starts_at", "ends_at", "availability_type", "preference_weight"],
                    [(uuid.uuid7(), wid, world.slot_time(a[0], a[1]), world.slot_time(a[0], a[2]), TYPES[kind], a[3] if len(a) > 3 else 1) for wid, p in people for kind in TYPES for a in p[kind]])
            db.copy(c, "demand_days", ["location_id", "day", "transactions", "promotion", "rain"],
                    [(lid[x["i"]], day0 + datetime.timedelta(days=d), x["tx"][d].tolist() if d < world.HISTORY_DAYS else None, bool(x["promo"][d]), bool(x["rain"][d])) for x in locs for d in range(w["days"])])
        out[key] = {"tenant": t, "world": w, "locations": lid, "skills": sid}
        print(f"{name}: {len(locs)} stores, {len(people)} workers, {len(locs) * world.HISTORY_DAYS:,} store-days of half-hourly demand")
    with db.tx() as c:
        c.execute("INSERT INTO model_registry (name, version, data_snapshot, metrics, baseline) VALUES (%s,%s,%s,%s,%s), (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                  ["demand-forecast", forecast.VERSION, "synthetic world, seed 9", Jsonb({"see": "docs/evaluation.md"}), Jsonb({"name": "same weekday last week"}),
                   "schedule-optimizer", solver.VERSION, f"rule pack {rules.VERSION}", Jsonb({"see": "docs/evaluation.md"}), Jsonb({"name": "greedy first-fit"})])
    print("demo tokens:", ", ".join(token(t, r) for t in TENANTS for r in ROLES))
    return out


if __name__ == "__main__":
    main("--if-empty" in sys.argv)
