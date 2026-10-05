"""Deterministic synthetic retailer: 50 stores, 5,000 hourly workers, two years of half-hourly demand, and the week to schedule.

Everything is invented. `build()` is pure, so the seed, the evaluation and the tests share one world.
Time is in slots: a day runs 06:00 to 22:00 in 32 half-hour slots; the store is open to customers from 08:00 (slot 4).
"""
import datetime
import functools
import math
import random

import numpy as np

SLOTS, DAY_START_H, OPEN_SLOT = 32, 6, 4
SKILLS = [("cashier", False), ("sales_floor", False), ("stockroom", False), ("service_desk", True)]      # (name, certification required)
WEEK0 = datetime.date(2026, 10, 12)          # Monday of the week being scheduled
HISTORY_DAYS = 728                           # two years of actuals before it
N_LOCATIONS, N_WORKERS = 50, 5000
FIRST = "Avery Blake Casey Devon Emery Finley Harper Jordan Kendall Logan Morgan Parker Quinn Reese Rowan Sage Taylor Alex Jamie Robin Drew Skyler Cameron Dana Elliot Noor Kai Remy Shay Tatum".split()
LAST = "Abbott Barlow Castell Draper Ellery Fenwick Garrow Hollis Ingram Jessop Kirkby Lowell Merrow Norcott Oakes Pruett Quill Radley Stanton Thorne Underhill Vance Whitlow Yardley Ashby Brandt Corwin Dunmore".split()
CITIES = "Ashford Brookvale Cedar Pinecrest Dunmore Eastgate Fairhaven Glenrock Harbor Calloway Ironwood Juniper Kingsport Lakeside Marlow Northfield Oakridge".split(" ")
WEEKDAY = [0.86, 0.82, 0.86, 0.92, 1.1, 1.45, 1.18]      # Monday first


def slot_time(day, slot):
    """Wall-clock start of a slot in the scheduled week. Stored as UTC; see README on time zones."""
    return datetime.datetime.combine(WEEK0 + datetime.timedelta(days=day), datetime.time(DAY_START_H), datetime.UTC) + datetime.timedelta(minutes=30 * slot)


def intraday():
    """Share of a day's transactions in each slot: nothing before opening, a lunch peak and a larger early-evening peak."""
    x = np.arange(SLOTS)
    curve = 0.35 + np.exp(-((x - 13) / 3.0) ** 2) + 1.25 * np.exp(-((x - 24) / 3.5) ** 2)
    curve[:OPEN_SLOT] = 0
    curve[-1] *= 0.5
    return curve / curve.sum()


def required(tx, size):
    """Staffing standard: half-hourly transactions -> workers needed per skill. tx: array [..., SLOTS] -> {skill: int array}."""
    tx = np.asarray(tx, dtype=float)
    open_ = np.arange(SLOTS) >= OPEN_SLOT
    stock = np.where(np.arange(SLOTS) < 8, math.ceil(2.5 * size), np.where(np.arange(SLOTS) < 26, math.ceil(0.8 * size), 1))
    return {"cashier": np.where(open_, np.maximum(1, np.ceil(tx / 16)), 0).astype(int), "sales_floor": np.where(open_, np.ceil(tx / 26) + 1, 0).astype(int),
            "stockroom": np.broadcast_to(stock, tx.shape).astype(int), "service_desk": np.where(open_, 1 + (tx > 290 * size), 0).astype(int)}


def demand_dict(tx_week, size):
    """{(day, slot, skill): workers} for one location's week of transactions [7, SLOTS]."""
    req = required(tx_week, size)
    return {(d, s, k): int(req[k][d, s]) for k in req for d in range(tx_week.shape[0]) for s in range(SLOTS) if req[k][d, s] > 0}


