"""Radar steering backtest: does wind-steered radar sampling warn of rain
better than Model B's fixed south-west points?

Python port of swf_radar_backtest.m (backtest toolkit v6), run on the
collector archive. Usage:

    python3 -I radar_backtest.py <swf_data dir> <out.json>

For every hour t0 with a CombiPrecip (CPC) picture and lead L = 1, 2, 3 h
it asks each method "will it rain at Taenikon in the hour ending t0 + L?"
using only the radar picture valid at t0 and the steering wind at t0.
Truth: Taenikon rain gauge (rre150z0, 10-minute sums; >= OBS_THR mm in the
hour = rain).

Methods (a method warns when any of its pixels holds >= RADAR_THR mm):
  local        the station pixel
  fixed_sw     v0.3.6 Model B's three points: 225 deg, 30/45/70 km
  modelb       local OR fixed_sw  (what Model B actually reacts to)
  steered_pt   one pixel upwind at distance v*L
  sector       wedge +/- HALF_ANGLE around the upwind bearing,
               distances v*(L-0.5) .. v*(L+0.5)
  cone         same wedge, distances 0 .. v*(L+0.5)
  radius       all directions, 0 .. max(10 km, v*(L+0.5))
  hybrid       cone when v >= WEAK_KMH, radius 30 km otherwise
Steering wind: mean of the 700 and 500 hPa wind vectors, ICON-D2.
"""
import csv, glob, json, math, os, random, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import h5py
import numpy as np

DATA, OUT = sys.argv[1], sys.argv[2]
STATION_E, STATION_N = 2710518.0, 1259824.0  # Taenikon, LV95 (MeteoSwiss metadata)
RADAR_THR = 0.5     # mm in the past hour == RADAR_PRECIP_ACCUM_MM_THRESHOLD
OBS_THR = 0.3       # mm per hour at the gauge == "it rains"
HALF_ANGLE = 25.0
WEAK_KMH = 15.0
LEADS = (1, 2, 3)
FIXED_BEARING, FIXED_KM = 225.0, (30.0, 45.0, 70.0)
UTC = timezone.utc
random.seed(7)

# ---- truth: gauge, hour ending at HH:00 ------------------------------------
gauge = defaultdict(float)
gauge_n = defaultdict(int)
for path in sorted(glob.glob(os.path.join(DATA, "station/tae/*/tae_t_*.csv"))):
    with open(path, encoding="cp1252") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            v = row.get("rre150z0", "")
            if v == "":
                continue
            t = datetime.strptime(row["reference_timestamp"], "%d.%m.%Y %H:%M").replace(tzinfo=UTC)
            # 10-minute value labelled at the END of its interval; it belongs
            # to the hour ending at the next full hour (HH:00 belongs to HH:00).
            end = t if t.minute == 0 else (t.replace(minute=0) + timedelta(hours=1))
            gauge[end] += float(v)
            gauge_n[end] += 1
truth = {t: gauge[t] >= OBS_THR for t in gauge if gauge_n[t] >= 5}  # >= 5 of 6 records

# ---- steering wind ---------------------------------------------------------
steer = json.load(open(os.path.join(DATA, "steer.json")))
H = steer["hourly"]
wind = {}
for i, ts in enumerate(H["time"]):
    t = datetime.fromisoformat(ts).replace(tzinfo=UTC)
    u = v = 0.0
    ok = True
    for lev in ("700hPa", "500hPa"):
        s = H[f"wind_speed_{lev}_icon_d2"][i]
        d = H[f"wind_direction_{lev}_icon_d2"][i]
        if s is None or d is None:
            ok = False
            break
        # direction is where the wind comes FROM; vector points downwind
        u += -s * math.sin(math.radians(d)) / 2
        v += -s * math.cos(math.radians(d)) / 2
    if ok:
        speed = math.hypot(u, v)
        from_bearing = (math.degrees(math.atan2(-u, -v)) + 360) % 360
        wind[t] = (speed, from_bearing)

# ---- radar -----------------------------------------------------------------
files = sorted(glob.glob(os.path.join(DATA, "radar/cpc/*/*/*/cpc_*.h5")))
with h5py.File(files[0], "r") as h:
    E = h["E"][:]
    N = h["N"][:]
EE, NN = np.meshgrid(E, N)          # raw[row=N, col=E]
dE = (EE - STATION_E) / 1000.0
dN = (NN - STATION_N) / 1000.0
DIST = np.hypot(dE, dN)
BEAR = (np.degrees(np.arctan2(dE, dN)) + 360) % 360   # bearing FROM station TO pixel
iS = int(np.argmin(np.abs(N - STATION_N)))
jS = int(np.argmin(np.abs(E - STATION_E)))


def pixel_at(bearing, km):
    e = STATION_E + km * 1000 * math.sin(math.radians(bearing))
    n = STATION_N + km * 1000 * math.cos(math.radians(bearing))
    if not (E.min() - 500 <= e <= E.max() + 500 and N.min() - 500 <= n <= N.max() + 500):
        return None
    return int(np.argmin(np.abs(N - n))), int(np.argmin(np.abs(E - e)))


def angdiff(a, b):
    return np.abs((a - b + 180) % 360 - 180)


FIXED = [pixel_at(FIXED_BEARING, k) for k in FIXED_KM]

