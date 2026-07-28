# Developer notes: architecture rationale

This document is the "why" behind decisions referenced throughout the
code's docstrings. It exists because a design this opinionated is easy to
accidentally un-fix during future changes if the reasoning isn't written
down somewhere durable.

## The two-model split, and what it isn't

Model A and Model B are split by **problem type**, not by time horizon.
Model A corrects the bias of every forecast source at every lead time it
provides — a source's next-hour value gets the same treatment as its
five-day-out value. Model B never looks at any forecast source at all; it
only watches the local station's live trend plus a live radar reading, and
answers one narrow question: is a storm starting in the next 15-60
minutes. The two differ in *mechanism* (a running average vs. a scored
rule/classifier), not in which time range they're responsible for.

## Why EMA, not gradient boosting, for Model A

This project deliberately does not run real numerical weather prediction —
it can't, and doesn't try to. What it does is Model Output Statistics: the
same technique national weather services have used for decades to
correct a coarse grid model against one specific point. That's a slowly
drifting, low-data, streaming estimation problem, and a bucketed
exponential moving average is the right-sized tool for it — not a
limitation being worked around. Gradient boosting was seriously considered
and is exactly the right tool for a different part of the system (Model
B's future v1, once real training data exists) — see below.

## Two real bugs found during design, and how they were fixed

**The lead-time bucket bug.** The original bucket key was
`(hour_of_day, season, source, measurement)` — no lead-time dimension.
That meant a source's highly-accurate near-term forecast and its much
noisier far-out forecast for the same hour-of-day got folded into one
shared bias estimate, diluting exactly the short-range accuracy that
matters most. Fixed by adding `lead_time_bucket` (`short <24h`,
`medium 24-72h`, `long >72h`) to the key, with EMA responsiveness (alpha)
also varying by lead-time bucket — short buckets adapt fast since recent
regime changes matter there, long buckets smooth heavily since their data
is sparser and noisier per bucket.

**The missing residual-error field.** The schema originally stored
`ema_bias` and `ema_weight` side by side as if independently maintained,
without ever specifying what weight was a running average *of*. Bias
(systematic offset) and weight (how much to trust a source) are different
statistics — a source can be unbiased but noisy, or biased but very
predictable. Fixed by adding `ema_abs_error` (mean absolute error *after*
debiasing), which is what weight is actually derived from
(`1/(ema_abs_error + ε)`).

Both fixes apply to every source with a multi-day horizon, not just the
one that happened to surface them.

## Source history: what was tried, kept, and dropped

- **INCA (MeteoSwiss nowcast)**: originally the plan's centerpiece for
  short-range, radar-informed nowcasting and the sole cross-model trigger
  source. Dropped entirely after the actual MeteoSwiss offer turned out to
  be $250 and SFTP delivery — not free, not self-serve, not an API. Same
  principle that ruled out the commercial provider Meteomatics earlier:
  paid + manual-delivery data doesn't fit this project.
- **DWD ICON-D2** (via Open-Meteo, `models=dwd_icon_d2`) replaced INCA's
  role as a Model A blend expert. Confirmed via geodesic distance
  checking that the deployment location sits comfortably within its
  domain (not near a boundary). **Important correction made during
  design**: ICON-D2 only reruns every 3 hours at the source (00/03/06/09/
  12/15/18/21 UTC), same as CH1 — the "15-minute" marketing refers to
  output time-step granularity *within* a run, not how often the model
  actually recomputes. So it does NOT solve the "genuinely fresher data on
  an early poll" problem INCA solved, and is deliberately excluded from
  the cross-model trigger for the same reason CH1/CH2 are.
- **MeteoSwiss CombiPrecip** (radar precipitation, self-serve STAC,
  5-minute native refresh) replaced INCA's role as the fast, radar-based
  precipitation signal — but reframed as a **Model B feature polled
  continuously**, not a Model A blend member and not something to
  "trigger" (it already refreshes fast on its own; there's nothing to
  trigger). This was originally out of scope entirely ("INCA covers this
  need") — that justification stopped applying once INCA was dropped.
- **Meteonomiqs (wetter.com PWA v4.0)**: added for its nowcast endpoint
  (an independent, differently-sourced radar-derived precipitation risk
  scale) and hourly pressure/precipitation forecasts. **CAPE was initially
  thought resolved via this source but is not** — the endpoint that
  exposes it (`/forecast2`) turned out to be a paid tier not included in
  the actual API key obtained; see below for the full correction. Budget-
  constrained to 1000 calls/year, which shaped its whole usage pattern
  (below).
- **SRF (SRG SSR)** and **meteoblue** remain as independent Model A
  experts, chosen specifically because they're genuinely separate
  forecasting operations from MeteoSwiss's own pipeline, not re-badged
  copies of it.

## Upwind radar sampling (Model B)

CombiPrecip's HDF5 grid, once downloaded for the local pixel, costs
nothing extra to sample at additional points — the expensive part
(download + parse) happens once regardless of how many pixels are read
from the result. This is why Model B samples 4 points, not 1: the local
location, plus three points along a fixed bearing (225°/southwest by
default) at 30/45/70 km, corresponding to roughly 20/35/60 minutes of lead
time at typical storm-cell propagation speed. The nearest point with a
detected precipitation signal wins (highest probability), the local point
(precipitation already arrived) wins over all of them.

**Known simplification**: the bearing is fixed, not dynamically
recomputed from actual observed or forecast wind direction. A real storm
doesn't always approach from the same direction. Upgrading to
dynamically-oriented sampling is a reasonable v0.2 enhancement — it needs
wind direction data the local station doesn't have yet (rain/wind sensors
are planned but not yet integrated) or would need to come from a forecast
source's wind direction field, neither of which was wired up for v0.1.

## Model B: v0 today, v1 once data exists

v0 is `score_v0_graduated` — the higher of two independent hand-crafted
signals (station tendency and radar-distance detection), not a strict sum
(so two signals agreeing doesn't inflate the score past what either alone
would justify). This is deliberately interpretable and requires zero
training data, which matters because v1 genuinely cannot exist yet: a
trained classifier needs a real storm season of `storm_events` +
`storm_predictions` to learn from, and severe convective events are rare
enough (dozens/season) that this will take real calendar time to
accumulate, not an engineering effort to speed up.

Once that data exists, the richer feature set this project ended up
with — multi-point radar detections at different lead times, station
tendency, meteoblue's/CH1's own predictability signals, and an independent
Meteonomiqs confirmation — is specifically well-suited to tabular gradient
boosting (XGBoost/LightGBM). CAPE remains unavailable (see the Meteonomiqs
correction below) rather than part of this list — worth adding if a future
source or tier upgrade makes a real convective-instability index
accessible. These models are good at exactly this kind of multi-signal,
engineered-feature problem, train in seconds even on modest hardware at
this data scale, and don't need a GPU despite the temptation to reach for
one — the honest bottleneck for v1 is calendar time waiting for storm
events, not compute.

**Deliberate scope boundary, not an oversight**: Model B (in both v0 and
any future v1 trained the same way) is tuned for the summer convective
signature — a sharp pressure "nose" plus a humidity jump. A winter
frontal-passage/icing situation has a different, more gradual pressure
signature that this rule was never built to recognize. Building a second
classifier for winter risk was considered and deliberately not pursued —
see the project's own "diminishing returns" conclusion. meteoblue's
seasonal polling schedule (below) is the entire mechanism for winter
relevance; there's no trigger-based supplement behind it the way there is
in summer.

## The cross-model trigger

When Model B's probability crosses upward through a threshold (fires once
on the crossing, not continuously while elevated — this is what makes an
event-based allowance actually mean "per event"), it requests an
out-of-cycle poll from sources that can actually benefit from one:

