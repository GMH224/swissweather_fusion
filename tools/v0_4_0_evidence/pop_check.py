"""Is the published rain probability calibrated? (C7 evidence)

Usage: python3 -I pop_check.py <swf.db> <swf_data dir>
Fused probability = mean of the sources' precip_probability (what the
integration publishes), each source's newest forecast issued at least L
hours before the target hour. Truth: Taenikon gauge >= 0.1 mm in the hour.
"""
import bisect, csv, glob, os, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB, DATA = sys.argv[1], sys.argv[2]
UTC = timezone.utc
gauge, gn = defaultdict(float), defaultdict(int)
for path in glob.glob(os.path.join(DATA, "station/tae/*/tae_t_*.csv")):
    with open(path, encoding="cp1252") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            if row["rre150z0"] == "":
                continue
            t = datetime.strptime(row["reference_timestamp"], "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
            end = t if t.minute == 0 else t.replace(minute=0) + timedelta(hours=1)
            gauge[end.timestamp()] += float(row["rre150z0"])
            gn[end.timestamp()] += 1
# The forecast for hour H covers precipitation in the hour ENDING at H in Open-Meteo
# convention ("preceding hour sum"); probabilities likewise.
wet = {t: gauge[t] >= 0.1 for t in gauge if gn[t] >= 5}

c = sqlite3.connect(f"file:{DB}?immutable=1", uri=True)
fc = defaultdict(list)
for src, iss, va, v in c.execute(
        "SELECT source, issued_at, valid_at, value FROM forecast_snapshots WHERE variable='precip_probability' "
        "AND value IS NOT NULL AND source != 'blend' AND valid_at >= '2026-09-20'"):
    fc[(src, datetime.fromisoformat(va).timestamp())].append((datetime.fromisoformat(iss).timestamp(), v))
for k in fc:
    fc[k].sort()
sources = sorted({k[0] for k in fc})
hours = sorted({k[1] for k in fc if k[1] in wet})


def prob(t, lead):
    vals = []
    for s in sources:
        lst = fc.get((s, t))
        if not lst:
            continue
        i = bisect.bisect_right(lst, (t - lead * 3600, 1e9)) - 1
        if i >= 0:
            vals.append(lst[i][1])
    return sum(vals) / len(vals) / 100 if len(vals) >= 3 else None


for lead in (3, 24):
    pts = [(t, prob(t, lead), wet[t]) for t in hours]
    pts = [p for p in pts if p[1] is not None]
    base = sum(p[2] for p in pts) / len(pts)
    bs = sum((p[1] - p[2]) ** 2 for p in pts) / len(pts)
    bsc = sum((base - p[2]) ** 2 for p in pts) / len(pts)
    print(f"\nlead {lead} h: n={len(pts)} rain frequency={base:.3f}  Brier={bs:.4f} clim={bsc:.4f} skill={1 - bs / bsc:+.3f}")
    bins = [(0, .1), (.1, .2), (.2, .3), (.3, .5), (.5, .7), (.7, 1.01)]
    for lo, hi in bins:
        sel = [p for p in pts if lo <= p[1] < hi]
        if sel:
            print(f"  forecast {lo:.0%}-{min(hi,1):.0%}: n={len(sel):4d} mean forecast {sum(p[1] for p in sel)/len(sel):.2f}  observed {sum(p[2] for p in sel)/len(sel):.2f}")
    # out-of-sample bin calibration: fit on first half, test on second half
    pts.sort()
    half = len(pts) // 2
    tr, te = pts[:half], pts[half:]
    table = {}
    for lo, hi in bins:
        sel = [p for p in tr if lo <= p[1] < hi]
        if len(sel) >= 20:
            table[(lo, hi)] = sum(p[2] for p in sel) / len(sel)
    def cal(p):
        for (lo, hi), f in table.items():
            if lo <= p < hi:
                return f
        return p
    b_raw = sum((p[1] - p[2]) ** 2 for p in te) / len(te)
    b_cal = sum((cal(p[1]) - p[2]) ** 2 for p in te) / len(te)
    clim_tr = sum(p[2] for p in tr) / len(tr)
    b_cl = sum((clim_tr - p[2]) ** 2 for p in te) / len(te)
    print(f"  second half: Brier raw {b_raw:.4f}  calibrated {b_cal:.4f}  climatology {b_cl:.4f}  "
          f"skill raw {1-b_raw/b_cl:+.3f} calibrated {1-b_cal/b_cl:+.3f}")
