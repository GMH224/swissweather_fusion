"""Replay of Model A on the live database: today's rules vs the v0.4.0 package.

Python port of swf_simulate.m + swf_ar.m (backtest toolkit v6), fed with the
integration's own stored forecasts and station observations instead of
archives. Usage:

    python3 -I replay.py <swf.db> <out.json>

Every stored forecast row is a learning sample, exactly as live (so the
live sample density is reproduced, not simulated). A row is learned once
its valid time has passed; a trial case (blend_comparison slot) is blended
from each source's newest row issued at or before the case's issue time,
using only outcomes known before that time. Scoring uses the trial's own
cases and observed values.

Variants:
  replica     v0.3.6 rules (validation: must reproduce the recorded blend)
  c1          + clear-and-calm regime in the key, shrink/discount/nalpha
  c2 / c2_3d / c2_1d   half-life 10 / 3 / 1 days (alpha from the average
              samples per day of each source and lead bucket)
  c2k         3 days, alpha per row from k rows per target hour — as built
  c1c2_3d     C1 with a 3-day half-life (rerun this on winter data)
  *_c3        + error persistence, predictor one hour older (backtest style)
  c2k_c3live  + error persistence as built (predictor at the issue hour)
Run on a copy of the database; it is opened read-only (immutable).
"""
import bisect, json, random, sqlite3, sys
from collections import defaultdict
from datetime import datetime

DB, OUT = sys.argv[1], sys.argv[2]
SOURCES = ("ch1", "ch2", "icon_d2", "srf", "meteoblue")
MEAS = ("temperature", "humidity", "pressure")
ALPHA = {0: 0.15, 1: 0.08, 2: 0.04}
EPS, MIN_TRUST, RATIO = 0.01, 5, 8.0
SHRINK_K = DISCOUNT_K = 10
TOL_S = 30 * 60          # models/model_a.py RECONCILIATION_TOLERANCE_MINUTES = 30
HALF_LIFE_DAYS = 10.0
C3_MAX_LEAD = 6
AR_WINDOW_DAYS = 30
random.seed(3)

c = sqlite3.connect(f"file:{DB}?immutable=1", uri=True)
P = lambda s: datetime.fromisoformat(s).timestamp()

# ---- observations -----------------------------------------------------------
obs = {m: [] for m in MEAS}
for ts, t, h, p in c.execute("SELECT ts, temperature, humidity, pressure FROM station_observations ORDER BY ts"):
    tt = P(ts)
    for m, v in zip(MEAS, (t, h, p)):
        if v is not None:
            obs[m].append((tt, v))
obs_t = {m: [x[0] for x in obs[m]] for m in MEAS}


