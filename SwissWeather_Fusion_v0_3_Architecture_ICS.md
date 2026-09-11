# SwissWeather Fusion — v0.3 Architecture and Work Plan (ICS)

**Prepared against:** `swissweather-fusion-v0.2.8` source (97 files, 600 test
functions / 692 tests, pyflakes clean)
**Carried forward from:** `SwissWeather_Fusion_v0_3_Session_Handover.md`
**Date:** 9 September 2026
**Status:** **W0 IMPLEMENTED AND RELEASED as v0.3.0. W1 COMPLETE.
W2–W5 DEFERRED PENDING MEASUREMENT.**
**Code written during this pass:** none. Nothing in the v0.2.8 tree was
modified. Every claim below is stated with the file and line region it was
read from, so it can be re-checked without trusting this document.

---

## 0a. Update — 11 September 2026

This document was written before W1's live verification and before any
code. Both have now happened, and the scope narrowed sharply as a result.

**What shipped:** W0 only, as v0.3.0 — the paired measurement, run-time
recording, and the retention bound on the measurement table. See
`swissweather_fusion_v0.3.0_release_audit.md`.

**What changed in this plan, and why:**

| Item | Then | Now |
| --- | --- | --- |
| Release scope | W0+W2, then W3, W4, W5 | **W0 alone.** W2–W5 all change blend output, and changing the blend while measuring the blend produces a number describing neither system |
| ARC-04 | High, gated W5 | **Blocking for W4**, but the measured lag spread across *current* sources is 22 minutes, so run-relative bucketing is deferred with AROME (§5.1 of the audit) |
| ARC-09 | Blocking | **Medium.** Open-Meteo nulls past a model's horizon rather than substituting — verified against AROME HD and, as a control, ICON-CH1 |
| ARC-11 | Low | **Closed.** No code needed; `wind_speed_unit=ms` has been on every URL since v0.1.5 |
| AROME `minutely_15` | Possible Model B nowcast gain | **Does not exist.** Interpolated from the hourly series: every hour boundary matches exactly, and values between them overshoot both endpoints |
| AROME scope | Precipitation, gusts, CAPE | Unchanged, and now **confirmed by data** — those three are among the 9 of 24 variables AROME HD actually populates |
| §2.1's blocked precondition | Two input ICS documents missing | **Moot for v0.3.0.** W0 depends on neither. Still required before W2 |

**What W1 established that this document assumed:** AROME HD's horizon is
51 hours from init, not the 42–48h publicly documented; its cadence is 3
hours, not 6; the metadata path is
`https://api.open-meteo.com/data/{model}/static/meta.json`, verified
against two different providers.

**The open question is now the only question.** v0.3.0 ships an
instrument and no finding. Sections 4.2 through 4.7 below remain the
design for W2–W5, and none of them should be built until the instrument
has reported. If it reports that the blend does not beat its best single
source, most of what follows should be deleted rather than implemented.

---

## 0. How to read this

Section 2 lists what is blocked and why, and should be read before the
findings. Section 3 is the findings from reading the v0.2.8 source against
the handover's claims — eleven items, three of them blocking. Section 4 is
the proposed target architecture. Sections 7 and 8 are the work plan and
test strategy, which is what "describe work" was asked for.

**The one thing to take away if nothing else is read:** the handover's §5
sequencing starts with "let v0.2.8 run and report", on the basis that the
blend-vs-best-source number reframes everything downstream. That is the
right instinct. But the instrument that produces that number cannot
currently answer the question it was built for (**ARC-05**, Blocking). The
first work item in v0.3 is therefore repairing the measurement, not waiting
on it. Waiting on it as it stands would burn a season of accumulated data
and produce a number that would be believed and should not be.

---

## 1. Executive summary

The handover is accurate about the state of the project and correct in
almost all of its judgements. Three refinements follow from reading the
source rather than the documentation:

1. **Correlation handling is a larger change than "add a family field".**
   For Class B and Class C parameters — which is most of them — the fusion
   layer discards source identity *before* combining
   (`coordinator.py:_fuse_class_b`, `:_resolve_categorical`, both of which
   build a bare `values` list). No correlation rule of any kind is
   expressible against that interface. This is the real precondition for
   AROME, and it is an interface change, not an added parameter (**ARC-01**).

2. **The measurement instrument is not sound.** `blend_beats_best_source`
   compares two MAE figures computed over different populations, at
   different lead-time distributions, and it credits the blend with a
   post-hoc debiasing that the published forecast never receives. It is the
   §7.2 trap — a number whose "passing" condition is satisfiable without the
   thing being true (**ARC-05**).

3. **Finer lead-time buckets are gated on the run-time fix.** `issued_at` is
   poll time, not model initialisation time. A 24-hour-wide bucket absorbs a
   3-hour attribution error; a 3-hour bucket does not. The Open-Meteo
   metadata API item in handover §6.2 is therefore a *precondition* for
   handover §5 step 5, not an independent improvement (**ARC-04**).

Everything else in the handover's sequencing survives. The revised order is
in §7.

**Scope proposed for v0.3:** measurement repair, source-metadata layer,
correlation-aware fusion, AROME via Open-Meteo scoped to precipitation /
gusts / CAPE, and two of the three learning refinements. Hierarchical
shrinkage, a regime dimension, and the direct WCS raster route are
explicitly out (§10).

---

## 2. Preconditions and blocked inputs

### 2.1 The two v0.3 input documents were not supplied

Handover §8 lists them as "to be supplied by the maintainer":

- `SwissWeather_Fusion_Learning_Algorithm_ICS.md`
- `SwissWeather_Fusion_AROME_Integration_and_Model_Expansion_ICS.md`

Neither is in the v0.2.8 zip. The zip contains
`SwissWeather_Fusion_Model_A_Expansion_and_Weather_Card_Architecture.md`
and its review, but those are the v0.2 documents, already implemented and
already reviewed (AR-01…AR-06, all dispositioned).

