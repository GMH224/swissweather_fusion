"""Confirm backtest findings C1/C2/C3 on live trial data (read-only).

Usage: python3 live_confirm.py <swf.db> <out.json>
"""
import json, math, random, sqlite3, sys
from collections import defaultdict
from datetime import datetime, timedelta

db, outp = sys.argv[1], sys.argv[2]
c = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
c.row_factory = sqlite3.Row
P = lambda s: datetime.fromisoformat(s)
out = {}
random.seed(1)

def boot_mean(pairs, B=2000):
    """day-block bootstrap of mean(values); pairs = [(day, value)]"""
    by = defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    days = list(by)
    if len(days) < 5:
        return None
    est = sum(v for _, v in pairs) / len(pairs)
    res = []
    for _ in range(B):
        s = n = 0
        for _ in days:
            vs = by[random.choice(days)]
            s += sum(vs); n += len(vs)
        res.append(s / n)
    res.sort()
    return [round(est, 4), round(res[int(.025 * B)], 4), round(res[int(.975 * B)], 4)]

rows = [dict(r) for r in c.execute(
    "SELECT valid_at, measurement, lead_hours, issued_at, blend_value, contributors, actual_value "
    "FROM blend_comparison WHERE status='reconciled' AND actual_value IS NOT NULL")]
for r in rows:
    r["contrib"] = json.loads(r["contributors"])
    r["err"] = r["blend_value"] - r["actual_value"]
    r["day"] = r["valid_at"][:10]
print("rows", len(rows), rows[0]["valid_at"], rows[-1]["valid_at"])
out["period"] = [min(r["valid_at"] for r in rows), max(r["valid_at"] for r in rows), len(rows)]

# ---- A: blend vs single sources and equal weights -------------------------
A = {}
for meas in ("temperature", "humidity", "pressure"):
    for lead in (1, 3, 6, 12, 24, 48):
        sel = [r for r in rows if r["measurement"] == meas and r["lead_hours"] == lead]
        if not sel:
            continue
        res = {"n": len(sel), "blend": sum(abs(r["err"]) for r in sel) / len(sel)}
        srcs = set().union(*(r["contrib"].keys() for r in sel))
        for s in sorted(srcs):
            for kind in ("raw", "debiased"):
                v = [abs(r["contrib"][s][kind] - r["actual_value"]) for r in sel if s in r["contrib"]]
                if len(v) > 0.8 * len(sel):
                    res[f"{s}_{kind}"] = sum(v) / len(v)
        eq = [abs(sum(x["raw"] for x in r["contrib"].values()) / len(r["contrib"]) - r["actual_value"]) for r in sel]
        res["equal_raw"] = sum(eq) / len(eq)
        eqd = [abs(sum(x["debiased"] for x in r["contrib"].values()) / len(r["contrib"]) - r["actual_value"]) for r in sel]
        res["equal_debiased"] = sum(eqd) / len(eqd)
        best = min((k for k in res if k.endswith("_debiased") and not k.startswith("equal")), key=lambda k: res[k])
        res["best_single_debiased"] = best
        # paired: blend - best single debiased
        s = best[: -len("_debiased")]
        pairs = [(r["day"], abs(r["err"]) - abs(r["contrib"][s]["debiased"] - r["actual_value"])) for r in sel if s in r["contrib"]]
        res["blend_minus_best_ci"] = boot_mean(pairs)
        pairs = [(r["day"], abs(r["err"]) - abs(sum(x["raw"] for x in r["contrib"].values()) / len(r["contrib"]) - r["actual_value"])) for r in sel]
        res["blend_minus_equal_raw_ci"] = boot_mean(pairs)
        A[f"{meas}@{lead}"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items()}
out["A_skill"] = A

# ---- B: regime split (clear & calm: mean cloud < 30 %, wind < 2.5 m/s) -------
lo = (P(out["period"][0]) - timedelta(days=3)).isoformat()
hi = out["period"][1]
fc = defaultdict(dict)  # (valid_hour, var) -> source -> (issued, value)
for r in c.execute(
    "SELECT source, issued_at, valid_at, variable, value FROM forecast_snapshots "
    "WHERE variable IN ('cloud_coverage','wind_speed') AND value IS NOT NULL "
    "AND source IN ('ch1','ch2','icon_d2','srf','meteoblue') AND valid_at >= ? AND valid_at <= ?",
    (lo, hi)):
    va = r["valid_at"][:13]
    if r["issued_at"][:13] >= va:   # only forecasts issued before the hour
        continue
    d = fc[(va, r["variable"])]
    old = d.get(r["source"])
    if old is None or r["issued_at"] > old[0]:
        d[r["source"]] = (r["issued_at"], r["value"])
