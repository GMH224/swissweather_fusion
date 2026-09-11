# SwissWeather Fusion v0.3.0 — Release Audit (ICS)

**Release:** v0.3.0 (W0 — measurement repair)
**Prior release:** v0.2.8
**Schema:** v3 → v4
**Date:** 11 September 2026
**Verdict:** **PASS with one accepted degradation and two open items**
**Tests:** 734 passing (from 691), pyflakes clean across `custom_components/`

---

## 1. What this release is

v0.3.0 adds no forecasting capability. It repairs the instrument that was
supposed to answer the project's central claim: *does blending five
sources beat using the best single one?*

It is deliberately the smallest release that makes that question
answerable. Three workstreams that were designed alongside it —
correlation-aware fusion, AROME integration, learning refinements — were
all deferred, because every one of them changes blend output, and
changing the blend while measuring the blend produces a number that
describes neither the old system nor the new one.

**The release ships a measurement, not a finding.** Reading it requires
two to three months of accumulated data.

---

## 2. Findings addressed

| ID | Severity | Status |
| --- | --- | --- |
| **ARC-05** | Blocking | **Closed** — paired comparison replaces the unpaired one |
| **ARC-04** | Blocking (for W4) | **Partially closed** — run time recorded; see §5.1 |
| **ARC-08** | Medium | **Closed** — reachability assertion added |
| **ARC-11** | Low | **Closed** — verified live, no code needed |
| **ARC-09** | Blocking → **Medium** | **Downgraded on evidence**, deferred to W4 |
| **ARC-01, 02, 03, 06, 07, 10** | — | Deferred (W2–W5), untouched |

### 2.1 ARC-05 — the comparison could report a win while the blend lost

**The defect.** `_compute_temperature_mae` compared two
sample-count-weighted averages taken from `bucket_stats`: one over the
blend pseudo-source's rows, one over each provider's. Those are not the
same population.

- A provider wrote a sample for every reconcilable hour of every run,
  spanning all three lead-time buckets.
- The blend wrote at six fixed lead offsets, the longest 48 hours, so it
  never entered the `long` bucket at all.

Forecast error grows with lead time. The blend was therefore being graded
on a systematically easier set of forecasts, and the comparison ran in
its favour before any skill was involved. Separately, the blend
pseudo-source accumulated an `ema_bias` of its own, so its reported
`ema_abs_error` described a *post-hoc debiased blend* — a forecast the
integration never published.

**Consequence.** `blend_beats_best_source` could read `True` while the
shipped forecast was worse than simply using its best input. This is
precisely the pattern DEVELOPER.md §7.2 names: a passing condition
satisfiable without the thing being true.

**The fix.** A `blend_comparison` table holding one row per
(valid_at, measurement, lead_hours), carrying the blend's **published**
value and every source's raw and debiased value as of the same moment,
plus the observation once it arrives. Because all of them live on one
row, they are graded on identical targets by construction.

Each source is compared with the blend only on rows where both produced a
value, and the blend's MAE is **recomputed on that source's own subset**
rather than taken from a global average. The verdict runs against the
strongest single source, not the average one, because the alternative to
this project is not "use the average source" — it is "use whichever one
turns out to be good here".

**Removed, not deprecated.** `blend_beats_best_source` was deleted from
the payload rather than left beside its replacement. Keeping it "for
continuity" would have left a plausible-looking boolean next to the
correct one, and the wrong one has the shorter name.

**Regression test.**
`test_the_old_unpaired_comparison_can_report_a_win_when_the_blend_actually_loses`
constructs the case: ch1 beats the blend at every horizon they share
(0.4 against 0.5), the old logic reports the blend winning because it
averages ch1's 2.0 long-lead error into "ch1's accuracy", the paired
report correctly reports a loss. The old logic is transcribed into the
test file rather than imported, because a regression test for deleted
logic must carry its own copy or it asserts nothing.

### 2.2 ARC-04 — lead time was measured from poll time

`issued_at` recorded when this integration first *saw* a run, not when
the provider initialised it. `run_initialised_at` and `lead_time_basis`
are now stored per row, fetched from Open-Meteo's metadata endpoint.

`issued_at` keeps its existing meaning: `freshness_factor` and the
v0.1.19 fingerprint de-duplication both legitimately want observation
time. The two are now distinct fields rather than one field doing two
jobs.

**See §5.1 for why this is only partially closed, and for a measurement
that reduces its urgency.**

### 2.3 ARC-08 — a missing cadence entry disables freshness silently