**Consequence:** handover §9.4 — "produce a formal review of the two input
documents in the established ICS format **before** any implementation" —
cannot be executed. This document is *not* that review. It is an
independent architecture derived from the handover plus the source, and it
assesses the two documents only through the handover's summary of them
(§4.1–§4.3), which is second-hand by definition.

If those documents contain design detail the handover's summary omits, this
architecture must be re-reconciled against them before W2 starts. That
re-reconciliation is cheap if done then and expensive if done later.

### 2.2 No diagnostics export was supplied

Handover §9.2 asks for a fresh diagnostics export to check
`blend_beats_best_source`. None was provided, and per **ARC-05** the number
would not be trustworthy if it had been. W0 addresses this.

### 2.3 What was verified for this document

Read in full or in relevant part: `models/model_a.py`, `models/model_b.py`
(structure), `forecast_parameters.py`, `storage/db.py` (schema and index
rules), `clients/open_meteo.py`, `const.py` (sources, cadence, learning
constants), `coordinator.py` (blend, fusion dispatch, reconciliation, MAE
computation), the v0.2.8 release audit headings, the Model A architecture
review, and DEVELOPER.md's known-gaps list. Grep-verified absences are
marked as such.

---

## 3. Findings

Severity uses the project's established scale. IDs are prefixed `ARC-` for
this document, continuing the `AR-` convention of the Model A review.

| ID | Severity | Finding |
| --- | --- | --- |
| **ARC-01** | **Blocking** | Class B/C fusion discards source identity before combining; no correlation rule is expressible |
| **ARC-05** | **Blocking** | The blend-vs-best-source comparison is unpaired, asymmetric, and temperature-only |
| **ARC-09** | **Blocking** | Nothing structurally prevents a mislabelled provenance row if a provider substitutes a model past its horizon |
| **ARC-02** | High | Model family is not representable; SRF's family is genuinely unknown and must not be guessed |
| **ARC-03** | High | `weather_code` majority vote is one derived algorithm applied to three correlated inputs |
| **ARC-04** | High | Finer lead-time buckets are unsound until `issued_at` is model run time |
| **ARC-06** | High | Correlation collapse must **not** be applied to envelope strategies (`max`, `min`) |
| **ARC-07** | Medium | The single-URL variable list is a shared failure domain for a new model |
| **ARC-08** | Medium | A missing `SOURCE_UPDATE_CADENCE` entry silently disables freshness weighting |
| **ARC-10** | Medium | Season is the sparsity problem, not sample rate; the numbers say so |
| **ARC-11** | Low | The gust unit risk in handover §3.2.4 is already mitigated — conditionally |

---

### ARC-01 (Blocking) — Class B and C fusion cannot express correlation

`coordinator.py:_fuse_class_b` builds its input as:

```
values = [
    latest_forecast[(source, measurement, target_iso)][0]
    for source in ALL_FORECAST_SOURCES
    if (source, measurement, target_iso) in latest_forecast
]
```

and then calls `parameter.fuse_values(values)`. `_resolve_categorical` does
the same and counts occurrences. `ForecastParameter.fuse_values` takes
`Sequence[Optional[float]]`. Every strategy —
`fuse_mean`/`fuse_median`/`fuse_max`/`fuse_min`/`fuse_wind_bearing` — takes
a bare sequence of floats.

**By the time a value reaches a fusion strategy, which model produced it is
gone.** There is no seam at which a family rule could be applied. This is
not a missing feature; it is an interface that forecloses the feature.

Class A is better placed: `model_a.blend()` takes
`list[SourceContribution]`, and `SourceContribution` carries `source`. A
family rule for Class A is an additive change inside one function.

**Scale of what this affects.** Of the 26 entries in `PARAMETERS`, three are
Class A (temperature, humidity, pressure). The rest — precipitation, rain,
showers, snowfall, snow depth, precipitation probability, wind speed, gusts,
bearing, dew point, apparent temperature, cloud cover, visibility, UV,
sunshine duration, CAPE, CIN, freezing level, snowfall height, cloud base,
predictability — plus `weather_code` in Class C, all go through the
identity-erasing path. That includes every parameter AROME is being added
for.

**Fix (design in §4.2):** change the fusion contract from
`Sequence[float]` to `Sequence[Contribution]` where `Contribution` carries
at least `source`, `value`, and the source's family. The strategies that
genuinely do not care (see **ARC-06**) can ignore the labels, but they must
be *given* them, so the choice is explicit per strategy rather than
structural.

**Sequencing consequence, restated because it is the crux:** the handover
says diversity handling is a precondition for AROME. That is right, and the
reason is stronger than stated — without it, AROME's precipitation and gust
values would be fused by strategies that literally cannot tell AROME from
ICON-CH1.

---

### ARC-02 (High) — family is not representable, and SRF's family is unknown

Grep for `family|correlat` across `custom_components/` returns three hits,
all incidental prose in comments. Confirmed: there is no family concept in
the codebase, exactly as the handover states.

The non-obvious part: the handover's §4.2 says three of five sources are one
family (ICON-CH1, ICON-CH2 MeteoSwiss ICON; ICON-D2 DWD ICON). But §2.2
describes SRF as "MeteoSwiss-derived". If SRF's underlying numerical model
is ICON-CH1 or ICON-CH2 — plausible, since MeteoSwiss runs those
operationally — then **four of five sources are one family**, and the
current blend is closer to a single opinion than to a consensus.

This is not established. `clients/srf.py` documents the endpoint shape, not
the upstream model, and DEVELOPER.md records SRF as "out of scope" for
deeper work.

