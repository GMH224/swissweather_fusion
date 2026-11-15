# SwissWeather Fusion v0.4.0 — Release Audit (ICS)

**Release type:** model change (learning) plus fusion and storage fixes.
v0.4.0 was reserved for changes to learning; this is that release.
**Trial:** the Class A accuracy trial ran on v0.3.x until this install. Its data
(15 Sep – 15 Nov 2026) is preserved in a database copy; a final copy follows in
early December. From this release on, the paired comparison scores the
forecast as published, including the short-range correction (§6).
**Verification:** 1010 tests passing (956 carried, 4 of them adapted, 54 new),
package pyflakes clean, 40 targeted mutations all caught (§7). The built
learning was shown to reproduce the replay formula bit for bit on the real
database (§4). Schema version 7. No learning reset, no configuration change.
Minimum Home Assistant version 2026.10.0.

---

## 1. Scope

| Item | Decision | Evidence |
|---|---|---|
| C2 learning memory as a half-life in days | **Built, 3 days** (backtest said 10) | §3 |
| Season removed from the bucket key | **Built** | §3 |
| C3 error persistence, leads 1–6 h | **Built** | §3 |
| C4 dew point derived from T and RH | **Built** | §4 |
| C5 gusts fused by median | **Built** | backtest INFO-003 §4.3 |
| Newest-run blend query | **Built** | §4 |
| WAL size limit, verification-row duplicates, HACS minimum | **Built** | §4 |
| C1 clear-and-calm regime | **Deferred** to a February rerun on winter data | §3 |
| C7 calibrated rain probability | **Dropped**: already calibrated | §5 |
| Radar steering for Model B | **Not built**: fixed points score better | §5 |
| Type checker (mypy) | No defect found; one stale annotation corrected | §5 |

## 2. Live check of the backtest findings (31 Oct copy, 19,443 comparisons)

Blend vs best single corrected source (MAE): temperature better at 1–12 h by
0.10–0.16 °C, level at 24–48 h; pressure better at every lead; humidity: SRF
alone slightly better at 1–12 h (0.3–0.45 %RH, not significant). Bias
correction is nearly all the value: humidity raw error ~19 %RH, corrected ~4.
Live learning memories: 0.2–0.6 days for short leads, under 3 days elsewhere.
Clear-calm regime: corrected temperature +0.17 °C on calm hours, −0.1 °C
otherwise (19 % calm hours; difference not significant in autumn). Persistence
of the 1-hour error: strong at 1–3 h, gone by 6–12 h, harmful for humidity at
12 h. Script: `tools/v0_4_0_evidence/live_confirm.py`.

## 3. Replay of the learning on the real database (15 Nov copy)

`tools/v0_4_0_evidence/replay.py`: every stored forecast row of the five
sources, 2 Sep – 15 Nov, learned as live (so the live sample density is real,
not simulated); scored on the trial's own comparison slots from 22 Sep (≈1,300
per lead and measurement); paired day-block bootstrap, 2,000 resamples.

**Validation:** the replica of v0.3.6 rules reproduces the recorded live error:
temperature 0.704 vs 0.710 °C (1 h), 0.913 vs 0.903 (24 h); humidity 3.31 vs
3.53 (1 h); pressure 0.393 vs 0.405 (1 h).

**MAE, v0.3.6 rules → v0.4.0 (C2 3 days + C3, predictor one hour old):**

| Lead | Temperature °C | Humidity %RH | Pressure hPa |
|---|---|---|---|
| 1 h | 0.704 → **0.438** (−38 %) | 3.31 → **2.02** (−39 %) | 0.393 → **0.251** (−36 %) |
| 3 h | 0.721 → **0.570** (−21 %) | 3.26 → **2.74** (−16 %) | 0.400 → **0.307** (−23 %) |
| 6 h | 0.744 → **0.661** (−11 %) | 3.34 → 3.25 (−3 %) | 0.400 → **0.355** (−11 %) |
| 12 h | 0.788 → **0.715** (−9 %) | 3.45 → 3.38 (−2 %) | 0.413 → 0.425 (+3 %) |
| 24 h | 0.913 → **0.839** (−8 %) | 4.07 → 3.82 (−6 %) | 0.553 → 0.523 (−5 %) |
| 48 h | 1.011 → **0.912** (−10 %) | 3.99 → 3.76 (−6 %) | 0.689 → 0.667 (−3 %) |

Bold: 95 % interval excludes zero. No cell is significantly worse.
With the predictor as built (reading at the issue hour), 1 h is lower still
(temperature 0.310, humidity 1.42, pressure 0.209); this audit quotes the
cautious figures until live data confirm.

**Why 3 days, not 10:** with a 10-day half-life humidity got worse by 0.32–
0.37 %RH at 3–12 h (significant at 6 h) and pressure by 0.06–0.07 hPa at
3–12 h (significant); 3 days was best or level for all three. 1 day was between. The station's humidity and pressure
offsets drift over days, which archives scored at MeteoSwiss instruments
could not show.

**Why C1 is deferred:** on top of 3-day memory, the regime changed temperature
by less than ±0.02 °C at every lead and made humidity worse at 3–12 h
(+0.22 to +0.26 %RH). The
backtest's gain was in winter (DJF −11 %); autumn cannot test it.

**The design as built** (alpha per row from k rows per target hour; C3 with the
reading at the issue hour) gives the same C2 result as the averaged-density
variant (within 0.004 °C at every lead).

## 4. Built code on the real database