- **meteoblue**: one bonus call allowed per storm scenario, overriding its
  scheduled polling window — a real detected signal outweighs whichever
  scheduling rationale (climatology in summer, commute-timing in winter)
  justified the routine schedule that day.
- **Meteonomiqs**: same idea, budget-permitting (see below).
- **CH1/CH2/ICON-D2 are excluded** — none of them refresh independently of
  their own fixed run schedule, so an early poll just re-fetches the
  identical previous run for a wasted call.
- **SRF was considered and dropped** from the trigger list to keep this
  simple, despite having genuine budget headroom — CombiPrecip's
  continuous polling was judged the better fit for filling INCA's old
  role, and SRF's own 45-minute schedule was left as-is.

## Why Meteonomiqs needs a daily heartbeat

MeteoSwiss/Meteonomiqs communicated that the API key is revoked after
roughly 30 days of inactivity. This turned "call it only when needed" into
a real operational requirement: at least one call happens every calendar
day unconditionally (checked well inside the 30-day window, for safety
margin). Both the daily heartbeat and the event-triggered bonus calls draw
from the same 1000-calls/year pool; the heartbeat is never skipped just
because bonus calls used budget elsewhere that day, since losing API
access entirely is a worse outcome than a tighter annual budget.

**Which endpoint the heartbeat uses is seasonal, and it's budget-neutral,
not an added cost.** During Mar-Oct (the same storm-season window already
established for meteoblue's schedule — reused deliberately rather than
introducing a third date range to track), the daily call happens at local
noon against `/forecast/hourly` (the plain, non-premium endpoint), which
returns mean sea-level pressure and precipitation sum/probability in one
response — genuinely useful hourly data for Model B, not just a ping to
keep the key alive. Any successful call, regardless of which endpoint,
satisfies the same keep-alive requirement, so swapping nowcast for this
richer call on a given day costs nothing extra against the annual budget.
Outside that window, or if noon hasn't arrived yet on a given check (the
coordinator polls every 6h), the lighter nowcast call is used as the
fallback, prioritizing "never miss a day" over "always hit noon exactly".