**The project's own rule applies here.** `unit_conversion.py` rejects
unrecognised units rather than guessing; the Wetter-Alarm `priority` scale
is preserved verbatim because it is undocumented. Family assignment must
follow the same rule: SRF's family is declared `UNKNOWN`, and `UNKNOWN` is
treated as *its own singleton family* — the conservative choice, because
assuming independence overstates diversity and assuming ICON kinship
under-weights a source that may be genuinely independent. The asymmetry is
deliberate: an `UNKNOWN` source keeps its full vote but never absorbs
another family's.

**This is a research item, not a code item.** It belongs in W1 alongside the
AROME live checks, and `srf_probe.py` already exists as the vehicle for
asking SRF questions against a live response.

---

### ARC-03 (High) — the condition majority vote is not three opinions

Handover §4.2 states this and it is confirmed in the source.
`_resolve_categorical` counts raw `weather_code` values across
`ALL_FORECAST_SOURCES` and breaks ties toward the more severe code. For
CH1/CH2/D2 those codes are synthesised by Open-Meteo from model fields —
MeteoSwiss publishes no WMO symbol — so the "majority" can be three
instances of one derivation algorithm agreeing with itself.

This compounds with **ARC-01**: the tie-break toward severity is a sensible
rule applied to an unsound vote count.

**Fix:** provenance must be a first-class property of a stored value, not
just of a source. A `derived_by` field distinguishes "the provider computed
this" from "Open-Meteo computed this from the provider's fields". The
categorical resolver then counts *distinct (family, derivation)* pairs, so
three Open-Meteo-derived ICON codes contribute one vote's worth, and a
native code from meteoblue contributes its own.

---

### ARC-04 (High) — run-time attribution gates finer lead-time buckets

`clients/open_meteo.py:parse_forecast_response` sets
`issued_at = datetime.now(timezone.utc)` — documented in the docstring as a
deliberate stand-in. `coordinator.py:_reconcile` derives the bucket key with
`derive_lead_time_bucket(issued_at, valid_at)`.

So the recorded lead time is *poll-relative*, not *run-relative*. The error
is bounded by the poll interval plus the provider's publication lag: with
`OPEN_METEO_CHECK_INTERVAL = 15 min` against a 3-hour model cadence, the
attribution error is small relative to a bucket boundary at 24 h. It is not
small relative to a boundary at 3 h or 6 h.

**Therefore:** handover §5 step 5 ("finer horizons") depends on handover
§6.2's metadata-API item. Introducing finer buckets first would produce
buckets whose contents are systematically misfiled near every boundary, and
the resulting learned weights would look plausible and be wrong — the
failure mode §7.2 identifies as the one this project is least able to
detect.

The metadata API is also, per handover §2.3, free of rate limits and already
verified to expose `last_run_initialisation_time`. This is the cheapest
high-value item in the whole plan.

---

### ARC-05 (Blocking) — the measurement cannot answer its question

`coordinator.py:_compute_temperature_mae` is the source of
`blend_accuracy`, `best_source_accuracy` and `blend_beats_best_source`.
Four problems, in increasing order of severity:

**(a) Temperature only.** `if row["measurement"] != "temperature": continue`,
twice. Defensible as a starting point, but the sensor is named as though it
covers the blend.

**(b) The comparison is unpaired.** Provider rows enter `bucket_stats` for
every reconcilable hour of every run — for ICON-CH1 at 3-hour cadence over a
33-hour horizon, roughly 24 valid hours × ~11 covering runs per day per
measurement. The blend writes verification rows only at
`BLEND_VERIFICATION_LEAD_HOURS = (1, 3, 6, 12, 24, 48)`. The two MAEs are
therefore computed over different sample populations with different
lead-time distributions. Since forecast error grows with lead time, and the
blend's six offsets are weighted toward short leads while a provider's rows
spread across its whole horizon, **the comparison is biased in the blend's
favour before any skill is involved.**

The bucket arithmetic makes the asymmetry concrete. Providers occupy all
three lead-time buckets (24 hours × 4 seasons × 3 buckets × 5 sources × 3
measurements = 4,320). The blend's six offsets fall only in `short` (1, 3,
6, 12 h) and `medium` (24, 48 h) — never `long` — giving 576 blend buckets.
The blend is never graded on the horizon where it is weakest.

**(c) The blend is credited with a debiasing it never receives.** For a
provider, `ema_abs_error` is the post-debias residual, and that debiasing is
genuinely applied in `model_a.blend()`. For the blend pseudo-source, the
same statistic is computed — `bucket_stats` treats it like any other source
— so `blend_mae` describes a hypothetical *debiased blend*. The published
forecast is `model_a.blend()`'s raw output, with no post-hoc blend-level
bias subtraction anywhere in `_compute_blend`. The number reported as the
blend's accuracy is the accuracy of something that is never shown to
anyone.

**(d) Consequently, `blend_beats_best_source` can read `true` while the
published product is worse than its best input.** That is precisely the
§7.2 pattern: "a test whose passing condition is satisfiable without the
code working."

**This blocks the 1.0.0 criterion**, which handover §5 step 6 defines as
"the accuracy sensors show fusion earning its complexity". The sensor as
built cannot establish that.