| Check | Result |
|---|---|
| Migration v6 → v7 on the 3.4 GB database | 0.009 s; 1,062 buckets relabelled SON → ALL, values unchanged |
| Blend v0.3.6 vs v0.4.0, frozen 15 Nov 17:05 | T/RH/P identical at lead 0 and at every lead > 6 h; shifted only at 1–6 h |
| Dew point | Derived at all 157 hours; never above temperature. Example: 8.4 °C / 62.8 % → 1.7 °C (fused was 4.7 °C, consistent with 81 %) |
| Gusts | Median; differs from the max in 42 of 157 hours |
| Fitted phi (temperature) | 0.86 (1 h), 0.61 (3 h), 0.27 (6 h) — matches the live check |
| Built learning vs replay formula | 20,298 rows of 14 Nov, 1,056 buckets: **difference 0**, sample counts equal, untouched buckets unchanged (`learn_equiv.py`) |
| Newest-run query vs old in-memory selection | Identical for every key the blend reads, at 1 Oct, 1 Nov, 15 Nov |
| Blend memory per cycle | 162 MB → 6 MB (rows 248,736 → 9,398) |
| Blend time | 2.25 s → 1.27 s |
| Learning cycle | 0.79 s → 0.44 s |

## 5. Items not built, with evidence

**C7 rain probability.** Published probability (mean of the five providers'
own probabilities) against the Tänikon gauge, 1,360 hours, 3 h lead: 0–10 %
forecast → 1 % observed, 30–50 % → 43 %, 70–100 % → 85 %; Brier skill +0.57
(24 h: +0.54). A bin calibration fitted on the first half made the second half
worse (skill +0.61 → +0.58). The backtest's miscalibration concerned a
probability derived from model agreement, which the integration does not use.

**Radar steering.** Collector archive 26 Sep – 15 Nov, 1,215 hours, 108 rain
hours at the Tänikon gauge. Heidke skill, Model B as built (local or three SW
points) 0.69 / 0.62 / 0.58 at 1 / 2 / 3 h; upwind wedge 0.64 / 0.48 / 0.42;
single upwind point 0.66 / 0.44 / 0.26. For rain not yet present, 1 h, the
single upwind point scored 0.54 vs 0.34 but on 27 events with an interval from
−0.06 to +0.45. 84 % of rain hours had west or south-west steering flow at
~55 km/h. Radar pixel at the station vs gauge: r = 0.997, best at zero offset
(grid read correctly). Repeat with summer convection.

**Type checker.** 40 messages, none a defect: narrowing mypy cannot follow,
Home Assistant stubs in the test environment, and stale annotations. The
return annotation of `validate_forecast_rows` was corrected.

## 6. What changes for users and consumers

- Temperature, humidity, pressure 1–6 h ahead: short-range correction (C3).
  Starts once 168 comparison pairs per lead exist (an existing installation
  has them; a new one after about a week).
- All leads: 3-day learning memory; effect builds up over the first days as
  buckets settle from their short-memory state.
- No restart of learning on 1 Dec / 1 Mar / 1 Jun / 1 Sep.
- Dew point derived; gusts by median.
- Unchanged: entities, attributes, units, timing; solar radiation, cloud
  layers, SRF irradiance, reference station, Model B.
- `blend_comparison.blend_value` is the published value; the new column
  `persistence_delta` holds the shift, so the learned blend remains
  recoverable. The trial verdict from 15 Sep – install day is unaffected.
- The blend's internal result carries a `persistence` key (error and phi per
  measurement) for diagnostics.

## 7. Tests and mutations

54 new tests (`tests/test_v0_4_0.py`). Adapted: two gust tests (median), the
window query test (newest only), the schema-version test.

| Area | Mutations | Caught |
|---|---|---|
| C2 alpha formula, k counting, override, validation | 4 | 4 |
| Season key in learning and blend | 2 | 2 |
| Migration: kept row, relabel, marker, column | 4 | 4 |
| Window query: order, tie, NULL, blend rows | 4 | 4 |
| WAL limit, checkpoint mode, checkpoint after purge, verification dedupe | 4 | 4 |
| C4 coefficient, clipping, applied, order vs C3 | 4 | 4 |
| C5 median | 1 | 1 |
| C3 fit clip, min pairs, lead limit, interpolation, learned vs published (fit and predictor), future readings, fallback hour, sign, cache, humidity clamp, comparison shift, stored delta, predictor hour, hourly, current, failure isolation | 17 | 17 |
| **Total** | **40** | **40** |

## 8. Risk and residuals

- **One season of evidence.** The replay covers late September to mid
  November at one site. Winter behaviour of the 3-day memory and of C3 is
  untested; the February rerun covers both, together with C1.
- **C3 depends on the station.** A faulty station reading shifts the next six
  hours. Bounded: phi ≤ 0.9 and fading with lead; humidity clamped to 0–100 %.
  The station's own plausibility checks (v0.2.1) still apply before storage.
- **First days after install:** the buckets start from their short-memory
  state and settle with the new memory; not a reset.
- **Home Assistant minimum 2026.10.0** in hacs.json: older installations are
  not offered the update.

## 9. Post-install check

1. Integration loads; the log shows no new warnings from SwissWeather Fusion.
2. Weather card: the dew point is below the temperature; gusts are lower on
   windy days than before.
3. After a day: the learning progress sensor stays at its level (no drop).
4. After a week: the blend comparison's 1-hour and 3-hour cells (temperature,
   humidity, pressure) should show clearly smaller errors than before install.

## 10. Upgrade

HACS update (or replace `custom_components/swissweather_fusion`), restart. No
configuration change, no learning reset.
