# Architecture Review — Model A Expansion & Weather Card

**Document under review:**
`SwissWeather_Fusion_Model_A_Expansion_and_Weather_Card_Architecture.md`

**Reviewed against:** SwissWeather Fusion v0.1.28 source
**Date:** 2 September 2026
**Reviewer verdict:** **APPROVE WITH CONDITIONS** — the architecture is
sound and materially better reasoned than the three external audits that
preceded it. Six findings must be resolved before Stage 1 implementation
begins; two of them invalidate specific design choices in the document as
written.

No code was changed in the course of this review.

---

## 1. Summary of findings

| ID | Severity | Finding | Effect on the document |
| --- | --- | --- | --- |
| **AR-01** | **Blocking** | The selected card cannot consume custom forecast fields. §19/§20's preferred option does not exist. | §19 option 1 and 3 must be struck; option 2 becomes the only path |
| **AR-02** | **Blocking** | Storage volume grows ~4×, uncosted. Reporting installation has retention disabled. | §14 needs a volume section and a retention precondition |
| **AR-03** | **High** | Class B's "arithmetic blend" default is meteorologically wrong for precipitation, snowfall and gusts | §6.2 and §13.1 need per-parameter strategies |
| **AR-04** | **Medium** | §19 overstates the HA `Forecast` gap. Most target fields are already standard. | Scope reduction — Stage 2 is cheaper than assessed |
| **AR-05** | **Medium** | Meteonomiqs promotion is viable, but blocked on IND-12 and on an unmeasured forecast horizon | §2.5's exclusion can be lifted, with conditions |
| **AR-06** | **Low** | `visibility` is not a HA forecast field; §5.2/§6.2 assume it is | Minor correction |

**Correction to a claim I made verbally before this review.** I stated
that a once-daily source could only populate a "diagonal slice" of the
`(hour_of_day × lead_time)` bucket grid. That is wrong when the forecast
horizon is long, because each hour of day is reached at several different
lead times across successive forecast days. The corrected analysis is in
AR-05. The conclusion survives, the reasoning does not, and the
difference matters because it changes what has to be measured.

---

## 2. What the document gets right

These are not courtesies; each closes a real defect in the current build.

**2.1 The Class A/B/C/D taxonomy (§6).** The governing rule — *parameters
without local ground truth must not receive fabricated learned bias
corrections* — is correct and is the single most important sentence in
the document. It generalises a principle the codebase already applies
piecemeal: `unit_conversion.py` rejects unrecognised units rather than
guessing, `_validated_risk_value` rejects out-of-scale input rather than
clamping. Extending "refuse to invent" to the fusion layer is the right
move.

Without this rule, the obvious implementation of a wider Model A would
have run precipitation and wind through the same EMA machinery as
temperature, producing `ema_bias` values for quantities with nothing to
reconcile against. Those numbers would have looked exactly like the
learned temperature bias and been silently wrong.

**2.2 "Do not infer a value when an upstream model provides it" (§1).**
This retires two acknowledged hacks. `derive_condition()` currently
infers `snowy` from `temperature ≤ 0 and precip > threshold`, and
`cloudy` from a humidity proxy that DEVELOPER.md openly labels
"plausible but unvalidated". Open-Meteo already returns `snowfall` and
`weather_code`; SRF already parses fresh snow and symbol codes. The
project is guessing at answers it is being handed and then discarding.

**2.3 Reuse of the narrow `forecast_snapshots` schema (§14.1).** Correct.
The table is already effectively entity-attribute-value, so new
parameters are new rows, requiring no migration. This is the single
biggest reason Stage 1 is tractable at all.

**2.4 "Categorical parameters are never averaged" (§6.3).** Correct and
worth stating explicitly, because the arithmetic is superficially
available and produces plausible-looking nonsense.

**2.5 Four-way precipitation split (§7.1).** Preserving total / rain /
showers / snowfall as distinct quantities rather than collapsing them is
right, and is a precondition for AR-03's fix.

**2.6 Parameter metadata registry (§17).** A registry rather than
branching logic is the correct structure for ~20 parameters with
differing fusion rules, and it makes the per-parameter strategies AR-03
requires cheap to express.

---

## 3. AR-01 (Blocking) — the card cannot consume custom forecast fields

**§19 proposes three options, in stated preference order:**

1. expose through the custom card if it can consume arbitrary forecast
   fields;
2. expose as dedicated sensor entities;
3. extend the card to support explicitly declared custom metrics.