**Fix (design in §4.8):** a paired comparison. Same target hours, same lead
offsets, same measurements, for blend and providers alike; blend graded on
its published value; providers graded on the debiased value that actually
enters the blend, which is the honest counterfactual ("would using this one
source, bias-corrected, have been better?"). Report a per-lead-offset
breakdown rather than one aggregate, because "the blend wins at 1 h and
loses at 48 h" is an actionable result and a single averaged boolean is not.

---

### ARC-06 (High) — do not collapse families under envelope strategies

This is the design point most likely to be got wrong when **ARC-01** is
fixed, so it is stated separately.

Once fusion strategies receive source labels, the tempting move is to apply
one family rule uniformly. That is wrong. The strategies split into two
kinds with opposite semantics:

- **Consensus strategies** — `fuse_mean`, `fuse_median`, the categorical
  majority, `fuse_wind_bearing`. These ask *what do the models agree on?*
  Correlated members inflate apparent agreement, so family collapse is
  required.
- **Envelope strategies** — `fuse_max` (gusts, CAPE, convective inhibition),
  `fuse_min` (visibility). These do not ask what the models agree on. Per
  `forecast_parameters.py`'s own rationale, they deliberately take the
  hazard-side extreme: "averaging away one model's warning is the wrong
  direction to be wrong in." A family rule that suppressed one ICON member's
  high gust because two siblings were lower would **defeat the strategy's
  entire purpose**, and would do so silently.

**Rule:** correlation handling applies to consensus strategies only. Each
`ForecastParameter` declares its strategy kind explicitly, and a reachability
test asserts every parameter has one — no defaulting, because a default is
how a new parameter inherits the wrong behaviour (the stated reason the
registry exists at all).

There is a legitimate middle case worth naming and deferring: a family's
*spread* under an envelope strategy carries information (three ICON members
disagreeing about gusts means something different from three agreeing). That
is a Model B input, not a Class B fusion change, and it is out of scope for
v0.3.

---

### ARC-07 (Medium) — one URL, one failure domain

`build_forecast_url` puts all 23 `HOURLY_VARIABLES` plus
`OPTIONAL_HOURLY_VARIABLES` into a single request per model. The
`include_optional` retry exists precisely because an unverified variable
could fail the whole request — the v0.2.1 reasoning for `uv_index`: "Three
sources dying at once to gain one nice-to-have is a bad trade."

AROME HD's variable coverage differs from ICON's, and handover §3.2.2 lists
this as an open question. The existing mitigation is a single flat optional
set shared by all sources, which is not expressive enough: a variable that
is core for ICON may be absent for AROME and vice versa.

**Fix:** `HOURLY_VARIABLES_BY_SOURCE` — a shared core plus per-source
required and optional sets, with the existing retry-without-optional
behaviour preserved per source. The live answers from W1 populate it. No
variable goes in a source's *required* set until a live response has shown
it non-null for that source, per §7.3.

---

### ARC-08 (Medium) — a missing cadence entry fails silently

`model_a.freshness_factor` returns `1.0` when cadence is `None`, documented
as "a missing signal never silently reweights anything". Correct in
isolation. But `_blend_at` reads cadence via
`SOURCE_UPDATE_CADENCE.get(source)`, so a new source added to
`ALL_FORECAST_SOURCES` without a cadence entry gets no freshness weighting
at all, with no error, no log line, and a plausible-looking result.

That is the §7.1 family exactly: implemented, unit-tested, never reached.

**Fix:** a reachability test asserting `set(SOURCE_UPDATE_CADENCE) ⊇
set(ALL_FORECAST_SOURCES)`, extended in the same test to every other
per-source map introduced in v0.3 (family, horizon, variable set). One test,
covering the whole class.

---

### ARC-09 (Blocking) — provenance needs a structural guard, not a test

Handover §3.2.3 raises the ARPEGE-substitution risk: `FORECAST_HOURS_AHEAD`
is 168, AROME's horizon is ~48 h, and if Open-Meteo returns values rather
than nulls past 48 h we would be "blending ARPEGE while labelling it AROME".

The handover proposes testing this with `forecast_days=7`. That is the right
live check (it is in W1), but a test verifies today's provider behaviour and
does not constrain tomorrow's. Provider behaviour changing silently under a
stable contract is this project's single most expensive recurring failure —
CombiPrecip's documented-uppercase / served-lowercase outage, 56 consecutive
failures.

**Fix:** `SOURCE_MAX_HORIZON_HOURS`, enforced at the storage boundary, so a
row labelled with a source can never exist beyond that source's declared
horizon regardless of what the API returns. Structural, cheap, and it makes
the provenance claim true by construction rather than by observation. It
also gives the existing `provider_validation.py` a natural home for the
check — that module already guards what reaches storage, which is exactly
the right layer.

Note the retention consequence: AROME data retention upstream is 5 days with
no archive (handover §3.3), so nothing here can be repaired retrospectively.
A provenance error in AROME rows is permanent. That is what makes this
blocking rather than high.

---

### ARC-10 (Medium) — season is the sparsity problem; sample rate is not

The handover's ~4,300 buckets is confirmed exactly: 24 × 4 × 3 × 5 × 3 =
**4,320**, plus 576 for the blend pseudo-source = 4,896 addressable keys.

But sample *supply* is high. `_reconcile` folds in every pending
`forecast_snapshots` row that finds a matching observation — not one per
(source, measurement, hour), but one per row, and each valid hour is covered
by every run whose horizon reaches it. Order-of-magnitude, a 3-hour-cadence
source contributes a few hundred samples per measurement per day.

So the arithmetic on finer lead-time buckets is less alarming than it looks:
eight lead buckets instead of three gives 24 × 4 × 8 × 5 × 3 = **11,520**,
which the sample rate can feed within weeks. AROME does not change this at
all under the recommended scoping, because precipitation/gusts/CAPE are
Class B and never enter `bucket_stats`.

**What sample rate cannot fix is a partition that resets.** `derive_season`
is a hard four-way split with no fallback; `bucket_lookup` misses, and the
source falls to cold start. Four times a year, a quarter of the grid empties
at once, and the autumn transition is imminent.

**This reframes the handover's §4.3 concern.** The objection to the learning
document was that shrinkage is hard to get right and easy to get subtly
wrong. True. But the *specific* sparsity that hurts today is one dimension
with one predictable failure mode, and it has a much simpler answer than
hierarchical shrinkage: a season-fallback rule (fall back to the
season-agnostic aggregate for that hour/lead/source/measurement until the
in-season bucket has samples). That is a single well-defined backoff, not a
shrinkage hierarchy, and it is testable by construction — with zero samples
it must equal the season-agnostic value, with many it must equal today's
value, and the crossover must be monotone. It delivers most of what
shrinkage was wanted for, at a fraction of the risk.

---

### ARC-11 (Low) — the gust unit risk is already mitigated, with a condition

Handover §3.2.4 warns that AROME gusts arrive in km/h while the project
vocabulary is m/s, citing SWF-P2-002. `build_forecast_url` already appends
`&wind_speed_unit=ms` for every Open-Meteo source, added in v0.1.5 for
exactly this reason.

So the risk applies to hand-built probe URLs (which is where the handover's
observation came from — the live test in §3.1 shows km/h) and **not** to the
client path, provided AROME is routed through `build_forecast_url` by adding
it to `MODEL_PARAM`, rather than given its own builder.

**Condition:** a test asserting `wind_speed_unit=ms` is present in the URL
for *every* member of `MODEL_PARAM`, not just for a named source. Written
that way, the test cannot be passed by a new source that bypasses the
builder.

---

## 4. Target architecture

### 4.1 Source metadata layer (new module: `sources.py`)

Today, per-source facts are scattered: `MODEL_PARAM` in the client,
`SOURCE_UPDATE_CADENCE` in const, `ALL_FORECAST_SOURCES` in const, variable
lists in the client, nothing at all for family, provenance or horizon.

v0.3 introduces one frozen dataclass per source, in one place:

```
SourceMeta:
    key                  # "ch1"
    display_name
    family               # ModelFamily enum, incl. UNKNOWN
    provider             # who serves it (open-meteo, srf, meteoblue…)
    cadence              # timedelta
    max_horizon_hours    # ARC-09 guard
    derived_fields       # which parameters are provider-derived (ARC-03)
    role                 # MODEL_A_BLEND | MODEL_B_ONLY | OBSERVATION
```

`ALL_FORECAST_SOURCES` and `SOURCE_UPDATE_CADENCE` become derived views over
this registry, so the two cannot drift apart and **ARC-08** is closed
structurally rather than by remembering.

Family assignment as proposed, pending W1:

| Source | Family | Basis |
| --- | --- | --- |
| ch1 | `ICON_MCH` | MeteoSwiss ICON-CH1 |
| ch2 | `ICON_MCH` | MeteoSwiss ICON-CH2 |
| icon_d2 | `ICON_DWD` | DWD ICON-D2 |
| srf | `UNKNOWN` | **ARC-02** — not established |
| meteoblue | `MULTIMODEL` | mLM is itself a multi-model blend |
| arome (v0.3) | `AROME` | Météo-France AROME |

Two judgement calls worth flagging for the maintainer. First, `ICON_MCH` and
`ICON_DWD` are held as *separate* families despite a shared ICON lineage:
different domains, different initial conditions, different assimilation.
Treating them as one would be a stronger claim than the evidence supports,
and the conservative direction here is to under-collapse. Second,
`MULTIMODEL` for meteoblue is honest but awkward — mLM may well contain ICON
internally, which would make it partially correlated with three other
sources in a way no family label can express. Recorded as a standing
limitation rather than papered over.

### 4.2 Correlation-aware fusion

**Class A** (`model_a.blend`): family-normalised weights. Each family
receives a total weight equal to one member's, divided among its members —
"one family, one vote's worth, split". This is a *structural* assumption
(members of a family are treated as fully correlated), stated as such, not a
measured correlation. It composes cleanly with the existing machinery: the
reference-weight scaling (IND-01) and the 8:1 clamp both operate before
normalisation, so their meanings are unchanged.

Under this rule, today's blend goes from an effective 5 votes to 3 (ICON_MCH
1 + ICON_DWD 1 + meteoblue 1) plus SRF's `UNKNOWN` singleton = 4. Adding
AROME makes it 5. **The whole point:** AROME adds a genuinely new opinion,
where a fourth ICON member would have added almost nothing.