METHODS = ("local", "fixed_sw", "modelb", "steered_pt", "sector", "cone", "radius", "hybrid")
records = []   # (day, lead, wind_class, dry_at_issue, {method: warn}, truth)
georef = defaultdict(list)  # shift -> [(radar, gauge)]
missing_radar = 0
for path in files:
    with h5py.File(path, "r") as h:
        raw = h["raw"][:]
        gain, offset = float(h.attrs["gain"]), float(h.attrs["offset"])
        t0 = datetime.strptime(h.attrs["valid_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    R = raw * gain + offset
    R[np.isinf(raw)] = 0.0
    if t0.minute != 0:
        continue
    wet = np.nan_to_num(R, nan=0.0) >= RADAR_THR
    # georeferencing check: radar pixel at shifts vs gauge, same hour
    if t0 in gauge and gauge_n[t0] >= 5:
        for di in range(-4, 5):
            for dj in range(-4, 5):
                val = R[iS + di, jS + dj]
                if not np.isnan(val):
                    georef[(di, dj)].append((val, gauge[t0]))
    if t0 not in wind:
        missing_radar += 1
        continue
    speed, upwind = wind[t0]
    local = bool(wet[iS, jS])
    fixed = any(wet[p] for p in FIXED if p is not None)
    dry_issue = (not local) and (t0 in truth) and (not truth[t0])
    wedge = angdiff(BEAR, upwind) <= HALF_ANGLE
    for L in LEADS:
        tv = t0 + timedelta(hours=L)
        if tv not in truth:
            continue
        reach = speed * (L + 0.5)
        p = pixel_at(upwind, speed * L)
        w = {
            "local": local,
            "fixed_sw": fixed,
            "modelb": local or fixed,
            "steered_pt": bool(wet[p]) if p is not None else False,
            "sector": bool(np.any(wet & wedge & (DIST >= speed * (L - 0.5)) & (DIST <= reach))),
            "cone": bool(np.any(wet & ((wedge & (DIST <= reach)) | (DIST <= 1.0)))),
            "radius": bool(np.any(wet & (DIST <= max(10.0, reach)))),
        }
        w["hybrid"] = w["cone"] if speed >= WEAK_KMH else bool(np.any(wet & (DIST <= 30.0)))
        wclass = "strong" if speed >= WEAK_KMH else "weak"
        records.append((t0.strftime("%Y-%m-%d"), L, wclass, dry_issue, w, truth[tv]))


def scores(recs, m):
    a = b = c = d = 0
    for r in recs:
        f, o = r[4][m], r[5]
        if f and o: a += 1
        elif f and not o: b += 1
        elif o: c += 1
        else: d += 1
    n = a + b + c + d
    pod = a / (a + c) if a + c else float("nan")
    far = b / (a + b) if a + b else float("nan")
    csi = a / (a + b + c) if a + b + c else float("nan")
    exp = ((a + b) * (a + c) + (c + d) * (b + d)) / n if n else 0
    hss = (a + d - exp) / (n - exp) if n - exp else float("nan")
    return {"hits": a, "false": b, "miss": c, "cn": d, "pod": pod, "far": far, "csi": csi, "hss": hss}


def boot_diff(recs, m1, m2, B=2000):
    by = defaultdict(list)
    for r in recs:
        by[r[0]].append(r)
    days = list(by)
    base = scores(recs, m1)["hss"] - scores(recs, m2)["hss"]
    res = []
    for _ in range(B):
        sample = [r for _ in days for r in by[random.choice(days)]]
        s1, s2 = scores(sample, m1)["hss"], scores(sample, m2)["hss"]
        if not (math.isnan(s1) or math.isnan(s2)):
            res.append(s1 - s2)
    res.sort()
    return [round(base, 3), round(res[int(.025 * len(res))], 3), round(res[int(.975 * len(res))], 3)]


out = {"hours_radar": len(files), "records": len(records), "skipped_no_wind": missing_radar,
       "wet_hours_truth": sum(truth.values()), "period": [files[0][-15:-3], files[-1][-15:-3]],
       "fixed_points_inside_grid": [p is not None for p in FIXED], "results": {}}
for subset in ("all", "dry_at_issue"):
    for L in LEADS:
        for wc in ("all", "weak", "strong"):
            recs = [r for r in records if r[1] == L and (subset == "all" or r[3])
                    and (wc == "all" or r[2] == wc)]
            if not recs:
                continue
            key = f"{subset}|L{L}|{wc}"
            out["results"][key] = {"n": len(recs), "rain": sum(r[5] for r in recs),
                                   **{m: {k: (round(v, 3) if isinstance(v, float) else v)
                                          for k, v in scores(recs, m).items()} for m in METHODS}}
            if wc == "all":
                out["results"][key]["hss_minus_modelb"] = {
                    m: boot_diff(recs, m, "modelb") for m in METHODS if m != "modelb"}
# georeferencing: correlation by shift
corr = {}
for (di, dj), pairs in georef.items():
    x = np.array([p[0] for p in pairs]); y = np.array([p[1] for p in pairs])
    if x.std() > 0 and y.std() > 0:
        corr[f"{di},{dj}"] = round(float(np.corrcoef(x, y)[0, 1]), 3)
best = max(corr, key=corr.get)
out["georef"] = {"at_station": corr.get("0,0"), "best_shift_rows_cols": best, "best": corr[best],
                 "n_hours": len(georef[(0, 0)])}
json.dump(out, open(OUT, "w"), indent=1)
print(json.dumps({k: v for k, v in out.items() if k != "results"}, indent=1))