**Option 1 does not exist, and option 3 means forking the card.**

`troinine/ha-weather-forecast-card` documents a fixed set of chartable
attributes — `apparent_temperature`, `humidity`, `pressure`, `uv_index` —
and states that attributes appear only if provided by the weather entity,
selectable only in chart mode. It offers no mechanism for declaring an
arbitrary forecast key.

It does, however, document exactly the escape hatch the project needs:
each displayed attribute's value can be **overridden with a custom sensor
entity**, explicitly for the case where the weather integration does not
provide it.

**Consequence for the document.** §19's preference ordering must be
inverted. Sensor entities are not the fallback; they are the mechanism.
Any Stage 2 design predicated on the card rendering `snowfall` or
`freezing_level_height` straight out of a `Forecast` dict will not work.

**Recommendation.** Strike options 1 and 3. Rewrite §19 around dedicated
sensor entities for the non-standard parameters, and note that this
aligns with an existing gap: the blended current values
(temperature, humidity, pressure, precip, wind) are presently reachable
**only** as weather-entity attributes and are exposed as no sensor at
all. Creating them serves Stage 2 *and* gives every blended value
long-term statistics, which the v0.1.24 IND-08 work established as a
requirement.

**Verification owed before Stage 2 design is finalised.** Install the
card against the current entity and confirm which attributes it renders
in practice. One afternoon; it de-risks the entire stage.

---

## 4. AR-02 (Blocking) — storage volume is uncosted

§14.1 concludes that no wide schema is required. Correct, and it is the
right conclusion. But "no schema change" is not "no cost", and the
document nowhere states the row volume it implies.

Measured against the code (`FORECAST_HOURS_AHEAD = 168`,
`ALL_FORECAST_SOURCES`, current poll intervals):

| | Rows per run per model | Worst-case rows/day | Rows at 90-day retention |
| --- | --- | --- | --- |
| Today (5 variables) | 840 | ~46,200 | ~4.2 M |
| Proposed (~20 variables) | 3,360 | ~184,800 | ~16.6 M |

A **~4× increase**, to roughly 16.6 million rows at the default retention
window. That is viable on a 90-day window and ordinary hardware. It is
not viable in two situations that both currently apply:

1. **The reporting installation has `purge_days = 0`** — retention
   disabled, "keep forever". The 90-day default introduced in v0.1.24
   applies to new installs only; this entry predates it and was
   deliberately not migrated. At 185k rows/day, unbounded, on SD-class
   storage.
2. **`purge_older_than()` never issues `VACUUM`**, so the file does not
   shrink after a purge. Growth is effectively one-way within a
   deployment's life.

Compounding: nothing surfaces database size to the user. `get_storage_stats()`
was added in v0.1.24 and reports row counts and file size, but no entity
exposes it.

**Recommendation.** Make Stage 1 conditional on three preconditions:

- retention set to a bounded value on any installation receiving the
  expansion, with an explicit upgrade note;
- a database-size sensor, using the existing `get_storage_stats()`;
- a stated per-parameter retention policy. Not every new variable
  deserves 90 days — `weather_code` and `snow_depth` are cheap to keep,
  a 168-hour `uv_index` series is not obviously worth 90 days of disk.

**Recommendation (design).** Consider whether all ~20 parameters need
storing at the full 168-hour horizon. Class B parameters exist to be
displayed, not reconciled; they arguably need only the horizon the card
renders. That single change could halve the increase.

---

## 5. AR-03 (High) — arithmetic blending is wrong for the Class B parameters that matter

§6.2 sets one default strategy for all of Class B:

```
available source values -> unit-normalized -> validity filter
                        -> availability-aware arithmetic blend
```

For continuous, approximately-Gaussian quantities — dew point, apparent
temperature, cloud coverage — that is fine.

For the three parameters users will actually look at, it is wrong.

**Precipitation is not Gaussian.** Its distribution is zero-inflated and
heavy-tailed. Averaging a model predicting 0 mm with one predicting 10 mm
yields 5 mm: a value neither model forecast, describing weather neither
expects. Across many hours this systematically *under-forecasts peaks*
and *over-forecasts drizzle* — the ensemble-mean smoothing problem, and
it is well known in operational meteorology.

**Snowfall is close to binary at the margin.** It snows or it does not.
An average across disagreeing models produces a small non-zero snowfall
that misrepresents both.

**Wind gusts are an extreme statistic.** A gust forecast is a maximum.
The mean of several maxima is not a maximum and understates the hazard —
the one direction in which being wrong matters most.

