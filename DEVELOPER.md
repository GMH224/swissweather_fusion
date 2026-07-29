# Developer notes: architecture rationale

## v0.1.7 — the blend crash that broke the whole card, plus a second blocking-I/O bug

Three confirmed bugs from direct log evidence (not hypotheses this time —
actual tracebacks and HA's own loop-blocking detector caught all three):

1. **The weather entity's persistent "Unavailable" state, root cause
   confirmed**: `model_a.blend()` crashed with `unsupported operand
   type(s) for *: 'NoneType' and 'float'` on every single refresh cycle
   since deployment (confirmed twice in the log, exactly 10 minutes apart
   — the blend coordinator's refresh interval). A source can legitimately
   return `null` for a given hour/measurement (Open-Meteo, SRF, and
   meteoblue can all do this), and that flows straight into
   `forecast_snapshots` as `None` — `blend()` never checked for it before
   doing arithmetic. Fixed: a `None` raw_value is now skipped, treated the
   same as "this source has nothing to say for this hour," not crashed
   on. This is the single highest-impact fix here, since it's why the
   card had never worked at all since the v0.1.5 rebuild.
2. **A second blocking-I/O bug, same class as the one fixed in weather.py
   for v0.1.5, different code**: HA's own loop-blocking detector caught
   the CombiPrecip client's file write (`open(..., "wb")`) and its
   temp-directory cleanup (`shutil.rmtree`'s `scandir` call) both
   happening directly inside the async coordinator method. h5py has no
   async support regardless, so the fix mirrors the weather.py rebuild:
   split into `async_fetch_latest_bytes()` (async STAC query + HTTP
   download, genuinely non-blocking via aiohttp) and
   `write_temp_and_extract()` (a plain synchronous method — temp dir,
   file write, h5py parse, cleanup, all in one place — called via
   `hass.async_add_executor_job()` by the coordinator, never awaited
   directly).
3. **SRF's diagnostic log truncation raised from 500 to 4000 characters.**
   The last capture showed the real response is dominated by verbose
   location metadata (station_id, alarm_region_name, district,
   geolocation_names...) before ever reaching whatever field holds the
   actual forecast data — the 500-character limit was entirely consumed
   by that metadata. This doesn't fix SRF's still-zero-data-points issue
   by itself, but the next diagnostic capture should actually show enough
   of the response to work with, rather than being cut off before the
   useful part.

**An open question, not yet resolved**: a later status check showed every
single source's `last_success` frozen at the same startup moment for
roughly 2.5 hours straight — not just SRF, all seven sources
simultaneously. That's a broader symptom than any of the three bugs above
individually explain. The working hypothesis is that the blend
coordinator crashing every 10 minutes, uninterrupted, for that whole
window may have cascaded into something affecting the other coordinators
too (resource buildup, event loop congestion) — but this is explicitly
unconfirmed without a log covering that specific window. Fixing bug #1
may resolve this as a side effect, or it may not — worth checking a fresh
log after this deploy specifically for whether all sources resume normal
independent update cadences, not just whether SRF's crash is gone.

**Update, from a fuller log capture**: this hypothesis turned out to be
wrong, and worth saying so plainly. The same fuller log showed the blend
coordinator crashing at exactly 10-minute intervals continuously for the
entire 2.5-hour window (16 crashes, no drift, no stopping) — proof that
HA's own coordinator scheduling was never actually disrupted by the
repeated failures. The far more likely explanation for the earlier
"everything frozen" screenshots is a stale cached view in the HA app's
more-info dialog (a real, common frontend behavior), not an actual
backend freeze — none of the other coordinators showed any repeated
errors over that same window, which is consistent with them working
correctly and simply not logging anything (successful coordinator runs
are silent by design).

## v0.1.7 addendum — the bias-learning step never actually existed

A direct question ("what's still broken?") led to checking something that
turned out to be a bigger gap than any bug fixed above: **nothing in
production code ever called `update_bucket_ema` or `upsert_bucket_stats`.**
Both existed and were unit-tested in isolation since early in this
project, but no coordinator ever invoked them. Practically, this meant
`bucket_stats` would have stayed empty forever — not just during a
cold-start window — meaning Model A's blend was only ever an unweighted
average of raw forecasts, never applying the learned bias correction
that's the actual point of the project. `expert_weight_*` sensors and
`last_learning_a` would have shown "Unknown"/stub values permanently, no
matter how long the integration ran.

**Fixed with a new coordinator, `ModelALearningCoordinator`**, which every
20 minutes:
1. Queries `forecast_snapshots` for rows whose `valid_at` has now passed
   (restricted to `temperature`/`humidity`/`pressure` — `precip` and
   `wind_speed` have no ground truth yet, since the local station has no
   rain/wind sensors) and that fall after a stored watermark (reusing the
   `schema_meta` table rather than a new one — a single small value isn't
   worth a dedicated table).
2. Fetches the local station's actual readings in that same window in one
   query (not one query per forecast row).
3. For each due forecast row, finds the nearest station reading within
   30 minutes (`find_nearest_observation`, a new pure function), and if
   one exists, computes the bucket key and folds the (forecast, actual)
   pair into `bucket_stats` via the existing, already-tested
   `update_bucket_ema`.
4. Advances the watermark, so the same row is never reconciled twice.

Deliberately processes every individual forecast_snapshots row rather than
deduplicating by (source, valid_at) — a source can have several snapshots
for the same valid_at from different issued_at times (different lead
times as a forecast run gets closer to the target hour), and each is a
genuinely separate, separately-informative data point for its own
lead_time_bucket, not a duplicate to collapse.

`LastLearningASensor` now reports this coordinator's real last-run
timestamp (plus how many rows it reconciled last time) instead of the
permanent `None` stub it was before. `LastLearningBSensor` remains a
stub — that one's still correct, since Model B v1 training genuinely
doesn't exist yet (see the v0 -> v1 upgrade path discussed earlier in
this document).

Tested at three levels: the new pure function (`find_nearest_observation`)
in isolation, the new DB queries in isolation, and — given how much this
depends on several pieces working correctly together — a dedicated
end-to-end test file (`test_learning_integration.py`) that replicates the
coordinator's exact logic against a real database, confirming a forecast
plus a matching station reading genuinely produces a real `bucket_stats`
row via the full flow, not just when the individual functions are called
directly.

## v0.1.6 — SRF going silent (hypothesis, not confirmed), and a real timezone bug

Two issues found from a status check before v0.1.5 was even deployed —
both from the still-running v0.1.4 instance, several hours after the SRF
fix in that version had been confirmed working:

**meteoblue's schedule was checked against UTC, not actual local time —
confirmed, not a hypothesis.** In summer (CEST = UTC+2) this meant
meteoblue was really polling at 14:00/18:00/22:00 local instead of the
intended 12:00/16:00/20:00 — a genuine 2-hour offset. Fixed by using
Home Assistant's own configured-timezone `now()` helper
(`homeassistant.util.dt.now()`) instead of hardcoded UTC.

**SRF's polling appeared to stop entirely, silently, for about 8 hours.**
`last success` was frozen at an old timestamp while every other source
(CH1/CH2/D2/CombiPrecip) showed fresh ones — but `consecutive_failures`
was also stuck at 0, meaning it wasn't failing loudly either, just not
running. At a 45-minute poll interval, roughly 10 attempts should have
happened in that gap. The most coherent explanation for *all* of these
symptoms together (frozen success, zero recorded failures, a notably
slow 2.7s successful call earlier) is a network call hanging indefinitely
rather than erroring — none of the SRF client's three HTTP calls (token
exchange, geolocation lookup, forecast fetch) had an explicit timeout, so
a stalled connection would leave the coordinator waiting forever instead
of raising something catchable.

**This is a reasoned hypothesis, explicitly not confirmed the way the
URL/shape bugs were** — there was no log evidence of a hang, only the
absence of any evidence of anything at all, which is itself the signature
an indefinite hang would produce. Fixed defensively either way, since an
HTTP call with no timeout is worth bounding regardless of whether it's
the exact cause here:
- Explicit `aiohttp.ClientTimeout(total=30)` added to all three of the
  client's HTTP calls.
- A second, coordinator-level `asyncio.timeout(60)` backstop around the
  whole fetch, in case a hang happens somewhere other than those three
  calls specifically (e.g. during the token-cache check).

If SRF goes silent again after this, that will be real evidence the
timeout hypothesis was wrong and something else is going on — worth
explicitly watching for on the next check rather than assuming this is
closed.

**Also added (no version bump — test/robustness work, not a behavior
change): explicit DST transition coverage.** Requested directly: verify
neither a winter→summer nor summer→winter transition can crash the
integration or corrupt learning data (a gap in updates during either is
explicitly acceptable). Working through where this could actually matter:
Model A's bucket keys and every stored timestamp are UTC-only by
construction — UTC has no skipped or repeated hours, so there's nothing
for DST to corrupt there, and a test now proves that directly (checking
bucket derivation and storage/purge ordering across both 2026 transition
instants) rather than leaving it as an unverified assumption. The one
place genuine local-time logic exists is meteoblue's scheduling guard —
extracted into a pure `should_fire_scheduled_call` function (behavior-
preserving refactor, same logic the coordinator used to run inline) so
both the spring-forward gap and the fall-back repeated hour could be
exercised directly. Neither crashes; a repeated local hour is treated as
"already handled" rather than firing twice, which is an accepted trade-off
consistent with the wider project's "gaps are fine, corruption is not"
tolerance, not something engineered around further.

## v0.1.5 — real forecast, wind speed, a genuine architecture fix, and a caught unit bug

Prompted by direct comparison against another integration's weather card:
ours was missing wind speed (data already flowing through Model A's blend
but never actually exposed) and the entire forecast section (removed
outright in v0.1.2 rather than ship a never-resolving spinner). Both
addressed properly rather than patched:

- **`ModelABlendCoordinator`** (new) computes current values plus a real
  168-hour (7-day) forecast in one batched executor job per 10-minute
  refresh. This also fixes a real architectural bug: the weather entity
  used to query the database directly and synchronously inside its
  properties — every other part of this project routed DB access through
  an executor job except this one had been doing blocking sqlite3 calls
  on the event loop on every state read.
- **Daily and twice-daily forecasts** are pure reshapes of the same
  hourly data (`models/model_a.py`: `aggregate_daily_forecast`,
  `aggregate_twice_daily_forecast`) — no extra database access, and both
  now carry total precipitation in mm, not just the hourly rate, per a
  direct request for that to be available at all three granularities.
  A tautological bug in the twice-daily night-period boundary logic (both
  branches of a conditional returned the same value) was caught by its
  own regression test before ever shipping — worth noting since it's
  exactly the kind of bug a quick correctness pass would have missed
  without a test specifically targeting the early-morning boundary case.
- **Wind speed unit mismatch, caught proactively while wiring this up**:
  Open-Meteo defaults wind speed to km/h; meteoblue's confirmed test
  response used values consistent with m/s. This had been silently
  flowing into Model A's blend unused since wind speed was never actually
  displayed — the moment it became visible on the card, the mismatch
  would have shown up as a visibly wrong number. Fixed by explicitly
  requesting m/s from Open-Meteo to match, same class of fix as the
  earlier surface-vs-sea-level pressure bug.
- Known simplification carried into this version: daily/twice-daily
  grouping uses UTC calendar-day boundaries, not the configured local
  timezone — hours near midnight can land in the "wrong" local day.
  Threading the HA-configured timezone through is a reasonable follow-up,
  not done here since it wasn't the immediate priority.

## v0.1.4 — SRF's third distinct failure, and a change in approach

After the URL fix in v0.1.3, SRF got past the 404 and reached real parsing
code for what appears to be the first time — and hit a third distinct
error: `'str' object has no attribute 'get'`. Given this is now three
separate SRF-specific surprises (dict-vs-list in v0.1.1, URL structure in
v0.1.3, and now this), guessing a fourth exact response shape from
documentation alone stopped being the right strategy. The approach
changed instead of just patching the specific symptom again:

- **Both parsers (`parse_geolocation_response`, `parse_forecast_response`)
  now defend against every level being a different shape than
  expected** — the top-level payload being a bare string instead of a
  list/dict, a "results" field not actually being a list, and individual
  list entries being plain strings rather than objects. Anything that
  doesn't fit is skipped rather than crashed on.
- **The client now logs a truncated repr of the raw response whenever
  parsing yields nothing usable.** Defensive parsing that silently
  returns an empty result is a regression in one way — a genuine ongoing
  problem could hide behind "no error, just no data" instead of a loud
  crash. The logging closes that gap: if there's still a mismatch, the
  next log capture shows the actual structure instead of requiring
  another screenshot-and-guess round.

This is a deliberate shift from "fix the specific shape" to "stop
assuming a fixed shape at all, and make sure we can see what's really
there if it's wrong again" — appropriate once the same integration point
has surprised us three times running.

## v0.1.3 — SRF's real root cause, found from a live 404, plus an optional Open-Meteo key

The v0.1.1 defensive fix (handling both list and dict-wrapped response
shapes) turned out to have fixed a real problem, but not the one still
causing failures — the geolocation lookup was actually succeeding all
along, returning a valid ID formatted as `lat,lon`. The actual bug,
confirmed from a live 404 in production logs plus the official SRG-SSR
docs and a real working third-party example hitting the same API:

- The forecast endpoint takes the geolocation ID as a **path parameter**
  (`/forecast/{geolocationId}`), not a query parameter
  (`?geolocationId=...`) as originally built.
- There is **no `/v2/` segment in the actual URL path** at all — "V2"
  refers to the product/subscription tier chosen on the developer portal,
  not a URL versioning scheme. Both the geolocation and forecast URLs had
  this incorrectly included.

Both fixed. This is the second and hopefully last SRF-specific bug —
between this and v0.1.1's fix, both halves of the request/response cycle
(what we send, how we parse what comes back) have now been corrected
against real evidence rather than documentation alone.

**Also added**: an optional Open-Meteo API key (config flow + options
flow). Confirmed from their own docs that using a key requires switching
to a `customer-` prefixed hostname, not just adding a parameter — worth
knowing this is their paid/commercial tier, not a free bonus, and it
raises rate limits/reliability rather than making CH1/CH2/D2 refresh more
often (that's fixed by MeteoSwiss/DWD's own model schedule regardless of
tier). While adding this, found and fixed a smaller gap from v0.1.2: the
SRF/meteoblue/Meteonomiqs credential fields added to the options flow
never got translation labels, so they were showing as raw config keys
(`srf_consumer_key`) instead of readable text — fixed alongside the new
field.

## v0.1.2 — second deployment round, five more real bugs

1. **Surface pressure vs. sea-level pressure mixed across sources.** CH1/
   CH2/D2 requested `surface_pressure` (pressure at each source's own grid
   elevation); SRF, meteoblue, and the local station all report sea-level-
   adjusted pressure. These are different physical quantities, differing
   by roughly 12 hPa per 100m of elevation — blending them produced a
   suspiciously low 966.2 hPa reading in the second deployment, which
   matches uncorrected surface pressure at a few hundred meters' elevation
   almost exactly. Fixed by requesting `pressure_msl` instead.
2. **No device grouping.** No entity set `device_info`, so all 42
   entities showed as an ungrouped flat list under the integration rather
   than a nested device card — visibly different from how a well-behaved
   integration (e.g. weather-fusion-ai, installed alongside this one)
   presents itself. Added a shared `device_info` (`device.py`) so every
   entity groups under one card.
3. **The weather card's "Forecast:" section spun forever.**
   `WeatherEntityFeature.FORECAST_HOURLY` was declared with no real data
   behind it — `async_forecast_hourly` always returned `[]`. The frontend
   kept waiting for hourly data that was never coming, rather than being
   told there simply wasn't any yet. Removed the feature declaration
   until a genuine multi-hour forecast exists.
4. **No way to view or change credentials after initial setup.** The
   options flow only ever exposed the three station-sensor fields and the
   purge-days setting — SRF/meteoblue/Meteonomiqs credentials were never
   in it at all. Added them as optional masked fields (blank = keep the
   existing value, since a masked secret can't be shown for editing the
   way a plain setting can).
5. **Two bugs that would have silently defeated fix #4 on its own**:
   credentials were read only from `entry.data` in `__init__.py`, never
   from `entry.options` — so even with the fields added to the options
   flow, saving them would have had no actual effect. And there was no
   update listener at all, so *any* options change (station sensors,
   purge days, now credentials) would sit unused until a manual restart.
   Both fixed: credentials now check `entry.options` first (matching the
   pattern already used for station sensors), and an update listener
   triggers a full reload whenever options are saved.

## Architecture rationale (v0.1 design)

## v0.1.1 — first deployment, four real bugs found and fixed

The first actual HA deployment surfaced four issues, none of which showed
up in the unit test suite — a useful reminder of exactly the limitation
flagged in "Testing philosophy" below: the pure logic was verified, the
HA-integration layer and the live third-party APIs were not, until now.

1. **CH1/CH2 requests all failed with 400 Bad Request.** The Open-Meteo
   model identifiers (`icon_ch1_eps`, `icon_ch2_eps`) were invented
   plausible-looking names, never actually checked against Open-Meteo's
   docs. The real values are `meteoswiss_icon_ch1` / `meteoswiss_icon_ch2`.
   ICON-D2's identifier (`dwd_icon_d2`) happened to already be correct.
   Fixed, and the client now also surfaces Open-Meteo's actual JSON error
   `reason` field on a 400 instead of a bare status code, so a mistake
   like this is immediately diagnosable next time rather than requiring a
   documentation re-check.
2. **SRF crashed with `'list' object has no attribute 'get'`.** The
   geolocation and forecast response parsers assumed a dict-wrapped shape
   that was never verified against a live call (flagged as an outstanding
   item since planning). The actual shape is very likely a bare JSON array
   at the top level. Both parsers now handle either shape.
3. **The whole integration reported "failed setup, will retry" because of
   bug #2 alone.** `__init__.py` ran each coordinator's first refresh in a
   plain sequential loop — one coordinator raising (SRF) propagated all
   the way up and failed `async_setup_entry` entirely, taking down
   station/meteoblue/CombiPrecip/Meteonomiqs too, even though those would
   have worked. This is exactly the opposite of the graceful-degradation
   principle the whole project was designed around, just never actually
   implemented at the setup level. Each coordinator's first refresh is now
   isolated in its own try/except; a failure logs a clear warning and
   setup continues with everything else.
4. **The pressure entity selector silently excluded valid sensors.** The
   config flow filtered on `device_class="pressure"` only. Real-world
   integrations like Netatmo use the newer, more specific
   `atmospheric_pressure` class instead — both describe the same kind of
   sensor, just under HA's older vs. newer taxonomy. The selector now
   accepts either. Temperature and humidity were checked too:
   temperature has no equivalent split, and humidity's second class
   (`absolute_humidity`) measures a different physical quantity (g/m³, not
   %) — adding it would have introduced a bug, not fixed one, so those two
   were deliberately left alone.

## Architecture rationale (v0.1 design)

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