def actual(m, t):
    lst, ts = obs[m], obs_t[m]
    i = bisect.bisect_left(ts, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(ts) and abs(ts[j] - t) <= TOL_S:
            if best is None or abs(ts[j] - t) < abs(ts[best] - t):
                best = j
    return lst[best][1] if best is not None else None


# ---- regime inputs: cloud cover and wind per source, valid hour -------------
aux = defaultdict(list)  # (var, src, valid_hour) -> sorted [(issued, value)]
for src, iss, va, var, val in c.execute(
        "SELECT source, issued_at, valid_at, variable, value FROM forecast_snapshots "
        "WHERE variable IN ('cloud_coverage','wind_speed') AND value IS NOT NULL "
        "AND source IN ('ch1','ch2','icon_d2','srf','meteoblue') AND valid_at >= '2026-09-01'"):
    aux[(var, src, int(P(va)) // 3600)].append((P(iss), val))
for k in aux:
    aux[k].sort()
reg_cache = {}


def regime(valid_hour, issued):
    """2 = clear and calm (mean cloud < 30 %, mean wind < 2.5 m/s) from the
    forecasts available at `issued`; 1 otherwise (also when unknown)."""
    key = (valid_hour, int(issued) // 600)
    if key in reg_cache:
        return reg_cache[key]
    means = []
    for var in ("cloud_coverage", "wind_speed"):
        vals = []
        for s in SOURCES:
            lst = aux.get((var, s, valid_hour))
            if not lst:
                continue
            i = bisect.bisect_right(lst, (issued, float("inf"))) - 1
            if i >= 0:
                vals.append(lst[i][1])
        means.append(sum(vals) / len(vals) if vals else None)
    r = 2 if (means[0] is not None and means[1] is not None and means[0] < 30 and means[1] < 2.5) else 1
    reg_cache[key] = r
    return r


def leadb(h):
    return 0 if h < 24 else (1 if h < 72 else 2)


out = {"variants": {}, "validation": {}, "spd": {}}
for m in MEAS:
    # ---- rows ---------------------------------------------------------------
    rows = []   # valid, issued, lead_h, src, f, y, regime
    for src, iss, run0, va, val in c.execute(
            "SELECT source, issued_at, run_initialised_at, valid_at, value FROM forecast_snapshots "
            "WHERE variable = ? AND value IS NOT NULL AND source IN "
            "('ch1','ch2','icon_d2','srf','meteoblue') AND valid_at >= '2026-09-02' ORDER BY id", (m,)):
        tv, ti = P(va), P(iss)
        if ti >= tv:
            continue
        base = P(run0) if run0 else ti
        lead = (tv - base) / 3600
        rows.append((tv, ti, lead, SOURCES.index(src), val, actual(m, tv),
                     regime(int(tv) // 3600, ti)))
    # samples per day per (source, lead bucket) -> alpha for a 10-day half-life
    span = (max(r[0] for r in rows) - min(r[0] for r in rows)) / 86400
    cnt = defaultdict(int)
    for r in rows:
        if r[5] is not None:
            cnt[(r[3], leadb(r[2]), int(r[0]) // 3600 % 24)] += 1
    spd = {}
    for s in range(len(SOURCES)):
        for b in range(3):
            per_hour = [cnt[(s, b, h)] for h in range(24) if cnt[(s, b, h)]]
            spd[(s, b)] = (sum(per_hour) / len(per_hour) / span) if per_hour else 1.0
    out["spd"][m] = {f"{SOURCES[s]}|{b}": round(v, 2) for (s, b), v in spd.items()}
    alpha_hl = {d: {k: 1 - 0.5 ** (1 / (d * v)) for k, v in spd.items()} for d in (1.0, 3.0, 10.0)}

    kcount = defaultdict(int)
    for r in rows:
        if r[5] is not None:
            kcount[(r[3], leadb(r[2]), int(r[0]))] += 1
    # index: (src, valid_hour) -> sorted [(issued, row index)]
    idx = defaultdict(list)
    for i, r in enumerate(rows):
        idx[(r[3], int(r[0]))].append((r[1], i))
    for k in idx:
        idx[k].sort()

    # ---- trial cases ----------------------------------------------------------
    cases = []
    for va, lead, iss, blend, y in c.execute(
            "SELECT valid_at, lead_hours, issued_at, blend_value, actual_value FROM blend_comparison "
            "WHERE measurement = ? AND status = 'reconciled' AND actual_value IS NOT NULL", (m,)):
        cases.append((P(iss), P(va), lead, blend, y))
    cases.sort()

    def simulate(variant):
        use_reg = variant.startswith("c1")
        refined = use_reg
        perk = variant.startswith("c2k")
        hl = {"c2": 10.0, "c1c2": 10.0, "c2_3d": 3.0, "c2_1d": 1.0, "c1c2_3d": 3.0}.get(variant)
        mem10 = hl is not None
        alpha10 = alpha_hl[hl] if hl else None
        bias, abse, n = defaultdict(float), defaultdict(float), defaultdict(int)
        pbias, pabse, pn = defaultdict(float), defaultdict(float), defaultdict(int)
        upd = sorted((r[0], i) for i, r in enumerate(rows) if r[5] is not None)
        u = 0
        res = []
        for ti, tv, lead, rec_blend, y in cases:
            while u < len(upd) and upd[u][0] < ti:
                r = rows[upd[u][1]]
                lb = leadb(r[2])
                rg = r[6] if use_reg else 1
                k = (int(r[0]) // 3600 % 24, lb, r[3], rg)
                a = alpha10[(r[3], lb)] if mem10 else ALPHA[lb]
                if perk:
                    a = 1 - 0.5 ** (1 / (3.0 * kcount[(r[3], lb, int(r[0]))]))
                if refined:
                    a = max(a, 1 / (n[k] + 1))
                err = r[4] - r[5]
                before = abs((r[4] - bias[k]) - r[5])
                if n[k] == 0:
                    abse[k], bias[k] = before, err
                else:
                    abse[k] = a * before + (1 - a) * abse[k]
                    bias[k] = a * err + (1 - a) * bias[k]
                n[k] += 1
                if refined:
                    pk = (lb, r[3], rg)
                    ap = alpha10[(r[3], lb)] if mem10 else ALPHA[lb]
                    ap = max(ap, 1 / (pn[pk] + 1))
                    pb = abs((r[4] - pbias[pk]) - r[5])
                    if pn[pk] == 0:
                        pabse[pk], pbias[pk] = pb, err
                    else:
                        pabse[pk] = ap * pb + (1 - ap) * pabse[pk]
                        pbias[pk] = ap * err + (1 - ap) * pbias[pk]
                    pn[pk] += 1
                u += 1
            contrib = []
            for s in range(len(SOURCES)):
                lst = idx.get((s, int(tv)))
                if not lst:
                    continue
                j = bisect.bisect_right(lst, (ti, float("inf"))) - 1
                if j < 0:
                    continue
                r = rows[lst[j][1]]
                lb = leadb(r[2])
                rg = (regime(int(tv) // 3600, ti) if use_reg else 1)
                k = (int(tv) // 3600 % 24, lb, s, rg)
                b, e, nn = bias[k], abse[k], n[k]
                if refined:
                    pk = (lb, s, rg)
                    if pn[pk] > 0:
                        lam = nn / (nn + SHRINK_K)
                        b = lam * b + (1 - lam) * pbias[pk]
                        e = lam * e + (1 - lam) * pabse[pk]
                        nn = max(nn, pn[pk])
                w = 1 / (e + EPS) if nn > 0 else 1.0
                contrib.append((r[4], b, w, nn, n[k]))
            if not contrib:
                continue
            tw = [cc[2] for cc in contrib if cc[3] >= MIN_TRUST]
            ref = sorted(tw)[len(tw) // 2] if len(tw) % 2 else (
                (sorted(tw)[len(tw) // 2 - 1] + sorted(tw)[len(tw) // 2]) / 2 if tw else 1.0)
            num = den = 0.0
            for f, b, w, nn, ncell in contrib:
                if nn >= MIN_TRUST:
                    d = f - b
                    ww = min(max(w, ref / RATIO), ref * RATIO)
                    if refined:
                        ww = ww * ncell / (ncell + DISCOUNT_K)
                        if ww <= 0:
                            ww = ref / RATIO
                else:
                    d, ww = f, ref
                num += d * ww
                den += ww
            res.append((ti, tv, lead, num / den, y, rec_blend))
        return res

    def ar(res, max_lead):
        """swf_ar.m: blend + phi_L * latest known error, phi per lead and day."""
        byl = defaultdict(list)
        for r in res:
            byl[r[2]].append(r)
        outl = []
        for L, lst in byl.items():
            lst.sort(key=lambda r: r[1])
            err = {int(r[1]): r[4] - r[3] for r in lst}
            for r in lst:
                val = r[3]
                if L <= max_lead:
                    issue = r[1] - L * 3600
                    last = err.get(int(issue - 3600))
                    day0 = (int(issue) // 86400) * 86400
                    tr = [(err[int(q[1])], err.get(int(q[1] - L * 3600 - 3600)))
                          for q in lst if day0 - AR_WINDOW_DAYS * 86400 <= q[1] < day0]
                    tr = [(e, l) for e, l in tr if l is not None]
                    if last is not None and len(tr) >= 7 * 24:
                        sxy = sum(e * l for e, l in tr); sxx = sum(l * l for e, l in tr)
                        phi = min(max(sxy / sxx if sxx else 0.0, 0.0), 0.9)
                        val = r[3] + phi * last
                outl.append((r[0], r[1], r[2], val, r[4], r[5]))
        return outl

    sims = {v: simulate(v) for v in ("replica", "c1", "c2_3d", "c2k", "c1c2_3d")}

    def ar_live(res, max_lead):
        """C3 as built: predictor = latest lead-1 error (valid = issue hour,
        else one hour earlier); phi per lead over the previous 30 days."""
        e1 = {int(r[1]): r[4] - r[3] for r in res if r[2] == 1}
        def pred(issue):
            h = (int(issue) // 3600) * 3600
            return e1.get(h, e1.get(h - 3600))
        byl = defaultdict(list)
        for r in res:
            byl[r[2]].append(r)
        outl = []
        for L, lst in byl.items():
            lst.sort(key=lambda r: r[1])
            hist = [(r[1], r[4] - r[3], pred(r[0])) for r in lst]
            for r in lst:
                val = r[3]
                if L <= max_lead:
                    p = pred(r[0])
                    day0 = (int(r[0]) // 86400) * 86400
                    tr = [(e, q) for t, e, q in hist if day0 - AR_WINDOW_DAYS * 86400 <= t < day0 and q is not None]
                    if p is not None and len(tr) >= 7 * 24:
                        sxy = sum(e * q for e, q in tr); sxx = sum(q * q for e, q in tr)
                        phi = min(max(sxy / sxx if sxx else 0.0, 0.0), 0.9)
                        val = r[3] + phi * p
                outl.append((r[0], r[1], r[2], val, r[4], r[5]))
        return outl
    for v in ("replica", "c2_3d", "c2k", "c1c2_3d"):
        sims[v + "_c3"] = ar(sims[v], C3_MAX_LEAD)
    sims["c2k_c3live"] = ar_live(sims["c2k"], C3_MAX_LEAD)
    rec = [(r[0], r[1], r[2], r[5], r[4], r[5]) for r in sims["replica"]]
    sims["recorded_live"] = rec

    # validation: replica vs recorded blend, case by case
    d = [abs(r[3] - r[5]) for r in sims["replica"]]
    out["validation"][m] = {"cases": len(d), "mean_abs_diff": round(sum(d) / len(d), 4),
                            "p95_abs_diff": round(sorted(d)[int(.95 * len(d))], 4)}

    def score(res, lead, start=None):
        sel = [r for r in res if r[2] == lead and (start is None or r[1] >= start)]
        return sel

    def boot(a, b, B=2000):
        pa = {(r[1], r[2]): abs(r[3] - r[4]) for r in a}
        pairs = [(int(r[1]) // 86400, pa[(r[1], r[2])] - abs(r[3] - r[4]))
                 for r in b if (r[1], r[2]) in pa]
        by = defaultdict(list)
        for dd, v in pairs:
            by[dd].append(v)
        days = list(by)
        est = sum(v for _, v in pairs) / len(pairs)
        bs = []
        for _ in range(B):
            s = nn = 0
            for _ in days:
                vs = by[random.choice(days)]
                s += sum(vs); nn += len(vs)
            bs.append(s / nn)
        bs.sort()
        return [round(est, 4), round(bs[int(.025 * B)], 4), round(bs[int(.975 * B)], 4)]

    start = P("2026-09-22T00:00:00+00:00")   # 20 days of learning before scoring
    for lead in (1, 3, 6, 12, 24, 48):
        row = {}
        for v, res in sims.items():
            sel = score(res, lead, start)
            row[v] = {"n": len(sel), "mae": round(sum(abs(r[3] - r[4]) for r in sel) / len(sel), 4)}
        for v in ("c1", "c2_3d", "c2k", "c1c2_3d", "c2_3d_c3", "c2k_c3", "c1c2_3d_c3", "c2k_c3live"):
            row[v]["minus_replica"] = boot(score(sims[v], lead, start), score(sims["replica"], lead, start))
        out["variants"][f"{m}@{lead}"] = row
    print(m, out["validation"][m], flush=True)

json.dump(out, open(OUT, "w"), indent=1)