**Recommendation.** Replace the single default with per-parameter
strategies declared in the §17 registry. Suggested starting points, all
to be validated rather than trusted:

| Parameter | Strategy | Rationale |
| --- | --- | --- |
| `precipitation`, `rain`, `showers` | median, or mean of non-zero contributors with a separate agreement count | resists the zero-inflation problem |
| `snowfall` | median; publish `None` where models disagree on occurrence | avoids inventing marginal snow |
| `wind_gust_speed` | maximum or high percentile | a gust forecast is an extreme |
| `precip_probability` | mean | genuinely a probability; averaging is defensible |
| `dew_point`, `apparent_temperature`, `cloud_coverage` | mean | continuous and well-behaved |

**Recommendation (transparency).** Where sources disagree materially,
expose the disagreement rather than hiding it in a mean. A
"number of contributing models" and a spread figure belong in Class D
(§6.4) and are more honest than a confidently-blended single value.

---

## 6. AR-04 (Medium) — the HA `Forecast` gap is smaller than assessed

§19 asserts that HA's `Forecast` contract "does not provide dedicated
standard fields for every provider-specific metric", listing seven
parameters as problematic. Verified against the current `Forecast`
TypedDict in HA core, most of the document's target set is **already
standard**:

**Already standard** — `condition`, `datetime`, `humidity`,
`precipitation_probability`, `cloud_coverage`, `native_precipitation`,
`native_pressure`, `native_temperature`, `native_templow`,
`native_apparent_temperature`, `wind_bearing`, `native_wind_gust_speed`,
`native_wind_speed`, `native_dew_point`, `uv_index`, `is_daytime`.

**Genuinely non-standard** — `snowfall`, `rain`, `showers`, `snow_depth`,
`snowfall_height`, `freezing_level_height`, `sunshine_duration`,
`weather_code`, model confidence.

So the gap is nine parameters, not the whole advanced set. Combined with
AR-01, these nine are the ones requiring sensor entities; everything else
flows through the standard contract into any card, including HA's
built-in one.

This is a **scope reduction** and should be reflected in the Stage 2
estimate.

---

## 7. AR-05 (Medium) — Meteonomiqs promotion: viable, with two preconditions

§2.5 states Meteonomiqs "must not be silently added to Model A merely
because it has useful parameters", citing differing source role, quota,
schedule and calibration strategy. The caution is right. The conclusion
should nonetheless be revisited, because the strongest objection —
that a once-daily source cannot be learned — does not hold.

**7.1 Data age is already a first-class concept.** `bucket_stats` is
keyed by `lead_time_bucket` (short < 24 h, medium 24–72 h, long > 72 h)
with per-bucket EMA responsiveness (`EMA_ALPHA_BY_LEAD_TIME`:
0.15 / 0.08 / 0.04). A forecast issued at noon produces genuine
short-lead samples for that afternoon, learned in the short bucket
alongside every other source. No new mechanism is required.

**7.2 Sample volume is adequate.** One run covering N hours yields N
reconcilable pairs per day. Against `MIN_SAMPLES_TO_TRUST_BUCKET = 5`,
even a 24-hour horizon crosses the trust threshold in days, not months.

**7.3 Bucket coverage depends entirely on the forecast horizon, which is
unmeasured.** This corrects my earlier verbal claim. Coverage of the 72
`(hour_of_day × lead_time)` combinations for a source issued at ~12:00:

| Horizon | Combinations reachable |
| --- | --- |
| 24 h | 24 / 72 — short-lead only; medium and long unreachable |
| 48 h | 47 / 72 |
| 72 h | 48 / 72 |
| 120 h | 71 / 72 |
| 168 h | 71 / 72 |

A short horizon does not disqualify the source — `blend()` handles absent
contributors cleanly, and IND-01's median-relative weighting means an
absent source does not distort the others. But it does determine *where*
Meteonomiqs can contribute, and the project does not currently know the
horizon. `parse_hourly_forecast` accepts whatever arrives.

**7.4 The usable intersection is narrow.** The hourly endpoint returns
mean-sea-level pressure, precipitation sum and precipitation probability.
Model A reconciles temperature, humidity and pressure. The intersection
is **pressure alone** — the measurement on which all models already
agree most closely, and therefore the one where a new expert adds least.

**Recommendation — conditional approval:**

- `meteonomiqs_pressure` → **Class A**, subject to the two preconditions
  below. Low cost, low expected benefit, methodologically sound.