**Correction made during development, worth stating plainly rather than
quietly fixing**: this was originally built against `/forecast2`, which
also returns a CAPE index — resolving what had been an open question for
Model B since it was first designed. It turned out `/forecast2` is
explicitly a paid tier ("Premium forecast data" per Meteonomiqs's own
endpoint-group table) not included in the actual API key obtained for
this project. This should have been caught before building against it —
the same document that got fetched and quoted while designing this
literally said "Premium" in the table, and it wasn't flagged as a tier
boundary to verify, despite this project otherwise being careful about
exactly that class of mistake (the INCA $250/SFTP surprise being the
clearest earlier example). The fix: switched to `/forecast/hourly` (plain
tier), which still has pressure and precipitation, just not CAPE — CAPE
goes back to being unresolved for Model B, same as before Meteonomiqs was
introduced.

**What's deliberately not done yet**: `/forecast/hourly`'s pressure and
precipitation values are a genuinely different kind of signal than
everything else feeding Model B — they're an independent model's
*forecast* for the next few hours, not a real-time observation like the
station stream or CombiPrecip. That's potentially valuable (an independent
confirmation that pressure is expected to keep falling, not just observed
to have fallen), but it also goes stale within the same day it's fetched,
unlike the continuously-refreshed signals elsewhere in Model B. Designing
exactly how a once-daily forecast should decay in relevance over the
following hours needs real thought, not a rushed addition — the noon
call's data is captured and available, but folding it into
`score_v0_graduated` is left for a deliberate follow-up rather than done
half-considered here.

## Storage

A single SQLite file, deliberately separate from Home Assistant's own
recorder database — this integration owns its schema and its migrations,
and never depends on or interferes with HA's. WAL journal mode and
`synchronous=NORMAL` are used for the same reason HA's own recorder uses
them: kinder to typical SD-card/VM storage under frequent small writes
than the default rollback-journal mode. All access from the HA event loop
goes through `hass.async_add_executor_job()`, since `sqlite3` is a
blocking library — `storage/db.py` itself is plain, framework-independent
Python specifically so it can be unit-tested directly, without mocking an
event loop.