**Class B, consensus strategies:** two-stage. Collapse each family to a
single value using the parameter's own strategy, then apply the strategy
across families. For precipitation with `fuse_median` and sources at
CH1 = 0, CH2 = 0, D2 = 8, SRF = 6, meteoblue = 5: today's median over
[0, 0, 5, 6, 8] is 5. Two-stage gives ICON_MCH → 0, ICON_DWD → 8,
UNKNOWN → 6, MULTIMODEL → 5, median over [0, 5, 6, 8] = 5.5. The difference
is modest here by construction; it becomes large exactly when the ICON
family votes as a bloc, which is the case the change exists for.

**Class B, envelope strategies:** unchanged. See **ARC-06**.

**Class C:** distinct (family, derivation) pairs vote once. See **ARC-03**.

**Interface change:** `ForecastParameter.fuse_values` takes labelled
contributions. `fuse_mean`/`median`/`max`/`min` keep bare-sequence
signatures internally and are called by a wrapper that does or does not
collapse, per the declared strategy kind. This keeps the pure functions pure
and testable, which they currently are and should remain.

### 4.3 Provenance and horizon guard

`derived_fields` on `SourceMeta` marks parameters the *serving provider*
synthesised rather than the model producing (`weather_code` for all three
ICON sources via Open-Meteo). `SOURCE_MAX_HORIZON_HOURS` is enforced in
`provider_validation.py` at the storage boundary. Both are inert for the
existing five sources except `weather_code`, whose vote count changes — a
deliberate behaviour change, called out in the release notes.

### 4.4 Run-time attribution

Open-Meteo's metadata API supplies `last_run_initialisation_time` per model.
`ParsedForecast` gains `run_initialised_at`, distinct from `issued_at`
(which stays as first-seen time, since `freshness_factor` and the v0.1.19
fingerprint de-duplication both legitimately want observation time).
`derive_lead_time_bucket` switches to run time where available and falls
back to `issued_at` where not — SRF, meteoblue and any source without
metadata.

**The fallback must be visible**, not silent: a per-source
`lead_time_basis` field recorded alongside, so a bucket's provenance is
knowable. Mixing run-relative and poll-relative lead times in one bucket
without recording which is which would be a new instance of the class of
defect this project keeps finding.