- `meteonomiqs_precip_probability` → **Class B**. Genuinely valuable as
  an independent probabilistic signal from a radar-nowcasting
  specialist, and unlearnable without a rain gauge.
- `meteonomiqs_precip_sum` → **Class B**, subject to AR-03's non-mean
  strategy.

**Precondition 1 — resolve IND-12 first.** Open-Meteo requests
`pressure_msl` and meteoblue maps `sealevelpressure`; SRF's
`PRESSURE_HPA` reference is undocumented. Adding a fourth pressure source
while one existing source may be on a different datum would corrupt the
blend for the exact measurement being added. This must be settled first.

**Precondition 2 — measure the forecast horizon** from a real response
before deciding which lead-time buckets Meteonomiqs can serve.

**Note on implementation.** Promotion means renaming
`meteonomiqs_pressure` → `pressure` in `forecast_snapshots`, which is a
data migration, and adding the source to `ALL_FORECAST_SOURCES`, which
changes blended pressure output for existing installations. Neither is
difficult; both need to be deliberate.

**Note on IND-10.** Whatever is decided, the currently-stored
`meteonomiqs_*` rows have **no reader** — the prefix constant appears
only at the write site. The status quo is not "safely excluded"; it is
"written and never used". Either promote the data or stop storing it.

---

## 8. AR-06 (Low) — `visibility` is not a forecast field

§5.2 lists visibility under "Visibility / cloud" and §6.2 includes it in
Class B. `visibility` is a **current-condition** attribute on
`WeatherEntity`; it is not a member of the `Forecast` TypedDict. A
forecast visibility value therefore cannot reach a card through the
standard contract and falls under AR-01's sensor-entity treatment.

---

## 9. Recommended sequencing

The document's two-stage split is right. This adds a Stage 0 of
preconditions, each of which invalidates downstream work if deferred.

**Stage 0 — preconditions (no new parameters).**
1. Set bounded retention; add the database-size sensor (AR-02).
2. Install the card against the current entity; record which attributes
   actually render (AR-01).
3. Resolve IND-12 — SRF's pressure datum (AR-05).
4. Capture one real Meteonomiqs hourly response; record the horizon
   (AR-05).

**Stage 1a — promote what is already parsed and discarded.** Open-Meteo's
`snowfall`, `weather_code`, `precipitation_probability`; SRF's fresh
snow, symbol code, dew point, gusts. Highest value per unit of work: no
new API calls, no new quota, and it retires the inferred-`snowy` and
inferred-`cloudy` hacks.

**Stage 1b — parameter registry and per-parameter fusion strategies**
(§17, AR-03). Must land before more Class B parameters, or the arithmetic
default becomes entrenched.

**Stage 1c — Meteonomiqs promotion** (AR-05), gated on Stage 0 items 3
and 4.

**Stage 1d — meteoblue's additional fields**, gated on the fixture
verification §2.6 already requires.

**Stage 2 — presentation.** Sensor entities for the nine non-standard
parameters, then card configuration.

---

## 10. Conditions for approval

Stage 1 implementation should not begin until:

1. **AR-01** is resolved — §19 rewritten around sensor entities, with the
   card's actual behaviour verified empirically.
2. **AR-02** is resolved — retention bounded, size telemetry exposed, and
   a per-parameter retention policy stated.
3. **AR-03** is resolved — §6.2 replaced with per-parameter strategies in
   the registry.
4. **AR-05** preconditions are met before Meteonomiqs promotion
   specifically. Other Stage 1 work is not blocked by this.

AR-04 and AR-06 are corrections to the document, not gates.

---

## 11. Standing risk, carried forward

The v0.1.24–v0.1.28 remediation record shows five defects introduced *by
fixes*, each sharing one shape: **the test's notion of success was
satisfiable without the code working** (see §9.8 of the remediation
audit). This expansion multiplies the surface fourfold.

The mitigation is the same discipline, applied from the start rather than
retrofitted:

- Every new parameter's fusion strategy gets a test that **fails when the
  strategy is replaced by a plain mean.** A test asserting "a number came
  out" would pass for all of them.
- Every new provider field gets a fixture **captured from a real
  response**, not written from documentation. The v0.1.28 CombiPrecip
  outage — 56 consecutive failures — came from encoding an uppercase
  filename convention out of a spec while the API served lowercase.
- Every non-standard parameter reaching the card gets an end-to-end check
  that it **renders**, not merely that it is emitted.