`freshness_factor` returns 1.0 for an unknown cadence — correct in
isolation, but a new source added without a `SOURCE_UPDATE_CADENCE` entry
would get no freshness weighting at all, with no error and no log line.
Closed by a reachability assertion over every per-source map, which is
one test covering the whole class rather than one per map.

### 2.4 ARC-11 — gust units

Closed with no code change. `build_forecast_url` has appended
`wind_speed_unit=ms` since v0.1.5. The concern applied to hand-built
probe URLs, not the client path. A test now asserts the parameter is
present for **every** member of `MODEL_PARAM`, so a future source cannot
pass by bypassing the builder.

### 2.5 ARC-09 — downgraded on evidence

The concern was that Open-Meteo might substitute ARPEGE past AROME's
horizon while still labelling it AROME. Live verification on 2026-09-10
showed Open-Meteo **nulls** past the horizon rather than substituting —
confirmed for AROME HD (51 hours past init) and, as a control on a model
whose horizon we already knew, ICON-CH1 (33 hours, matching the
documented value exactly).

Downgraded Blocking → Medium. The structural horizon guard is still worth
building, because a test pins today's provider behaviour and does not
constrain tomorrow's, but it is no longer gating and has moved to W4 with
AROME.

---

## 3. Bugs introduced during this pass

This project has never been able to leave this section empty and did not
start now.

### SWF-030-001 (High) — the new columns nearly disabled bounds validation

`provider_validation.validate_forecast_rows` tested `len(row) != 6` and
passed any other shape through untouched. The moment the Open-Meteo
coordinator started appending `run_initialised_at` and `lead_time_basis`,
physical-bounds validation would have silently stopped running for ch1,
ch2 and icon_d2 — **three of five sources and the majority of all stored
values** — with every existing test still green and nothing logged.

Caught during implementation, not by a report. Fixed by validating by
field *position* rather than exact tuple length, so a future column
cannot switch it off by accident. Two regression tests, one per row
shape.

Worth naming plainly: this is the same defect class the whole release
exists to measure, and it appeared inside the fix for it.

### SWF-030-002 (High) — migration dispatch skipped the v3 rebuild

The first implementation decided whether to run `_migrate_to_v3` by
testing a single column, `forecast_snapshots.reconciliation_status`. A v2
database **has** that column and still lacks
`storm_predictions.reconciled`, so the rebuild was skipped and index
creation then failed with `no such column: reconciled` — reproducing the
exact v0.1.24 setup outage that the table/migrate/index ordering exists
to prevent.

Caught by the existing `test_v0_1_26_construction.py` suite before
release. This is the second time a hand-picked sentinel column has caused
this (see v0.2.2, SWF-021-008), so the fix is structural: per-version
requirement sets, with v4's **derived** from v3's rather than restated,
so a future v5 cannot drop a v3 requirement while copying the block.

### SWF-030-003 (Low) — a per-source sample floor no test could see

Mutation testing found that deleting the per-source minimum-sample guard
left the entire suite green: every existing fixture had a thinly-sampled
*cell* whenever it had a thinly-sampled *source*, so the cell-level guard
masked it. A guard no test can distinguish from its neighbour is a guard
nobody knows is working. Closed by a fixture where one source speaks
three times inside a well-sampled cell.

---

## 4. Pre-existing defect found, not fixed

**IND-030-01 (Low, documentation).** `const.py` stated that "the
coordinator checks each model's own `last_run_availability_time` via
Open-Meteo's metadata API before fetching". It never did — no metadata
call existed anywhere in the codebase before this release. The comment
described the plan document rather than the code.

Not a behavioural defect, but it is the documentation half of the
"implemented but never reached" class: a reader auditing freshness
handling would have concluded run-time awareness existed. The comment has
been replaced with what the code now actually does.

---

## 5. Accepted degradations and open items

### 5.1 ARC-04 is recorded, not yet consumed

`run_initialised_at` is **stored but not yet used** to derive lead-time
buckets. `derive_lead_time_bucket` still keys off `issued_at`.

This is deliberate, and the reason is a measurement taken during W1. At
the 15:00Z run on 2026-09-10:

| Model | Init | Available | Lag |
| --- | --- | --- | --- |
| ICON-CH1 | 15:00Z | 16:52Z | 1h 52m |
| ICON-D2 | 15:00Z | 16:29Z | 1h 29m |

A **22-minute** spread across the Open-Meteo sources this integration
blends today. A 24-hour-wide lead-time bucket absorbs 22 minutes
comfortably, so switching the bucket key now would churn learned state
for no measurable gain — and it would do so in the same release that
clears learned state, making the two effects impossible to separate.

