# Performance

Docker stack (one uvicorn process, one worker, Postgres 16) after the demo: 50 stores, 5,000 workers, 36,400 store-days of
half-hourly demand, one published schedule of 241 shifts. Reproduce with `make demo && make load-test`.

## Reads: store list, schedule with one day in half-hour detail, fairness report (in rotation), closed loop, 10 s per level

| Concurrency | Requests | Throughput | p50 | p95 | p99 | Errors |
|---|---|---|---|---|---|---|
| 1 | 899 | 89.8 req/s | 12.4 ms | 15.0 ms | 16.9 ms | 0.00% |
| 8 | 1084 | 107.8 req/s | 73.4 ms | 110.2 ms | 128.1 ms | 0.00% |
| 32 | 940 | 91.8 req/s | 284.5 ms | 333.3 ms | 353.2 ms | 0.00% |

Hardware: arm64, 12 cores, client and server on the same machine. Target: p95 under 750 ms for interactive reads: met.

Throughput is flat at about 100 requests a second because every schedule and fairness read reloads the store's workers,
skills, availability, demand and shifts and recomputes all metrics and the rule check in Python, on one process. Nothing is cached.
A manager's screen does not need more; a mobile app for 5,000 workers reading their own shifts would want a narrower query.

## Solves

| Work | Time |
|---|---|
| Week schedule, 61 workers, 21,182 yes / no decisions, 896 coverage constraints | stops at the 20 s limit, feasible, within 0.3% of the proven bound (0.03% to 0.6% across the six stores in evaluation.md) |
| First-fit baseline for the same week | under 0.1 s |
| Call-out repair: one day, other days frozen | 290 to 380 ms solve, 444 ms for the whole request |
| Forecast a week + 8-week backtest + write 896 demand intervals | 34 ms |
| Replacement ranking, marketplace candidates, claim validation | 10 to 16 ms |
| Seed: two tenants, 5,152 workers, 37,856 demand days | about 3 s |

The weekly solve runs in the worker inside one database transaction, eight solver threads. It was only tried on stores of 61 to
69 workers. Decision count grows with workers x days x start times x lengths x skills, so a 150-worker store would need a longer
limit or a decomposition (by day with weekly-hours budgets, or by skill group); that was not built or measured.

Not measured: all 50 stores solved concurrently, multi-week horizons, or 15-minute intervals.
