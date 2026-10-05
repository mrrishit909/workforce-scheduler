"""demand-forecast: half-hourly transactions for a store, turned into required workers by the staffing standard.

Method: strip the known drivers (promotion, rain) out of history, average the last six same-weekdays slot by slot, apply a damped
recent trend, then put the drivers of the day being forecast back in. Baseline: the same weekday last week, as it happened.
"""
import numpy as np

VERSION = "seasonal-drivers-1.0.0"
WEEKS = 6


def effects(tx, promo, rain, weekday):
    """Learn this store's promotion uplift and rain damping from its own history: ratio of a driver day to the median plain day of that weekday."""
    daily = tx.sum(axis=1).astype(float)
    plain = ~promo & ~rain
    base = np.array([np.median(daily[plain & (weekday == d)]) if (plain & (weekday == d)).any() else np.median(daily) for d in range(7)])[weekday]
    ratio = daily / np.maximum(base, 1)
    up = float(np.median(ratio[promo & ~rain])) if (promo & ~rain).sum() >= 5 else 1.0
    damp = float(np.median(ratio[rain & ~promo])) if (rain & ~promo).sum() >= 5 else 1.0
    return max(1.0, up), min(1.0, damp)


def forecast(tx, promo, rain, weekday, future_promo, future_rain, future_weekday):
    """tx [days, slots] history (oldest first); driver flags per day -> (forecast [future days, slots], details)."""
    up, damp = effects(tx[-364:], promo[-364:], rain[-364:], weekday[-364:])
    mult = np.where(promo, up, 1.0) * np.where(rain, damp, 1.0)
    clean = tx / mult[:, None]
    recent, before = clean[-28:].sum(), clean[-56:-28].sum()
    trend = 1 + 0.5 * (recent / max(before, 1) - 1)                          # half of the last month's growth, carried forward
    out = []
    for p, r, d in zip(future_promo, future_rain, future_weekday):
        same = clean[weekday == d][-WEEKS:]
        out.append(same.mean(axis=0) * trend * (up if p else 1.0) * (damp if r else 1.0))
    return np.array(out), {"promotion_uplift": round(up, 3), "rain_effect": round(damp, 3), "trend": round(float(trend), 4), "weeks_averaged": WEEKS, "model": VERSION}


def last_week(tx, n_future):
    """Baseline: each future day is a copy of the same weekday one week earlier."""
    return np.array([tx[-7 + (i % 7)] for i in range(n_future)], dtype=float)


def wape(actual, predicted):
    return float(np.abs(np.asarray(actual) - np.asarray(predicted)).sum() / max(1.0, np.asarray(actual).sum()))


def backtest(tx, promo, rain, weekday, weeks=8):
    """Rolling origin over the last `weeks` weeks: forecast each week from everything before it. -> {model, baseline} WAPE at slot level, plus per-day errors."""
    a, m, b = [], [], []
    for k in range(weeks, 0, -1):
        cut = len(tx) - 7 * k
        f, _ = forecast(tx[:cut], promo[:cut], rain[:cut], weekday[:cut], promo[cut:cut + 7], rain[cut:cut + 7], weekday[cut:cut + 7])
        a.append(tx[cut:cut + 7]), m.append(f), b.append(last_week(tx[:cut], 7))
    a, m, b = np.concatenate(a), np.concatenate(m), np.concatenate(b)
    return {"weeks": weeks, "model_wape": round(wape(a, m), 4), "baseline_wape": round(wape(a, b), 4), "model_daily_wape": round(wape(a.sum(1), m.sum(1)), 4), "baseline_daily_wape": round(wape(a.sum(1), b.sum(1)), 4)}


if __name__ == "__main__":
    from . import world
    w = world.build()
    H = world.HISTORY_DAYS
    res = [backtest(x["tx"][:H], x["promo"][:H], x["rain"][:H], w["weekday"][:H], 12) for x in w["locations"]]
    print({k: round(float(np.mean([r[k] for r in res])), 4) for k in res[0]})
    x = w["locations"][0]
    f, d = forecast(x["tx"][:H], x["promo"][:H], x["rain"][:H], w["weekday"][:H], x["promo"][H:], x["rain"][H:], w["weekday"][H:])
    print(d, "true uplift", round(x["uplift"], 3), round(x["damp"], 3), "sat fc", int(f[5].sum()), "actual", int(x["tx"][H + 5].sum()), "last sat", int(x["tx"][H - 2].sum()))
    for day in range(7):
        req_f, req_a = world.required(f[day], x["size"]), world.required(x["tx"][H + day], x["size"])
        print(day, int(f[day].sum()), int(x["tx"][H + day].sum()), {k: (int(req_f[k].sum()), int(req_a[k].sum())) for k in req_f})