### 4.5 Learning refinements

Two of the three, in this order:

1. **Continuous confidence**, replacing the `MIN_SAMPLES_TO_TRUST_BUCKET = 5`
   cliff. A confidence factor rising from 0 at n = 0 toward 1, blending the
   raw value toward the debiased value and the neutral reference weight
   toward the learned weight. **Constraint that makes this safe:** it must be
   a strict generalisation — substituting a step function at n = 5 must
   reproduce v0.2.8 output exactly, and that is a test, not a claim.
2. **Season fallback** (**ARC-10**), which is the smallest useful piece of
   what the learning document wanted shrinkage for.

**Finer lead-time buckets are deferred within v0.3** until §4.4 has run long
enough to show run-time attribution working on live data. Not blocked
outright — gated on evidence.

Hierarchical shrinkage across horizon × season × regime × parameter × source
is out of scope (§10).

### 4.6 AROME integration

Scoped as the handover recommends: **precipitation, wind gusts, CAPE.** Not
temperature.

Consequences of that scoping, which are larger than they first appear:

- All three are Class B. AROME therefore **never enters `bucket_stats`** and
  adds zero learning buckets. The sparsity objection to adding a source does
  not apply.
- But the same fact means the existing machinery cannot measure whether
  AROME helps. Class B has no local ground truth by definition — that is why
  it is Class B.
- The only available ground truth for AROME's headline parameter is
  **CombiPrecip radar**, already in the database as
  `radar_observations.precip_accum_mm_1h`, currently a Model B input only.

Reconciling forecast precipitation against radar accumulation would make
precipitation learnable and would let AROME's value be measured rather than
asserted. It is a real option and a significant one. It is **not proposed
for v0.3**: it changes the class taxonomy, requires care about point-versus-
grid representativeness, and would expand scope in a release whose whole
purpose is to establish measurement discipline. Recorded in §10 as the
strongest candidate for v0.4, with the note that until it exists, "AROME
earns its place" is a judgement, not a measurement — and the project should
say so in its own documentation rather than let the ambiguity ride.

Implementation is otherwise small: add `arome` to `MODEL_PARAM` (**ARC-11**),
add its `SourceMeta`, add its per-source variable set (**ARC-07**), declare
its horizon (**ARC-09**). If `minutely_15` proves genuine at these
coordinates (W1), it is a Model B input for precipitation timing and is
handled as a separate, later item — Model B changes are not in v0.3's
critical path.

### 4.7 Schema

Schema v4. Changes:

- `forecast_snapshots`: `run_initialised_at TEXT NULL`,
  `lead_time_basis TEXT NOT NULL DEFAULT 'poll'`
- `bucket_stats`: unchanged in shape. The season-fallback rule (§4.5) reads
  existing rows differently rather than storing new ones — deliberately, to
  avoid a migration that rewrites learned history.
- New `blend_comparison` table for the paired measurement (§4.8), rather
  than overloading `forecast_snapshots` with a pseudo-source, which is what
  makes the current comparison unpaired.

**Migration rules that apply, from handover §7.4 and `db.py`'s own
comment block:** every new index goes in `_INDEX_SQL`, never `_TABLE_SQL`
— asserted by an existing test. The migration test builds the **complete**
v3 schema, not a partial fixture. Both have bitten this project before, and
the second one is the more dangerous because a partial fixture gets cited as
coverage.

Learned history is preserved across this migration. v0.1.24's precedent for
discarding it (schema v3) existed because the weight semantics changed
incompatibly; here they do not, and discarding a season's data immediately
before the autumn boundary would be gratuitous.

### 4.8 The measurement instrument

New `blend_comparison` table, one row per (target hour, measurement, lead
offset), recording: the published blend value, each source's raw value, each
source's debiased value, and the observation once available. Populated at the
same six lead offsets for every participant, so the populations are identical
by construction.

Reported per lead offset, per measurement:

- blend MAE vs best-single-source MAE (debiased), on identical targets
- the win/loss margin, not just a boolean
- sample count backing each figure

`blend_beats_best_source` remains as a headline attribute but becomes a
strict aggregate of the paired figures, and it reports `None` — not `false` —
until every offset has a minimum sample count. The existing sensor's
"None when nothing is learned" ambiguity (SWF-P1-007, four releases of
silently-broken code) is avoided by reporting sample counts alongside, so
`None` is always distinguishable from `None because it crashed`.

---

## 5. Module change map

| Module | Change | Risk |
| --- | --- | --- |
| `sources.py` | **new** — SourceMeta registry | Low; pure data |
| `const.py` | `ALL_FORECAST_SOURCES`, `SOURCE_UPDATE_CADENCE` become derived views | Medium — widely imported |
| `forecast_parameters.py` | strategy kind; labelled contributions; family collapse wrapper | **High** — the ARC-01 interface change |
| `models/model_a.py` | family-normalised weights; continuous confidence; season fallback | **High** — blend output changes |
| `coordinator.py` | `_fuse_class_b`/`_resolve_categorical` pass labels; paired comparison; run-time plumbing | **High** — 157 KB module, most-touched file |
| `clients/open_meteo.py` | AROME in `MODEL_PARAM`; per-source variable sets; metadata API; `run_initialised_at` | Medium |
| `provider_validation.py` | horizon guard | Low |
| `storage/db.py` | schema v4; `blend_comparison`; migration | Medium — migration risk is the whole risk |
| `sensor.py` | paired-comparison attributes; flat scalars (§7.5) | Low |
| `diagnostics.py` / `redaction.py` | new fields must be redaction-reviewed | Medium — v0.1.20 precedent |

`coordinator.py` at 157 KB is the largest single risk surface in the
project, and v0.3 touches it in four places. Splitting it is tempting and is
**not** proposed here: a refactor concurrent with behaviour changes would
make the release audit unable to attribute any regression to a cause.
Recorded as a v0.4 candidate.

