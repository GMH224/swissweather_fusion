"""Does the BUILT learning code compute what the replay computed?

On a copy of the real database (already opened once by v0.4.0, so
migrated): every temperature/humidity/pressure row valid on 14 Nov is set
back to 'pending'. Then
  (a) the built ModelALearningCoordinator._reconcile runs once (frozen at
      15 Nov 00:40 UTC);
  (b) an independent implementation of the replay formula (alpha =
      1 - 0.5**(1/(3k)), k rows per bucket and target hour; predict-then-
      update; nearest observation within 30 min) runs on the same rows
      from the same starting buckets.
Every bucket touched must agree.

Usage: python3 wrap.py <v040 tree> learn_equiv.py <db copy>
"""
import bisect, sys
from collections import defaultdict
from datetime import datetime, timezone

from freezegun import freeze_time

sys.path.insert(0, sys.argv[1])
DB = sys.argv[2]
NOW = datetime(2026, 11, 15, 0, 40, tzinfo=timezone.utc)
LO, HI = "2026-11-14T00:00:00+00:00", "2026-11-15T00:00:00+00:00"

with freeze_time(NOW):
    from swissweather_fusion.storage.db import SwissWeatherDB
    from swissweather_fusion import coordinator as coord
    from tests.test_v0_2_1_fixes import FakeHass
    db = SwissWeatherDB(DB)
    c = db._conn
    c.execute("UPDATE forecast_snapshots SET reconciliation_status='pending' WHERE valid_at >= ? AND valid_at < ? "
              "AND variable IN ('temperature','humidity','pressure') AND value IS NOT NULL", (LO, HI))
    c.execute("UPDATE forecast_snapshots SET reconciliation_status='reconciled' WHERE valid_at < ? "
              "AND reconciliation_status='pending' AND variable IN ('temperature','humidity','pressure')", (LO,))
    c.execute("UPDATE forecast_snapshots SET reconciliation_status='reconciled' WHERE valid_at >= ? "
              "AND reconciliation_status='pending' AND variable IN ('temperature','humidity','pressure')", (HI,))
    c.commit()
    before = {(r[0], r[1], r[2], r[3], r[4]): (r[5], r[6], r[7]) for r in c.execute(
        "SELECT hour_of_day, season, lead_time_bucket, source, measurement, ema_bias, ema_abs_error, sample_count FROM bucket_stats")}
    rows = c.execute("SELECT * FROM forecast_snapshots WHERE reconciliation_status='pending' "
                     "AND variable IN ('temperature','humidity','pressure') ORDER BY valid_at ASC").fetchall()
    obs = c.execute("SELECT ts, temperature, humidity, pressure FROM station_observations "
                    "WHERE ts >= '2026-11-13T23:00' AND ts <= ?", (NOW.isoformat(),)).fetchall()
    print("pending rows:", len(rows))

    # (b) independent replay formula
    P = lambda s: datetime.fromisoformat(s)
    ob = {m: sorted((P(o[0]), o[i]) for o in obs if o[i] is not None)
          for i, m in ((1, "temperature"), (2, "humidity"), (3, "pressure"))}
    def actual(m, t):
        lst = ob[m]; ts = [x[0] for x in lst]; i = bisect.bisect_left(ts, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(lst) and abs((lst[j][0] - t).total_seconds()) <= 1800:
                if best is None or abs((lst[j][0] - t).total_seconds()) < abs((lst[best][0] - t).total_seconds()):
                    best = j
        return lst[best][1] if best is not None else None
    def leadb(iss, va):
        h = (va - iss).total_seconds() / 3600
        return "short" if h < 24 else ("medium" if h < 72 else "long")
    matched = []
    for r in rows:
        va = P(r["valid_at"]); y = actual(r["variable"], va)
        if y is None:
            continue
        key = (va.hour, "ALL", leadb(P(r["issued_at"]), va), r["source"], r["variable"])
        matched.append((key, va, r["value"], y))
    k = defaultdict(int)
    for key, va, _, _ in matched:
        k[(key, va)] += 1
    state = dict(before)
    for key, va, f, y in matched:
        b, e, n = state.get(key, (0.0, 0.0, 0))
        a = 1 - 0.5 ** (1 / (3.0 * k[(key, va)]))
        before_err = abs((f - b) - y)
        if n == 0:
            e, b = before_err, f - y
        else:
            e = a * before_err + (1 - a) * e
            b = a * (f - y) + (1 - a) * b
        state[key] = (b, e, n + 1)

    # (a) built code
    coord.ModelALearningCoordinator(FakeHass(), db)._reconcile()
    after = {(r[0], r[1], r[2], r[3], r[4]): (r[5], r[6], r[7]) for r in c.execute(
        "SELECT hour_of_day, season, lead_time_bucket, source, measurement, ema_bias, ema_abs_error, sample_count FROM bucket_stats")}
    touched = {key for key, *_ in matched}
    worst = max(max(abs(after[key][0] - state[key][0]), abs(after[key][1] - state[key][1])) for key in touched)
    counts_ok = all(after[key][2] == state[key][2] for key in touched)
    untouched_ok = all(after[key] == before[key] for key in before if key not in touched)
    print(f"rows learned: {len(matched)}  buckets touched: {len(touched)}  "
          f"max |difference|: {worst:.2e}  sample counts equal: {counts_ok}  untouched buckets unchanged: {untouched_ok}")
    db.close()