regime = {}
for (va, var), d in fc.items():
    regime.setdefault(va, {})[var] = sum(v for _, v in d.values()) / len(d)
calm = {va: (x.get("cloud_coverage", 100) < 30 and x.get("wind_speed", 99) < 2.5)
        for va, x in regime.items() if "cloud_coverage" in x and "wind_speed" in x}
out["B_regime_share"] = sum(calm.values()) / max(1, len(calm))
B = {}
for meas in ("temperature", "humidity", "pressure"):
    for lead in (3, 24, 48):
        sel = [r for r in rows if r["measurement"] == meas and r["lead_hours"] == lead and r["valid_at"][:13] in calm]
        res = {}
        for name, flag in (("calm", True), ("other", False)):
            s = [r for r in sel if calm[r["valid_at"][:13]] == flag]
            if not s:
                continue
            res[name] = {"n": len(s),
                         "bias": round(sum(r["err"] for r in s) / len(s), 3),
                         "mae": round(sum(abs(r["err"]) for r in s) / len(s), 3),
                         "bias_ci": boot_mean([(r["day"], r["err"]) for r in s])}
            # night only (18-06 UTC), where inversions live
            sn = [r for r in s if int(r["valid_at"][11:13]) >= 17 or int(r["valid_at"][11:13]) <= 5]
            if sn:
                res[name]["night_bias"] = round(sum(r["err"] for r in sn) / len(sn), 3)
                res[name]["night_n"] = len(sn)
        B[f"{meas}@{lead}"] = res
out["B_regime"] = B

# ---- C: error persistence ------------------------------------------------------
# latest known error at issue time t0: the lead-1 comparison valid at floor(t0)
e1 = {(r["measurement"], r["valid_at"][:13]): r["err"] for r in rows if r["lead_hours"] == 1}
C = {}
for meas in ("temperature", "humidity", "pressure"):
    for lead in (1, 3, 6, 12, 24, 48):
        pts = []
        for r in rows:
            if r["measurement"] != meas or r["lead_hours"] != lead:
                continue
            k = (meas, r["issued_at"][:13])
            if k in e1:
                pts.append((r["day"], r["valid_at"], e1[k], r["err"]))
        if len(pts) < 100:
            continue
        pts.sort(key=lambda p: p[1])
        half = len(pts) // 2
        tr, te = pts[:half], pts[half:]
        sxy = sum(p[2] * p[3] for p in tr); sxx = sum(p[2] ** 2 for p in tr)
        phi = max(0.0, min(0.9, sxy / sxx if sxx else 0))
        corr_n = sum(p[2] * p[3] for p in pts)
        corr = corr_n / math.sqrt(sum(p[2] ** 2 for p in pts) * sum(p[3] ** 2 for p in pts))
        diff = [(p[0], abs(p[3] - phi * p[2]) - abs(p[3])) for p in te]
        C[f"{meas}@{lead}"] = {"n": len(pts), "corr": round(corr, 3), "phi_train": round(phi, 3),
                               "mae_test": round(sum(abs(p[3]) for p in te) / len(te), 4),
                               "mae_test_corrected": round(sum(abs(p[3] - phi * p[2]) for p in te) / len(te), 4),
                               "delta_ci": boot_mean(diff)}
out["C_persistence"] = C

# ---- D: live learning memory ----------------------------------------------------
alpha = {"short": 0.15, "medium": 0.08, "long": 0.04}
D = {}
first = c.execute("SELECT min(valid_at) FROM forecast_snapshots WHERE reconciliation_status='reconciled' AND valid_at >= '2026-09-01'").fetchone()[0]
last = c.execute("SELECT max(last_updated) FROM bucket_stats").fetchone()[0]
days = (P(last) - P(first)).total_seconds() / 86400
for r in c.execute("SELECT lead_time_bucket b, source, measurement, avg(sample_count) n FROM bucket_stats GROUP BY 1,2,3"):
    spd = r["n"] / days
    hl_samples = math.log(0.5) / math.log(1 - alpha[r["b"]])
    D[f"{r['measurement']}|{r['source']}|{r['b']}"] = {"samples_per_day": round(spd, 2),
                                                      "half_life_days": round(hl_samples / spd, 2) if spd else None}
out["D_memory"] = {"days_in_season": round(days, 1), "first": first, "buckets": D}
json.dump(out, open(outp, "w"), indent=1)
print(json.dumps({k: out[k] for k in ("period", "B_regime_share")}, indent=1))