---

## 6. What changes for the user

Stated plainly because it belongs in release notes, not buried in design:

- Blended values change for every parameter, because family weighting
  changes the arithmetic. Not a bug report; expected.
- Conditions (`weather_code`) may change more visibly than numeric
  parameters, because a 3-vote bloc becomes 1 vote.
- Gusts, CAPE, CIN and visibility do **not** change from the correlation
  work (**ARC-06**).
- New accuracy attributes appear; the headline `blend_beats_best_source`
  may revert to `None` for a period while paired samples accumulate. **This
  is the honest state and should be presented as such**, not backfilled from
  the old unpaired figures.

---

## 7. Work plan

Seven workstreams. Each states its exit criteria; a workstream is not done
when the code works, it is done when the exit criteria are demonstrated.

### W0 — Measurement repair and prerequisites (no new capability)

**Why first:** handover §5 step 1 is right that measurement precedes
redesign, and **ARC-05** shows the instrument is not fit. Every week spent
waiting on the current instrument is a week of data that cannot answer the
question.

- Paired `blend_comparison` (§4.8), schema v4
- Retention: `purge_days = 0` on the reporting installation (handover §6.2)
  — an unbounded database is a risk to the measurement window itself
- Run-time attribution via the metadata API (§4.4), because it is cheap and
  everything downstream wants it

**Exit:** paired comparison recording on the live installation; sample
counts visible; retention bounded; `lead_time_basis` populated per source.
**No conclusion is drawn from the numbers at this stage** — the instrument
must run before it is read.

### W1 — Live verification (research; no production code)

Handover §3.2's four questions, plus three added by this analysis:

1. `minutely_15` at 47.5536 / 8.9120 for AROME HD — genuine output or
   interpolation
2. Variable coverage on HD vs the 2.5 km domain
3. `forecast_days=7` tail inspection — ARPEGE substitution (**ARC-09**)
4. Gust units on the client path (**ARC-11** — expected m/s, must be
   confirmed rather than assumed)
5. **New:** does AROME accept the full existing `HOURLY_VARIABLES` list in
   one request, or does it 400? (**ARC-07**)
6. **New:** does the metadata API expose `last_run_initialisation_time` for
   AROME? (§4.4 depends on it)
7. **New:** SRF's upstream model, via `srf_probe.py` (**ARC-02**) — and
   `UNKNOWN` is an acceptable, expected answer

**Exit:** every answer captured as a **real response fixture** committed to
the repo, per §7.3. No answer recorded from documentation.

### W2 — Source metadata layer (structure only, no behaviour change)

`sources.py`; derived views; reachability tests (**ARC-08**).

**Exit:** the full suite passes **unchanged** — no test output differs.
That is the criterion: a structural refactor that alters behaviour has
failed, and the suite is what says so.

### W3 — Correlation-aware fusion (the largest behaviour change)

Labelled contributions (**ARC-01**), strategy kinds (**ARC-06**),
family-normalised Class A weights, two-stage Class B consensus, Class C
provenance-aware voting (**ARC-03**).

**Exit:** for a recorded window of live data, before/after blend outputs
computed and diffed offline, with every material difference explained by a
named family rule. Unexplained differences block the release. Envelope
parameters must show **bit-identical** output.

### W4 — AROME integration

`MODEL_PARAM`, `SourceMeta`, per-source variable set, horizon guard.
Scoped to precipitation, gusts, CAPE.

**Exit:** AROME contributing; horizon guard demonstrated against a fixture
containing values past the cap; provenance correct; no ARPEGE row can exist
under an AROME label.

### W5 — Learning refinements

Continuous confidence (with the strict-generalisation test), then season
fallback (**ARC-10**). Finer lead buckets gated on W0 evidence.

**Exit:** step-function substitution reproduces v0.2.8 exactly; season
fallback demonstrated across a synthetic season boundary with zero in-season
samples.

### W6 — Release, audit, 1.0.0 assessment

Full ICS documentation set, release audit, zip. The 1.0.0 question is
**assessed, not answered**: v0.3 ships the instrument that can answer it,
and the answer needs a season of data. Declaring 1.0.0 in the same release
that first measures honestly would repeat the pattern §7.2 warns about.

### Dependencies

```
W0 ──┬─> W2 ──> W3 ──> W4 ──> W6
     │                  ↑
     └─> W1 ────────────┘
                W3 ──> W5 ──> W6
```

W1 runs concurrently with W0 and gates W4 only. W3 gates both W4 and W5.

### Release shape

Proposed: **v0.3.0 = W0 + W2**, **v0.3.1 = W3**, **v0.3.2 = W4**,
**v0.3.3 = W5**. Four small releases rather than one large one, on the
project's own evidence: five setup-blocking defects across v0.1.24–v0.1.28
were all introduced by fixes, and the larger the release, the harder the
attribution. Each ships a full ICS documentation set and zip.

---

## 8. Test strategy

The governing standard stays **"if I break the fix, does this test fail?"**
Every regression test is confirmed failing against the broken code before
the fix is restored.

**Reachability assertions to add** (the §7.1 defect class — implemented,
unit-tested, never reached):

- Every `ALL_FORECAST_SOURCES` member has `SourceMeta`, cadence, horizon,
  family, and a variable set
- Every `PARAMETERS` entry declares a strategy kind — no default
- Every `MODEL_PARAM` member's URL contains `wind_speed_unit=ms`
  (**ARC-11**, written over the map rather than a named source)
- Every source in the fusion path appears in the paired comparison
- `_TABLE_SQL` contains no `CREATE INDEX` — existing test, extended to v4

**Tests that must be written to fail for the right reason:**

- Continuous confidence: step-function substitution reproduces v0.2.8
  exactly. Fails if the generalisation is not strict.