The purge policy (configurable "days to keep", 0 = forever) only ever
touches the high-volume, timestamp-driven tables: `station_observations`,
`forecast_snapshots`, `radar_observations`, `storm_predictions`.
`bucket_stats` stays small permanently by design (fixed number of
buckets). `storm_events` is Model B's entire ground-truth training set and
is never auto-purged.

## Testing philosophy — what's actually verified

Every pure-function module (`models/`, `clients/`, `storage/db.py`) has a
real, running pytest suite exercising its logic directly, including
against synthetic data structured to match real documented API response
shapes. What this test suite does **not** verify: `config_flow.py`,
`coordinator.py`, `weather.py`, `sensor.py`, `binary_sensor.py`, and
`__init__.py` all import Home Assistant directly, which wasn't installed
in the environment that built this — they're confirmed syntactically
valid (`ast.parse` succeeds) but not functionally exercised. Running this
inside an actual Home Assistant development environment (or with the
`pytest-homeassistant-custom-component` plugin) is the natural next step
before relying on this in production, and is likely to surface real
integration issues no amount of syntax-checking can catch.

Similarly, `clients/combiprecip.py`'s HDF5 parsing is built against the
documented ODIM_H5 standard (the format MeteoSwiss's radar network
participates in via EUMETNET OPERA), not a downloaded real file — there
was no network access to `data.geo.admin.ch` available while building
this. The coordinate transform (WGS84 → LV95) is standard and verified;
the specific HDF5 group/dataset names and gain/offset/nodata handling are
a documented best-effort starting point, flagged clearly for verification
against a real file early in actual deployment.

## A note on confidentiality

No real deployment coordinates, elevation, or location name appear
anywhere in this codebase, its tests, or its documentation — all examples
use clearly generic placeholder coordinates. Configuration is entirely
runtime-driven (via Home Assistant's config flow), never hardcoded.

## Per-source diagnostics (health.py)

Added after a direct question about whether an expired API key would be
easy to identify — the honest answer at the time was "partially": the
aggregate `binary_sensor.*_degraded` genuinely worked (it's backed by
each coordinator's real `last_update_success`), but the per-source
breakdown was stubbed. `health.py` closes that gap: each of the 5 vendor
integrations (Open-Meteo — tracked per-model, since CH1/CH2/D2 can fail
independently of each other — SRF, meteoblue, CombiPrecip, Meteonomiqs)
now has a `SourceHealth` instance tracking last success time, last poll
duration, and — the specific scenario that prompted this — **data errors
and auth errors as distinct categories**. An expired SRF credential shows
up as `sensor.*_srf_last_auth_error`, separate from a generic timeout,
specifically because the fix differs: an auth error needs the reauth
flow, a data error just needs the normal retry/cooldown to run its
course. `classify_exception` is a pure, dependency-free function (checks
for a `.status` attribute rather than importing aiohttp's exception
types), so it's directly unit-tested without needing a live failure to
trigger it.

## Known v0.1 gaps (the honest list)

- `forecast_accuracy` and `last_learning_a/b` are still wired as stubs
  returning `None` rather than fabricated placeholder numbers — computing
  a true rolling MAE needs a join between `forecast_snapshots` and
  `station_observations` by `valid_at`, meaningful work best validated
  against real data rather than built speculatively.
- `weather.async_forecast_hourly` returns an empty list — a full
  multi-hour blended forecast (not just "now") is the natural next
  iteration once the core loop is proven.
- The elevation lapse-rate pre-correction (`models/model_a.py`) is
  implemented but not yet wired into the live blend in `weather.py` — it
  needs each source's own grid elevation (`HSURF` for CH1/CH2/D2,
  `height` for meteoblue) threaded through from the forecast responses,
  which wasn't completed for v0.1.
- Dynamic (wind-direction-based) upwind sampling, wind-direction and
  cloud-cover EMA bucket dimensions, and Model B's winter/frontal
  classifier are all deliberately deferred — see the relevant sections
  above for why each is a "later" decision rather than a gap.