@functools.lru_cache(maxsize=2)
def build(seed=9):
    rng, nrng = random.Random(seed), np.random.default_rng(seed)
    days = HISTORY_DAYS + 7
    sizes = [rng.uniform(0.55, 1.5) for _ in range(N_LOCATIONS)]
    sizes[0] = 0.5                                                            # the demo store: mid-sized, so its week solves in seconds
    doy = np.array([(WEEK0 - datetime.timedelta(days=HISTORY_DAYS - i)).timetuple().tm_yday for i in range(days)])
    wd = np.array([(WEEK0 - datetime.timedelta(days=HISTORY_DAYS - i)).weekday() for i in range(days)])
    season = 1 + 0.08 * np.sin(2 * np.pi * (doy - 100) / 365) + 0.35 * np.exp(-((doy - 355) / 12.0) ** 2)
    trend = 1 + 0.05 * np.arange(days) / 365
    curve = intraday()
    locations = []
    for i, size in enumerate(sizes):
        promo = nrng.random(days) < 0.09
        rain = nrng.random(days) < 0.24
        if i == 0:
            promo[HISTORY_DAYS:] = [False, False, False, False, False, True, False]      # the demo Saturday carries a promotion
            rain[HISTORY_DAYS:] = [False, True, False, False, False, False, False]
            promo[HISTORY_DAYS - 7] = False                                              # and last Saturday did not: "same as last week" will be short
        uplift, damp = rng.uniform(1.15, 1.35), rng.uniform(0.85, 0.95)
        daily = 3900 * size * np.array(WEEKDAY)[wd] * season * trend * np.where(promo, uplift, 1) * np.where(rain, damp, 1) * nrng.normal(1, 0.045, days)
        tx = nrng.poisson(np.outer(daily, curve))                             # [days, SLOTS]
        locations.append({"i": i, "name": f"{CITIES[i % len(CITIES)]} #{i + 1:02d}", "size": size, "timezone": "America/New_York", "tx": tx, "promo": promo, "rain": rain, "uplift": uplift, "damp": damp})
    # head-count per store in proportion to its demand, 5,000 in all
    need = [sum(v.sum() for v in required(loc["tx"][HISTORY_DAYS - 28:HISTORY_DAYS], loc["size"]).values()) for loc in locations]
    counts = [max(20, round(N_WORKERS * n / sum(need))) for n in need]
    counts[-1] += N_WORKERS - sum(counts)
    n = 0
    for loc, count in zip(locations, counts):
        loc["workers"] = [_worker(rng, n + j, loc["i"]) for j in range(count)]
        n += count
    desk = [x for x in locations[0]["workers"] if "service_desk" in x["skills"]]
    desk[0]["expires"]["service_desk"] = WEEK0 - datetime.timedelta(days=20)      # the demo store has a lapsed certificate for the rules to catch
    return {"locations": locations, "days": days, "weekday": wd, "seed": seed}


def _worker(rng, n, loc):
    full = rng.random() < 0.42
    skills = {rng.choices(["cashier", "sales_floor", "stockroom"], [40, 38, 22])[0]: 3}
    for k in ("cashier", "sales_floor", "stockroom"):
        if k not in skills and rng.random() < (0.45 if full else 0.3):
            skills[k] = rng.choice([1, 2])
    expires = {}
    if rng.random() < (0.3 if full else 0.08):                                # the service desk needs a certificate, and some have lapsed
        skills["service_desk"] = rng.choice([2, 3])
        expires["service_desk"] = WEEK0 + datetime.timedelta(days=rng.randrange(-90, -1) if rng.random() < 0.15 else rng.randrange(30, 600))
    unavail, prefer, avoid = [], [], []                                       # (day, first slot, end slot[, weight])
    kind = rng.random()
    if not full and kind < 0.35:                                              # students: no weekday mornings
        unavail += [(d, 0, 18) for d in range(5)]
    elif not full and kind < 0.55:                                            # second job: no evenings early in the week
        unavail += [(d, 20, SLOTS) for d in range(3)]
    for d in rng.sample(range(7), rng.choice([0, 1, 1, 2])):                  # fixed days off
        unavail.append((d, 0, SLOTS))
    p = rng.random()
    if p < 0.35:
        prefer += [(d, 0, 18, 1) for d in range(7)]                           # likes mornings
        avoid += [(d, 24, SLOTS, rng.choice([1, 2])) for d in range(7)]       # dislikes closing
    elif p < 0.6:
        prefer += [(d, 14, SLOTS, 1) for d in range(7)]
        avoid += [(d, 0, 6, rng.choice([1, 2])) for d in range(7)]            # dislikes the 6am start
    if rng.random() < 0.3:
        avoid += [(d, 0, SLOTS, rng.choice([1, 2, 3])) for d in (5, 6)]       # would rather not work weekends
    return {"n": n, "employee_number": f"E{n + 1:05d}", "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}", "location": loc, "type": "full_time" if full else "part_time",
            "cost": round(rng.uniform(19, 27) if full else rng.uniform(15.5, 21), 2), "max_hours": 48 if full else rng.choice([20, 24, 28, 30]), "target_hours": 36 if full else rng.choice([12, 16, 20]),
            "skills": skills, "expires": expires, "unavail": unavail, "prefer": prefer, "avoid": avoid, "recent_weekends": min(8, int(rng.expovariate(1 / 2.5)))}


def valid_skills(w, on=WEEK0):
    return {k for k in w["skills"] if k not in w["expires"] or w["expires"][k] >= on}


if __name__ == "__main__":
    w = build()
    loc = w["locations"][0]
    dem = demand_dict(loc["tx"][HISTORY_DAYS:], loc["size"])
    hours = sum(dem.values()) / 2
    print(len(w["locations"]), "stores,", sum(len(x["workers"]) for x in w["locations"]), "workers; demo store:", len(loc["workers"]), "workers,", round(hours), "demand hours,",
          "capacity", sum(x["max_hours"] for x in loc["workers"]), "peak", max(sum(dem.get((5, s, k), 0) for k, _ in SKILLS) for s in range(SLOTS)))
    print({k: sum(v for (d, s, kk), v in dem.items() if kk == k) / 2 for k, _ in SKILLS}, {k: sum(k in valid_skills(x) for x in loc["workers"]) for k, _ in SKILLS})
    print("sat tx", loc["tx"][HISTORY_DAYS + 5].tolist())