The number that makes ARC-04 blocking is AROME's **5h 09m** lag against
ICON's ~1h 50m, a spread of over three hours. AROME is not in this
release. Run time is recorded now because the paired comparison keys on
exact lead offsets, where 22 minutes is no longer negligible, and because
W4 cannot begin without this data already flowing.

**Open item for W4:** switch `derive_lead_time_bucket` to run-relative
where available, and record the basis mix per bucket.

### 5.2 Learned state is cleared

By explicit maintainer decision, not by necessity. Unlike v0.1.24's
rebuild, nothing in v0.3.0 changes what a learned weight *means* —
`ema_bias` and `ema_abs_error` carry identical semantics before and
after. The wipe was chosen for simplicity over reasoning about buckets
that mix poll-relative and run-relative lead times.

Two consequences, stated in the README rather than discovered by users:

1. Every source returns to cold start; the blend degenerates to a plain
   average of raw values until buckets refill. **A visible accuracy dip
   for the first days is the reset, not a regression.**
2. The SON season buckets accumulated since 1 September are lost with
   everything else. The autumn boundary passed on 1 September, so this
   discards the newest and most currently-relevant data rather than stale
   summer history.

### 5.3 Temperature, humidity and pressure only

Class B and Class C parameters are not recorded for comparison, because
they have no local ground truth by definition — that is what makes them
Class B and C. A comparison row for them could never be scored, and
recording them to be abandoned later would make a stalled measurement
look busy.

**This means precipitation, gusts and cloud cover remain unmeasured**,
and they are what most users judge a forecast by. Closing that gap
requires reconciling precipitation against CombiPrecip radar, which
changes the class taxonomy and is the strongest v0.4 candidate.

### 5.4 The verdict will read `null` for weeks

Each cell needs 30 scored pairs. A 48-hour-lead cell cannot record its
first pair until 48 hours after the forecast. This is correct behaviour
and is documented on the sensor, which reports sample counts alongside
the null so that "not yet" and "something is broken" are visibly
different — the ambiguity that hid a broken accuracy sensor for four
releases (SWF-P1-007).

---

## 6. Verification record

**Live verification (2026-09-10).** Seven Open-Meteo requests plus two
metadata endpoints, run by the maintainer against the live API. Findings
that changed this release: AROME HD carries 9 of 24 requested variables
(including all three it was scoped for); its horizon is 51h from init,
not the 42–48h documented publicly; its cadence is 3h, not 6h;
`minutely_15` is interpolated from the hourly series rather than native
AROME-PI output, which removes the Model B nowcast gain the handover
hoped for; no ARPEGE substitution occurs.

**Metadata path verified against two providers**, not one —
`meteoswiss_icon_ch1` and `dwd_icon_d2` — so the path could not be
provider-scoped by coincidence. Both captures are committed verbatim in
the test file per §7.3. Inventing a plausible Open-Meteo path without
checking is the v0.1.1 defect this project already paid for once.

**Test suite.** 734 passing, up from 691. 45 new tests. The four tests
that asserted v0.2.4's now-removed verdict were rewritten rather than
deleted, and the replacement documents at its own site why the old
assertion held and was wrong anyway.

**Mutation testing** on `models/comparison.py`, the analytical core:

| Mutation | Caught |
| --- | --- |
| Verdict sign flipped | Yes (3 tests) |
| Weakest source used as benchmark instead of strongest | Yes (3 tests) |
| Per-source pairing replaced with a global blend MAE | Yes (1 test) |
| `None` verdict collapsed to `False` | Yes (2 tests) |
| Per-source sample floor removed | **No** → SWF-030-003, now caught |

**Not done:** no live run of v0.3.0 against a real Home Assistant
instance. Every result above is from the test suite and from live
provider responses, not from the integration running in production. The
migration is exercised against a complete v3 fixture and a complete v2
fixture, both built at their real shapes rather than as convenient
subsets — but a fixture is not an installation.

---

## 7. What to send back in 2–3 months

Download diagnostics from the device page and send the file. The
`internal_coordinators.blend_comparison` block contains the entire
report: per-cell sample counts, per-source MAE, margins, the window
covered, and the verdict. Nothing else is needed, and no shell access to
the database is required.

The one thing worth checking before then: if
`internal_coordinators.blend_comparison.total_pairs` is still 0 after a
week, the measurement is not running and that is worth investigating
rather than waiting out.