- Family collapse: a synthetic 3-member family must not outvote two
  singletons under a consensus strategy — and **must** still win under an
  envelope strategy. The second assertion is the one that catches a
  uniform-rule mistake.
- Horizon guard: a fixture with values past the cap. Asserts rows are
  absent, not merely that a warning was logged.
- Paired comparison: a fixture where the blend is genuinely worse than its
  best source must report `blend_beats_best_source = false`. **The current
  instrument would not necessarily do so — that is the ARC-05 regression
  test, and it must be confirmed failing against v0.2.8's implementation
  before the new one is accepted.**
- Season fallback: zero in-season samples must yield the season-agnostic
  value; many must yield today's; the crossover must be monotone.

**Anti-patterns explicitly avoided**, from §7.2: no assertion satisfiable by
a crash (every "None" assertion pairs with a sample-count assertion); no
wall-clock thresholds (`test_blend_query_count.py` counts work done and is
the model to follow).

**Fixtures:** every new fixture captured from a real response (§7.3). W1
exists to produce them.

**Regression baseline:** the 692 existing tests pass at every workstream
boundary. W2 and W3 additionally require the offline before/after diff on
recorded live data.

---

## 9. ICS deliverables per release

Bundled in each GitHub zip, matching the v0.2.8 convention:

- `SwissWeather_Fusion_v0_3_Architecture_ICS.md` (this document, updated)
- `swissweather_fusion_v0.3.x_release_audit.md` — cumulative, including a
  "bugs introduced during this pass" section, which this project has never
  been able to leave empty and should not start pretending it can
- ICS quality bug/testing report per release
- Updated `DEVELOPER.md` — architecture notes and the honest gaps list
- Updated `README.md`, `manifest.json` version, `hacs.json`
- `LICENSES/wetter-alarm-MIT.txt` (unchanged, still required)
- Full `tests/` tree and `requirements-test.txt`
- Verification record: test count, pyflakes status, live-verification
  fixtures with capture dates

---

## 10. Explicitly out of scope for v0.3

| Item | Why |
| --- | --- |
| Hierarchical shrinkage across horizon × season × regime × parameter × source | Handover §4.3 — hard to get right, fails silently, and §4.5's season fallback delivers most of the value |
| Regime dimension | Multiplies an already-sparse grid before the existing sparsity is handled |
| Direct Météo-France WCS raster | ~720 requests per run; justified only if AROME wins decisively **and** needs fields Open-Meteo lacks (handover §3.3) |
| Precipitation reconciliation against CombiPrecip | Strongest v0.4 candidate (§4.6); changes the class taxonomy; out of a measurement-discipline release |
| `coordinator.py` split | Concurrent refactor would defeat regression attribution (§5) |
| Model B v1 / AROME `minutely_15` nowcast | Not on v0.3's critical path |
| Fog, Roundshot, MeteoNews, wetter.de, SRF warnings, meteoblue Warnings API | Closed in handover §6.3 — not revisited |
| MeteoSwiss official warnings | Closed, but flagged for periodic recheck ("not before end of 2026") |

---

## 11. Risk register

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Family assumption is wrong (SRF, meteoblue mLM) | Medium | Medium | `UNKNOWN` as singleton; structural not measured; documented as an assumption |
| Migration to v4 breaks upgrading installs | Medium | **High** | Full prior-schema migration test; every index in `_INDEX_SQL`; both rules have bitten before |
| `coordinator.py` change introduces a setup-blocking defect | Medium | **High** | Four small releases; reachability tests; construction tests (`test_v0_1_26_construction.py` pattern) |
| Open-Meteo changes AROME behaviour silently | Medium | Medium | Horizon guard is structural (**ARC-09**); fixtures from real responses |
| Blend gets *worse* under family weighting | **Medium** | Medium | This is a real possibility and must be allowed to be visible — W0's instrument exists to detect it, and reverting is an acceptable outcome |
| Autumn season boundary lands mid-release | **High** | Medium | It is imminent. W5's season fallback is the answer; until it ships, expect a cold-start dip and do not misread it as a regression |
| Measurement still inconclusive after a season | Medium | **High** | Report per-lead-offset rather than one boolean; a partial answer is still an answer |

---

## 12. Open questions for the maintainer

1. **The two input ICS documents** (§2.1) — supply them, or confirm this
   architecture supersedes them. If they contain design detail beyond the
   handover's summary, reconcile before W2.
2. **Release shape** — four small releases (§7) or one v0.3.0?
3. **Learned history across schema v4** — this document proposes preserving
   it (§4.7). v0.1.24's precedent was to discard. Confirm.
4. **Blend behaviour change** — family weighting changes every published
   value. Acceptable in a point release, or does it want a version signal?
5. **Precipitation-vs-radar reconciliation** (§4.6) — deferred here. Confirm
   that "AROME earns its place" may remain a judgement rather than a
   measurement for v0.3.
6. **Reporting installation retention** — W0 proposes setting `purge_days`
   from 0. Confirm the value (90-day default, or longer to protect the
   measurement window?).

---

## 13. Verification record for this document

- No code was written or modified. The v0.2.8 tree was extracted read-only
  and inspected.
- Every finding cites the file and function it was derived from. The three
  grep-verified absences (family/correlation handling, `SOURCE_BLEND` write
  sites, index placement) are marked as grep results.
- Bucket arithmetic recomputed independently: 4,320 Class A keys, matching
  the handover's "~4,300"; 576 blend keys; 11,520 under eight lead buckets.
- Test-function count (600 across 30 files) read from the tree; the
  handover's 692 passing tests reflects parametrisation and was not re-run —
  the suite was **not** executed during this pass.
- Claims that could not be verified from the source are marked as such:
  SRF's upstream model (**ARC-02**), AROME variable coverage (**ARC-07**),
  and whether Open-Meteo substitutes ARPEGE past AROME's horizon
  (**ARC-09**). All three are W1 items, and none is assumed in the design.
