# Developer notes: architecture rationale

## v0.1.23 — Remediation of the independent ICS code/system audit

An independent code/system audit (`swissweather-fusion-v0_1_22.zip`,
dated 11 August 2026, scope: Model A learning, SRF/SRG forecasting,
persistence, restart/recovery, quotas, deduplication, lifecycle) found
12 defects (L-01 through L-12), plus 4 more found in a follow-up
internal review of areas the audit didn't cover. The full audit findings,
fix descriptions, and verification approach for every one of them are in
`swissweather_fusion_v0.1.23_remediation_audit.md`. Summary here; that
document is the authoritative record.

**The headline defect (L-01, CRITICAL)**: the retry-watermark design
could re-select and re-fold already-reconciled forecast rows into
`bucket_stats` during any station-data gap, silently corrupting every
learned source weight, including SRF's. Its mirror-image (L-02, HIGH): a
late-arriving forecast row could land after the watermark had already
passed its `valid_at`, making it permanently unreachable. Both are fixed
by the same change: `forecast_snapshots` gained a per-row
`reconciliation_status` ('pending'/'reconciled'/'skipped') via a real
schema migration (v1→v2), replacing the single global watermark
entirely. A row transitions out of 'pending' exactly once, ever — which
makes both L-01's double-counting and L-02's permanent loss structurally
impossible, not just less likely. See `storage/db.py`'s
`get_pending_forecast_snapshots()`/`mark_forecast_snapshots_status()`
and `coordinator.py`'s rewritten `ModelALearningCoordinator._reconcile`.

**The migration itself also wipes `bucket_stats`.** The audit's own
verdict was explicit: persisted source weights are "NOT READY... until
the learning identity/reconciliation layer and EMA ordering are
corrected." There's no way to retroactively separate genuine samples
from L-01's duplicated ones in existing bucket_stats rows after the
fact, so starting clean on upgrade is the only way to make that
persistence layer trustworthy again — see `_migrate_to_v2()`'s
docstring. Rows older than 14 days (`MIGRATION_REOPEN_WINDOW`, same
value as the project's own `INITIAL_LOOKBACK`) are marked already-
`reconciled` so the first post-upgrade run doesn't try to reprocess
years of history; anything more recent comes back as `pending` and gets
a fresh, correct pass.

**L-03 (HIGH, EMA self-fitting)**: `update_bucket_ema()` computed the
new bias first, then measured `ema_abs_error` against that *same*,
already-updated bias — so the current observation partially fit itself
before it ever contributed to the error statistic, making
`ema_weight = 1 / (ema_abs_error + epsilon)` systematically too
generous. Worst case (cold start, `previous_sample_count == 0`): the old
code was mathematically guaranteed to produce `ema_abs_error == 0.0` for
every bucket's first sample, every time (`debiased_forecast =
forecast_value - new_bias` collapses to exactly `actual_value` when
`new_bias == raw_error`). Fixed by judging the residual against
`previous_bias` — the bias as it stood *before* this sample — before
updating it. Standard predict-then-update discipline; see
`test_update_bucket_ema_cold_start` and
`test_update_bucket_ema_judges_second_sample_against_bias_before_this_update`
in `tests/test_model_a.py` for both the fixed numeric behavior and a
direct check that it's NOT the old formula.

**L-04/L-05/L-06 (HIGH, no durable provider-run identity)**: three
separate symptoms — SRF's `forecast_snapshots` rows carry no run
identity at all, Meteoblue had no dedup mechanism whatsoever, and
Open-Meteo's dedup fingerprint lived only in a coordinator instance
dict, reset on every restart — turned out to be the same missing piece.
New shared `fingerprint.py` module: `fingerprint_points()` hashes a
provider's already-*parsed* points (variable, valid_at, value) rather
than raw response bytes, deliberately robust to a provider's internal
metadata shape changing without the forecast itself changing (SRF's
response shape in particular has already proven to be a moving target —
see the v0.1.18/v0.1.21 entries below). The fingerprint is persisted via
`SwissWeatherDB.get/set_provider_run_fingerprint` (schema_meta-backed),
not just held in memory — that's what makes the fix survive a restart.
Wired into all three of Open-Meteo, Meteoblue, and SRF's coordinators,
each with an in-memory fast-path cache loaded from the DB exactly once
per coordinator lifetime.

**L-07/L-08 (HIGH/MEDIUM-HIGH, quota and scheduling state reset on
restart)**: `AnnualCallBudget` (Meteonomiqs' 1000-calls/year tracker),
`BonusCallTracker` (Meteoblue's — and, by the identical bug class,
Meteonomiqs' own — same-day bonus-call allowance), and Meteoblue's
`_last_scheduled_call_hour` all lived only as plain instance attributes,
reset to their empty defaults on every restart/reload. Both tracker
classes gained `to_state()`/`from_state()` (or `load_state()`)
serialization; each affected coordinator now loads persisted state from
the DB exactly once per lifetime and re-persists immediately after every
mutation — not at some later checkpoint, since the whole point is
surviving a restart that could happen at any moment.

**L-09 (MEDIUM-HIGH, Model B false-trigger risk on restart)**:
`_previous_probability` reset to `0.0` on every restart. If a storm
probability was already elevated above the crossing threshold when HA
restarted, the first post-restart score could look like a fresh upward
crossing and fire an unwarranted bonus call. Now persisted
(`get/set_model_b_previous_probability`) and loaded once per lifetime —
which turns out to need no special-casing at all: comparing a fresh
score against the genuinely-last-known probability (rather than a reset
`0.0`) naturally makes an already-elevated-then-restarted scenario read
as "no new crossing," exactly as intended.

**L-10 (MEDIUM, retention purge never wired in)**: `purge_older_than()`
was correctly implemented but had no caller anywhere in production — the
configured `purge_days` setting had no operational effect, and
high-volume tables could grow unbounded. New `RetentionCoordinator`
(its own 24h schedule, independent of any polling coordinator) is now
the sole caller. Per the audit's own explicit recommendation, `purge_older_than`
also now excludes `reconciliation_status = 'pending'` rows from deletion
regardless of age — a `purge_days` window shorter than
`RETRY_GIVE_UP_AGE` (48h) must not silently convert a retryable gap into
a permanently lost learning sample.

**L-11 (MEDIUM, SRF fallback treats every failure as fallback-eligible)**:
a broad `except Exception` around the primary forecastpoint fetch meant
a *permanent* 4xx (confirmed real: the free plan's one-registered-
location restriction) got the exact same fallback-to-daily-endpoint
treatment as a genuine transient 5xx — wasting a call on every single
poll forever, while hiding the real, permanent cause behind what looked
like ordinary degraded operation. New `SrfPermanentError` (carries the
HTTP status) is raised specifically for 4xx and is NOT fallback-eligible;
5xx and other transient failures still fall through to the existing
fallback path unchanged.

**L-12 (MEDIUM, no 401 invalidation for cached SRF token)**: the only
token-refresh trigger was local expiry — a token invalidated for any
other reason (revocation, server-side rotation) stayed cached and kept
being sent as-is until its local expiry arrived on its own, causing
repeated auth failures in between. New `_async_get_with_token_retry`
wrapper: on a 401, clears the cache, refreshes exactly once, retries the
same request exactly once. Every SRF request (geolocation, forecast,
forecastpoint) now goes through it uniformly.

**Four more, found in a follow-up internal review (not in the external
audit)**:

- Meteoblue's request URL had no `&tz=` parameter. Per meteoblue's own
  API documentation (§3.6–3.7 of their data-packages doc): omitting `tz`
  makes the API auto-detect a *local* timezone from the request
  coordinates and return human-readable timestamps in that zone — only
  the numeric-epoch time formats are UTC "by definition." This client's
  parser tagged every parsed timestamp `tzinfo=timezone.utc` regardless,
  silently mislabeling what was very likely Europe/Zurich local time as
  UTC. Fixed with `&tz=UTC`, matching Open-Meteo's already-correct
  `&timezone=UTC` and meteoblue's own documented recommendation
  ("for autonomous systems we recommend to use UTC").
- Meteonomiqs' seasonal `/forecast/hourly` call fetched and parsed data
  into `last_hourly_forecast`, spending real annual-budget quota, and
  nothing ever read that attribute again — not persisted, not exposed,
  not fed into Model B, despite `const.py`'s own comment describing the
  call as "useful for Model B" during those months. That usefulness was
  never actually wired up. Rather than invent new Model B scoring
  behavior nothing specified, the data is now persisted into
  `forecast_snapshots` under `meteonomiqs_`-prefixed variable names — the
  same pattern SRF's own daily-only fields already use — specifically so
  it can never be picked up by Model A's blend even by accident
  (Meteonomiqs stays deliberately excluded from `ALL_FORECAST_SOURCES`).
- `aggregate_twice_daily_forecast()` used `max(temps)` for BOTH the day
  AND the night period — a night entry reported its warmest point
  (usually right after sunset) rather than the overnight low, the
  opposite of what "night temperature" conventionally means and
  inconsistent with `aggregate_daily_forecast`'s own separate
  `native_temperature`/`native_templow` split. This was silent: the
  shipped unit tests asserted the max()-for-night behavior as if it were
  correct. Night periods now use `min(temps)`.
- Dead code cleanup: `get_all_bucket_stats_for_measurement_hour()`
  (untested, no production caller, predated the v0.1.13 bulk-query
  rework) removed. `METEONOMIQS_MAX_CALLS_PER_EVENT` (an exact duplicate
  of `METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT`, which is the constant
  actually used) and `METEONOMIQS_KEEPALIVE_INTERVAL`'s dead sibling
  removed from `const.py`. `insert_forecast_snapshot()` (singular) and
  `get_forecast_values_for_valid_at()` were kept, not removed — both
  have direct test coverage of their own as documented test-setup
  utilities, even though production always uses the bulk variants.

**Verification**: every fix has direct test coverage — 198 tests total
(up from 169), including a from-scratch v1→v2 migration test that hand-
builds a raw pre-migration database file and confirms the exact
recent-vs-old row split and the `bucket_stats` wipe; two direct
regression tests reproducing L-01's mixed-batch re-learning scenario and
L-02's late-arrival scenario end-to-end; new async SRF client tests
(built around a small fake `aiohttp` session, since none existed before
this at that layer) for the 401-retry-once and permanent-vs-transient
error classification; persistence round-trip tests for every one of the
L-04 through L-09 durable-state fixes; and — new territory for this
project's test suite — coordinator-level tests that bypass `__init__`
and call the real async coordinator methods directly under a minimal
`FakeHass`, rather than only mirroring the logic elsewhere. That last
category earned its keep immediately: it caught a real bug in this same
pass's own L-07 fix (`set_annual_call_budget_state`'s keyword-only
parameters made it silently uncallable through the actual
`hass.async_add_executor_job` positional-only call path, despite passing
every DB-layer test written against it directly) before it shipped. See
`swissweather_fusion_v0.1.23_remediation_audit.md`'s "Coordinator-level
tests" section for the full account. `pyflakes` across the whole
integration shows no new issues introduced by this change (only the
same pre-existing cosmetic unused-import warnings from before).



**The bug**: `diagnostics.py` had two separate
`async def async_get_config_entry_diagnostics` definitions — an
orphaned, incomplete leftover from an earlier edit (ending abruptly
right after `secrets = [...]`, no return statement) directly above the
real, complete one. Python silently keeps the LAST definition of a
module-level function; the real one being called had no `secrets`
variable defined at all, because that assignment only existed in the
dead, shadowed copy above it. Every single "Download Diagnostics" click
crashed with `NameError: name 'secrets' is not defined`, confirmed
directly from a real HA log (six repeated crashes in three minutes,
from repeated attempts to check status) — this had been live since
v0.1.20 shipped, several days before being caught, precisely because it
only fails when that exact code path executes, not on import.

**Why the existing safety nets missed it**: `ast.parse`-based syntax
checking (what `tests/test_syntax.py` had) cannot catch this — a
duplicate function name at module scope, and a reference to an
undefined name inside a function body, are BOTH syntactically legal
Python. The fast unit suite tested the smaller helper functions
(`_health_summary`, `_redact_event`, `_redact_text`) directly, but never
called the top-level `async_get_config_entry_diagnostics` itself. Even
the deeper real-HA functional testing pass from v0.1.19/v0.1.20 didn't
catch it, because that pass tested `_reconcile` and `async_setup_entry`
specifically — it never occurred to call the diagnostics endpoint in
that same pass either.

**The fix**: removed the dead duplicate, moved `secrets = [...]` into
the actual function that runs. Confirmed via the same real-HA
functional-testing approach as before, with a proper control: the new
test reproduces the exact `NameError` when the bug is reintroduced, and
passes cleanly with the fix in place.

**The more durable fix — added a whole new class of test**: an AST-based
scan across every file in the package for duplicate top-level (and
per-class) definitions, plus a `pyflakes`-based undefined-name check.
Both run in well under a second, need no Home Assistant install, and
would have caught this exact bug — with the exact line number —
instantly, before it ever shipped. Confirmed with a control run: with
the bug artificially reintroduced, `test_no_undefined_names` fails
immediately, flagging all three now-broken call sites by line number;
restoring the fix makes it pass again. Both tests are now permanent
parts of the fast unit suite (`tests/test_syntax.py`), not the heavier
optional HA-dependent functional tests, specifically because they don't
need to be — this whole class of bug is catchable statically. Swept the
rest of the package the same way as part of this fix: no other
duplicate definitions or undefined names found anywhere else.

**Unrelated SRF status note, from the same investigation**: log
timestamps show SRF was still hitting the identical `400.01.007`
location-limit error as late as 05:53:59 UTC (12:53 Vietnam) — nearly
two hours after the ~11am Vietnam restart that was expected to have
already taken effect. A later diagnostics capture showed
`expert_weight_srf` numeric, meaning it did eventually start working,
sometime in the roughly 80-minute gap the available log doesn't cover.
Most likely explanation: SRF's backend took a while to actually apply
the location reset after the account-side change, rather than it taking
effect instantly. Not a code issue either way — noted here only because
it's a real, confirmed timeline worth having on record if this ever
needs revisiting.

## v0.1.21 — SRF's 400 confirmed: an account/API-plan restriction, not a bug

**Root cause confirmed** (v0.1.20 left this as "still under
investigation" — resolved via a standalone probe script rather than
another round of HA deploy cycles, much faster for this kind of thing).
The real error body, read directly from a live probe against the actual
account:

```json
{"code": "400.01.007", "message": "location mismatch for developer app", "info": "You have exceeded your location limit"}
```

The SRF free API plan allows exactly **one** registered location per
developer app, with **no self-service reset**. The account in question
had tested with slightly different coordinates once before (still
Frauenfeld-area, but not identical), which claimed the plan's one
allowed slot — every subsequent `v2/forecastpoint` call from a different
coordinate 400s, permanently, until SRF support resets it manually.
Confirmed directly with the account holder, not guessed.

The same probe also ruled out two other live hypotheses in the same
pass: only ONE geolocation candidate is ever returned for these
coordinates (so the documented "takes `results[0]`, never verified it's
the closest" concern isn't the cause here — nothing to fix in
`parse_geolocation_response` for this case), and percent-encoding the
comma in the ID made no difference (rules out a URL-encoding gateway
quirk). Genuinely nothing wrong in this codebase's SRF handling — the
merge fix, the UTC-normalization fix, and everything else from v0.1.19
were all real and correct, they just can't matter until `forecastpoint`
succeeds at least once.

**What WAS fixed this release**: nothing about this restriction is
fixable in code — but `async_fetch_forecastpoint` used to call
`resp.raise_for_status()` immediately on a non-200 status, which raises
before the response body is ever read. That meant SRF's own structured
error detail (the `code`/`message`/`info` shape above) never reached the
log or diagnostics — only a generic `400, message='Bad Request', url=...`,
which looks identical to a transient network/API problem and gives no
hint that the actual fix is "check your developer portal account," not
"debug the integration." Added `parse_srf_error_detail()` to read the
body first and extract SRF's own explanation when there is one, and
`async_fetch_forecastpoint` now raises a message that states outright
this is very likely an account/plan restriction, with where to go to
fix it (`https://developer.srgssr.ch`, or `meteo.api@srgssr.ch`). Once
recorded via the v0.1.20 `forecastpoint_fallback` diagnostics event,
this makes the real cause visible from a single diagnostics download,
with no HA log access and no probe script needed for anyone who hits
this same free-plan limit in the future.

**Also worth noting for anyone debugging this kind of "confirmed working
in dev, broken in someone else's account" issue**: a small, dependency-
free standalone Python script (stdlib `urllib` only, credentials as CLI
args, never printed) that reproduces the auth → geolocation →
forecastpoint flow directly against the real API turned out to be far
faster than iterating through HA deploy/log-download cycles — got a
definitive, complete answer (including testing every geolocation
candidate and a URL-encoding variant) in one run instead of several
rounds of "deploy, wait, download diagnostics, deploy again."

## v0.1.20 — a real, live credential/coordinate leak in diagnostics_events, found while chasing SRF's 400

**Background**: after v0.1.19 shipped, `expert_weight_srf` was still
showing `Unknown` in production. A downloaded diagnostics file showed
the retry-watermark fix genuinely working (1221 rows reconciled in one
run) — so the remaining cause had to be upstream of learning entirely.
`diagnostics_events` showed SRF landing on the fallback endpoint on
**every single poll** (6/6 observed over several hours), which is fatal
on its own: the fallback's fields map to `temperature_daily_max`/`_min`,
never a variable literally named `"temperature"`, so SRF's data is
structurally invisible to reconciliation while stuck there — not
delayed, never eligible at all.

**Root cause (still under investigation, not yet fixed)**: the actual
HA log showed the real reason for every fallback: `400, message='Bad
Request', url='.../v2/forecastpoint/{lat},{lon}'`. SRF's own developer
docs are explicit that the geolocationId — even though it's *formatted*
like a coordinate pair — must be a genuine registered point obtained via
their search, and "it is not enough to simply round any
geo-coordinates." The same coordinate-shaped ID that 400s against
`v2/forecastpoint` succeeds every time against the legacy `/forecast/`
endpoint, which is consistent with v2 having stricter validation than a
soon-to-be-deprecated v1. Not fixed yet — deliberately not guessing at a
5th SRF response-shape/behavior assumption without a live capture to
confirm it, consistent with why this project has a "verify against a
real response" rule in the first place (v0.1.1/v0.1.4/v0.1.8 all exist
because an earlier guess was wrong). Next step is inspecting what the
geolocation search itself actually returns.

**What WAS fixed this release — found investigating the above, more
serious than the SRF question itself**: `diagnostics.py`'s own
docstring and the "note" field returned to the user have always claimed
`diagnostics_events` "are passed through the same redaction" as
everything else. That was never actually true.
`DiagnosticsRecorder.record()` does no redaction of its own by design
(a dumb append — see diagnostics_recorder.py), and
`async_get_config_entry_diagnostics` used to do
`recorder.get_events() if recorder is not None else []`: passed straight
through, completely unredacted. Only one call site
(`SrfClient._record_diagnostic`, for raw API response bodies) redacted
anything before recording; every other `self._diagnostics.record(...)`
call across every coordinator — most importantly `poll_failure` events,
whose `detail` is built from `str(exception)` — went out as-is.

This became a genuine credential leak, not just a location one, once
combined with something separately found in the same pass: Open-Meteo's
own client builds its request URL as `url += f"&apikey={api_key}"` (see
clients/open_meteo.py) — a real API key in the URL, not just in headers.
A `poll_failure` from that source with diagnostic logging enabled would
have put the actual key into a downloaded diagnostics file in plain
text. (This specific user's own downloaded file happened not to contain
one — SRF's fallback always *succeeds*, so it never reached a
`poll_failure` — but the new `forecastpoint_fallback` event added in
this same release, recording the primary attempt's failure detail,
would have carried the raw 400 URL, coordinates and all, had it existed
one release earlier.)

Fixed centrally, not at each scattered call site: added
`redact_secret_values()` to `redaction.py` (a straightforward literal
substring replacement for known configured credential values — no
ambiguity to worry about the way coordinate-format-guessing has, a
secret is either present verbatim or it isn't), and
`async_get_config_entry_diagnostics` now redacts every event's `detail`
and any string values in `extra` — for both coordinates and secrets —
before returning, the same "redact once, at the single funnel point
everything already passes through" pattern already used for
`config_data`/`source_health`. `_health_summary`'s `last_data_error`/
`last_auth_error` also gained secret redaction (same Open-Meteo apikey
vector, missed by the original v0.1.10 fix which only added coordinate
redaction there).

**Also added — diagnostics visibility gaps, unrelated to the leak, found
in the same investigation**:
- Successful geolocation resolutions were never recorded to diagnostics
  at all (only failed lookups were) — so there was no way to see how
  many candidates SRF's geolocation search actually returned, or
  whether the chosen one looks like a genuine registered point, without
  separately pulling HA's core log. Now recorded on every (cached, so
  this only fires once per coordinate change) successful resolution.
- The primary `v2/forecastpoint` attempt's failure reason was only ever
  logged to HA's own log (`_LOGGER.warning`), never to
  `diagnostics_events` — meaning "100% of polls are silently landing on
  the fallback endpoint" was only visible by cross-referencing two
  separate downloads (diagnostics + HA log) rather than one. Now
  recorded as a `forecastpoint_fallback` event with the actual failure
  detail (redacted, per the fix above).

## v0.1.19 — Remediation of three independent audit reports

Three independent code audits of v0.1.18 (two general, one focused
specifically on why `expert_weight_srf` was rendering as `Unknown`
despite healthy SRF polling) converged on the same core findings.
Every finding below was independently re-verified against the actual
source before being fixed — either by direct code trace, or by a small
standalone simulation reproducing the exact failure mode described. See
`swissweather_fusion_v0.1.19_remediation_audit.md` in the repo root for
the full verification writeup, including the reproduction for the
watermark bug.

**Fixed — data/learning correctness (the most consequential group)**:

- **Reconciliation retry-watermark boundary** (`coordinator.py`
  `ModelALearningCoordinator._reconcile`). The v0.1.15 fix intended to
  give an unmatched-but-still-young forecast row repeated retry chances
  (up to `RETRY_GIVE_UP_AGE`, 48h) by capping the watermark at that row's
  own `valid_at` instead of advancing past it. But
  `get_forecast_snapshots_to_reconcile` queries with `valid_at >
  since_ts` (strict) — so the row became its own exclusion boundary and
  got **zero** further retries starting on the very next reconciliation
  pass, not the intended up-to-48h window. Directly reproduced with a
  small simulation before fixing (see the remediation audit). Fixed by
  backing the watermark off one microsecond before the earliest
  retryable row's `valid_at`, so it stays on the correct side of the
  strict inequality. This is very likely the primary reason
  `expert_weight_srf` stayed `Unknown` even with a healthy SRF fetch
  layer — any SRF row that missed a station match on its first attempt
  was silently gone, not "eventually given up on."
- **SRF `forecastpoint` hours/three_hours merge** (`clients/srf.py`).
  The merge used to be a dict `.update()` keyed only by `valid_at`,
  replacing three_hours' *entire* point list for a shared timestamp with
  hours' list — so a field three_hours reported that hours simply didn't
  (no real conflict) was silently dropped. Now merges per
  `(valid_at, variable)`: three_hours is the base layer, hours overwrites
  only the specific variables it itself provides at that timestamp.
- **SRF daily-fallback timestamp normalization** (`clients/srf.py`
  `parse_forecast_response`). Offset-aware `local_date_time` values
  (e.g. the real `+02:00` CEST the daily endpoint returns) kept their
  original offset instead of being converted to UTC, unlike the
  hourly/forecastpoint path's `_parse_entry_datetime`. Since
  `storage/db.py` compares/sorts `valid_at` as exact ISO strings, an
  un-normalized row could never match the UTC keys everything else uses
  — it would look stored but be invisible to the blend. Now calls
  `.astimezone(timezone.utc)` unconditionally, same as the hourly path.
- **Open-Meteo dedup was a no-op** (`clients/open_meteo.py`,
  `coordinator.py`). `issued_at` was always `datetime.now(timezone.utc)`,
  so the old dedup check (`parsed.issued_at <= previous_issued`) could
  essentially never be true — every poll looked like a brand-new model
  run, inflating `forecast_snapshots` and learning samples even when the
  upstream data hadn't changed. Added `run_fingerprint`, a deterministic
  content hash of the actual returned time/value series, and the
  coordinator now dedups on that instead.
- **Open-Meteo array-length mismatches were invisible**
  (`clients/open_meteo.py`). `zip(times, values)` silently truncates to
  the shorter array — a provider regression or malformed/partial
  response looked identical to a normal, slightly-short forecast. Added
  `ParsedForecast.array_length_mismatches`; the coordinator now logs a
  warning and records a diagnostics event when a mismatch is detected.
  The truncation behavior itself is intentionally unchanged — this is
  visibility, not a behavior change.

**Fixed — scheduling**:

- **Meteoblue's scheduled polling could permanently miss every slot**
  (`clients/meteoblue.py`). `is_scheduled_poll_time` required
  `local_dt.minute == 0`, but it's checked from a `DataUpdateCoordinator`
  ticking every 5 minutes *relative to whenever the coordinator was
  created* (HA startup or reload) — not wall-clock aligned. Unless that
  moment happened to land on a multiple-of-5 minute that was also `:00`,
  the checks would land on `:17`/`:22`/`:27`/... forever, and the
  12:00/16:00/20:00 (or winter) scheduled calls could simply never fire.
  `is_scheduled_poll_time` is now a whole-hour window check; the existing
  `last_scheduled_call_hour` guard in `should_fire_scheduled_call` (not
  minute alignment) is what already prevented duplicate fires within the
  same hour, so removing the minute check doesn't introduce repeat
  firing.

**Fixed — diagnostics/security**:

- **Coordinate redaction only covered 3 hardcoded formats**
  (`redaction.py`). `str(value)`/`.4f`/`.2f` missed coordinates embedded
  at other decimal precisions or in different textual forms (e.g.
  bracketed `[lat, lon]`). Widened to decimal precisions 2 through 8
  (2 is the floor deliberately — 0/1-decimal renderings are short enough
  to plausibly collide with an unrelated number elsewhere in a weather
  payload; this was caught directly by a test during development, where
  a 0-decimal longitude variant clipped the front off an unrelated
  longer number). Substitution is guarded on both sides against an
  adjacent digit or decimal point so a match can't clobber part of a
  longer, unrelated number, and the longest/most-precise variant is
  always tried first.

**Documentation-only correction (not a behavior change)**:

- `parse_geolocation_response`'s docstring claimed SRF's geolocation
  search results are sorted by distance and "the closest one" is taken.
  The implementation has only ever taken `results[0]`. Corrected the
  docstring to describe actual behavior and explained why this wasn't
  changed to a real distance calculation in this pass: none of the three
  confirmed SRF geolocation response shapes include a documented
  lat/lon or distance field per entry, and guessing at an unconfirmed
  field shape is exactly the mistake that caused three earlier SRF
  parsing bugs (v0.1.1, v0.1.4, v0.1.8) in the first place. Tracked as a
  follow-up risk requiring a live multi-result capture, not fixed
  speculatively.

**Deliberately not changed in this pass** (real gaps, but design
decisions or out of scope for a bug-fix release, not defects):
Model A still only reconciles temperature/humidity/pressure (SRF's
precip/wind can't get a learned weight without local rain/wind ground
truth — this is why SRF's *weight* can legitimately stay neutral even
after the watermark fix, though it should now at least become numeric).
`ForecastAccuracySensor` and the Model B training-timestamp sensor
remain stubs. Both were flagged as Low severity / explicit product gaps
by all three source audits, not correctness bugs.

**Also fixed — found via real functional testing, not the original
audits**: closed a documented test-suite gap (`coordinator.py`,
`__init__.py`, `config_flow.py`, `weather.py`, `sensor.py`, and
`binary_sensor.py` were previously only syntax-checked, never
functionally exercised, since `homeassistant` wasn't installed when
this project was built) by installing the real `homeassistant` package
and `pytest-homeassistant-custom-component` for a one-off deeper
verification pass. This surfaced a genuine defect the static audits
never caught: `SwissWeatherDB.__init__` calls `sqlite3.connect()`
without first ensuring its parent directory exists. In a normal
production HA install this is masked because `.storage/` already exists
by the time any integration loads — but a fresh test instance without
it reproduced an unhandled `sqlite3.OperationalError` immediately, well
before `__init__.py`'s per-source failure isolation even gets a chance
to run. Fixed with `os.makedirs(parent_dir, exist_ok=True)` before
opening the connection. The same functional pass also confirmed, for
real rather than by reading the code, that (a) the retry-watermark fix
works in the actual `ModelALearningCoordinator` class (with a control
run proving the test would have caught the pre-fix bug), and (b) the
whole integration survives total network failure gracefully — every
source coordinator's first refresh failing individually, all 9
coordinators and 40+ entities still set up successfully, clean unload
afterward. See `swissweather_fusion_v0.1.19_remediation_audit.md` for
the full account.

## v0.1.18 — SRF's real hourly endpoint, confirmed and fully wired in

Built, NOT yet deployed — held pending a joint decision on timing, same
as the previous UI-text and probe-script work, given the ongoing
stability soak test.

**Background**: v0.1.8 confirmed SRF's `/forecast/{id}` endpoint (the
only one this project had ever found) returns daily-only data — genuine
evidence, not a defensive guess made out of freeze-suspicion (worth
being precise about that distinction, since it came up). A separate,
untested reference implementation later suggested a different endpoint
(`v2/forecastpoint`) might return real hourly data, but its own assumed
response shape didn't match anything this project had ever confirmed,
and no network access exists in this environment to verify SRF's API
directly. Rather than guess a second time, a standalone probe script
(not part of this codebase) was built and run directly against the real
API. Confirmed:

- `v2/forecastpoint/{geo_id}` is real and working (200 OK).
- It returns `days`, `three_hours`, and `hours` as **top-level siblings**
  alongside `geolocation` — not wrapped in a `"forecast"` key, which
  neither this project's own prior assumption nor the reference
  implementation's guess had right either. SRF's response shape has now
  surprised this project four separate times across its history.
- Confirmed field names differ from the reference's guesses in several
  places — `TTTFEEL_C` not `FEELSTTT_C`, `UVI` not `UV_INDEX` — verified
  against a real response body, not assumed.

**Built, in full, from the confirmed real data — nothing skipped**:
- `parse_forecastpoint_response()` extracts every confirmed numeric
  field from `hours`, `three_hours`, and `days`. The five measurements
  Model A's blend actually looks up (temperature, humidity, pressure,
  precip, wind_speed) use the exact same variable names every other
  source already uses — meaning SRF can finally participate in the
  hourly blend at all, ending its permanently-"Unknown" expert weight.
  Every other confirmed field (dewpoint, feels-like, uncertainty bounds,
  snow, irradiance, sun minutes, wind gust/direction, precip
  probability, symbol codes, and the daily equivalents) is stored
  prefixed `srf_`/`srf_daily_`, specifically so none of it can ever be
  mistaken for one of the five core measurements and accidentally picked
  up by the blend coordinator's generic bulk queries.
- **One HTTP call, not several** — the new endpoint already returns
  hours+three_hours+days together, so there was never a reason to also
  call the old endpoint separately. `SrfCoordinator` tries the new
  endpoint first and falls back to the old daily-only one only if it
  fails for any reason — daily data is better than none, and SRF's API
  has surprised this project enough times that keeping a working
  fallback seemed worth the small amount of extra code.
- **Unit conversion, made explicit and impossible to miss**: SRF reports
  wind in km/h; every other source reports `wind_speed` in m/s (the
  v0.1.5 Open-Meteo fix). `KMH_TO_MS` converts both `FF_KMH` (→
  `wind_speed`) and `FX_KMH` (→ `srf_wind_gust`) — storing the raw km/h
  value under the same name other sources use would have silently
  corrupted the blend exactly the way the original v0.1.5 bug did.
- **`hours`/`three_hours` overlap, deduplicated deliberately**: both
  arrays cover some of the same timestamps (confirmed: hours starts at
  the top of the current day, three_hours starts 2 hours later the same
  day). Without deduplication, both would insert a row for the same
  (source, variable, valid_at), and which one a later query picked up
  would depend on insertion order, not a deliberate choice. `hours`
  (finer native granularity) wins for any timestamp both cover;
  `three_hours` fills in whatever extends beyond `hours`' own range.
- **What's deliberately NOT persisted, and why**: `SUNRISE`/`SUNSET` are
  timestamps, not numbers — `forecast_snapshots.value` is a REAL/float
  column, and sunrise/sunset isn't a learning-relevant quantity in the
  first place (Home Assistant's own `sun` entity already tracks this
  astronomically). The `cur_color`/`min_color`/`max_color` fields are
  UI color hints entirely derived from a temperature value already being
  stored — not independent weather data. Neither is "lost" data so much
  as genuinely redundant or out of scope for a time-series weather
  database.

**Test coverage**: 11 new tests using the actual confirmed sample
entries from the real API response (not fabricated data) — core
measurement extraction, unit conversion, srf_ prefixing, daily fields,
the hours-wins-over-three_hours deduplication (and its inverse — that
three_hours-only timestamps still come through), UTC conversion, and
defensive handling of missing fields and malformed input.

**Deliberately out of scope for this build** (functionality first, per
the established priority): no new sensors or weather-entity exposure for
the srf_ extras (dewpoint, UV, feels-like, etc.) — that's a UI/display
question, not a functionality one. SRF's confirmed symbol_code values
(-1, 100, 11, 21) don't match the existing 1-16 SYMBOL_MAPPING table, but
this doesn't currently matter: the blended weather entity's own
condition logic is a simple precipitation-based heuristic that never
reads any source's symbol code, so there's nothing depending on that
mapping being correct right now. Worth revisiting only if SRF's own
condition ever needs to be surfaced independently of the blend.

## v0.1.17 — an urgent, confirmed budget-drain gap in Meteonomiqs's bonus-call path

Reported directly, with real evidence: `sensor.*_meteonomiqs_last_success`
advancing every 5 minutes, not once a day as designed. Confirmed against
the actual diagnostics events log — `meteonomiqs | poll_success | nowcast`
firing at 04:56, 05:01, 05:06, 05:11, 05:16, exactly matching `ModelBCoordinator`'s
5-minute scoring interval, not Meteonomiqs's own 6-hour
`CHECK_INTERVAL` (verified directly, ruling out the coordinator's own
schedule as the cause).

**The actual gap, found by comparing the two symmetric code paths
directly**: `MeteoblueCoordinator.async_request_bonus_call()` has always
been protected by `BonusCallTracker` — capped at one bonus call per
calendar day regardless of how many times the cross-model trigger fires.
`MeteonomiqsCoordinator.async_request_bonus_call()` never had the
equivalent — it only checked the overall 1000-calls/year budget, with no
per-day cap at all. If the trigger condition kept re-evaluating true
(a separate question, not yet fully resolved — see below),
meteoblue was protected and Meteonomiqs wasn't, on an otherwise identical
code path.

**Fixed**: `BonusCallTracker`'s daily cap (previously hardcoded to
`METEOBLUE_MAX_BONUS_CALLS_PER_EVENT`) is now parameterized, and
Meteonomiqs's coordinator gets its own instance with the same one-per-day
philosophy (`METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT`). This bounds the
worst case to one extra call per day regardless of the underlying
trigger-firing question.

**Being honest about what's still open**: `evaluate_cross_model_trigger`'s
own logic (`previous_probability < threshold <= current_probability`,
requiring an upward crossing, not just staying elevated) looks correct on
inspection, and the diagnostics snapshot's `current_probability` (0.375)
sits below the 0.5 threshold — which shouldn't be triggering at all under
that logic. The most likely explanation, not yet confirmed, is early
cold-start noise in `compute_tendency_features` (sparse station history
right after startup producing a probability that briefly crossed above
threshold on several early cycles before settling) — but this is a
hypothesis, not a verified root cause. The daily cap added here contains
the *consequence* regardless of the exact mechanism; if the trigger keeps
firing even once a day going forward, that's worth investigating further
with more targeted logging of the per-cycle probability values.

## v0.1.16 — the actual root cause of the multi-hour freeze, most likely found

A third outside review proposed something genuinely different from every
previous theory: not a resource contention problem, not a slow
computation, but a Home Assistant framework behavior — `DataUpdateCoordinator`
stops automatically rescheduling itself once it has zero registered
listeners. Checked directly against Home Assistant's own source history
before acting on it, the same discipline as every other finding in this
project: a core PR titled "Only schedule a refresh if listeners" confirms
this is real, intentional behavior (added specifically to stop a
coordinator nobody reads from polling forever and leaking memory), not a
guess about undocumented internals.

Checked directly against this project's own entities: `CoordinatorEntity`
is used in exactly two places — the weather entity and
`ExpertWeightSensor` — and both are tied to `blend_coordinator` only.
Every other coordinator (station, open_meteo, srf, meteoblue,
combiprecip, meteonomiqs, model_b, learning) has never had a single
registered listener, because every sensor reading their data does so via
plain attribute access (`self._runtime["x_coordinator"].health`, `.data`,
`.current_probability`) rather than the `CoordinatorEntity` pattern that
would have registered one automatically. That's an exact match for the
observed pattern across every diagnostics capture this project has taken:
one guaranteed first refresh via `async_config_entry_first_refresh()`,
then total silence, forever, for every affected coordinator
simultaneously — not because anything hung or crashed, but because Home
Assistant's own coordinator framework had no reason to ask any of them to
run again.

**This also explains why v0.1.12, v0.1.13, and v0.1.14 didn't resolve
it.** Each of those fixes addressed a real, separate problem — but a
coordinator that Home Assistant isn't even asking to run again isn't
helped by making its own run safer (the SQLite lock), faster (the
query-count reduction), or more bounded (the HTTP/coordinator timeouts).
None of them touch *whether the coordinator gets invoked at all*.

**Fixed** with a genuine (functionally no-op) listener registered for
every coordinator that lacked one, removed cleanly via
`entry.async_on_unload()` on unload — `blend_coordinator` deliberately
excluded, since it already has real listeners. A larger refactor
(converting every sensor to a proper `CoordinatorEntity` of its own
coordinator, which would also get live push-based updates instead of
Home Assistant's default ~30s polling) is a reasonable follow-up but was
not the priority for this specific fix — the goal here was the smallest,
most direct change that closes the actual scheduling gap.

**Being honest about confidence here**: this is the strongest, most
directly-confirmed candidate found so far — a specific, checkable HA
framework behavior, an exact structural match against this project's own
entity code, and a coherent explanation for why three prior fixes didn't
help. But "most likely" isn't "confirmed" until it's been watched running
for the same several-hour window that made every previous freeze
undeniable.

## v0.1.15 — an independent, from-scratch review, plus two outside code reviews, all reconciled into one pass

Prompted directly: "stop debugging reactively, review this as if it were
new code, ICS-grade." A full, independent pass through every file,
followed by two additional outside code review reports (one focused on
production risk, one addendum on the elevation override), with every
claim from all three checked directly against the actual source before
acting on it — nothing here was assumed correct or incorrect based on how
confidently it was written.

**One specific correction worth calling out**: the person pushed back on
the Meteonomiqs 30-day finding, believing a daily forecast call was
already keeping the API key alive independently of the nowcast-specific
30-day check. Tracing the actual code showed this belief didn't match
reality — `needs_keepalive_call()` gated the *entire* method, both the
seasonal forecast branch and the nowcast fallback, meaning neither ever
fired more often than once per 30 days regardless of season or time of
day. Worth remembering: an outside review's finding matched the code:
even a mismatch between design intent (documented elsewhere as "daily")
and implementation reality can survive quietly for a long time.

**Fixed, all confirmed against the current source before touching anything:**

1. **No coordinator was ever explicitly shut down** — `entry.async_on_unload(coordinator.async_shutdown)`
   now registered for all 9. Independent review.
2. **No cleanup on partial setup failure** — a failed `async_forward_entry_setups`
   now shuts down every already-started coordinator and closes the
   database before re-raising, instead of leaving them orphaned for a
   retry to duplicate. Independent review.
3. **Meteonomiqs keepalive redesigned**: daily-once-per-day is now the
   actual gate; the 30-day threshold is a warning-only backstop, not
   something that blocked every call. Outside report, confirmed above.
4. **Zero elevation overrides silently discarded** — fixed on both the
   config-flow write side and the `__init__.py` read side; both used to
   treat `0.0` as falsy. Outside report, confirmed.
5. **Learning watermark advanced past permanently-skipped rows** — now
   bounded: a row that can't find a matching station reading gets
   retried for up to 48 hours (`RETRY_GIVE_UP_AGE`) before the gap is
   treated as genuinely permanent, instead of being dropped on the first
   miss. Outside report, confirmed.
6. **A transient bonus-call failure discarded the freshly computed storm
   probability** — isolated in its own try/except; the base scoring
   result is now always saved regardless of whether the meteoblue/
   Meteonomiqs bonus calls succeed. Independent review.
7. **Meteonomiqs's "local noon" decision used UTC, not local time** —
   same class of bug already fixed for meteoblue in v0.1.6, never
   checked here; fixed alongside item 3 above. Outside report, confirmed.
8. **Persisted storm probability could differ from the live sensor
   value** — now persisted *after* Meteonomiqs refinement, with both
   `base_probability` and `refined_probability` stored explicitly rather
   than the refined value silently overwriting what was saved. Outside
   report, confirmed.
9. **`apply_lapse_rate_precorrection` existed and was tested since early
   in this project, but nothing ever called it** — now wired into
   `OpenMeteoCoordinator` specifically, since Open-Meteo's response
   confirmed includes the model grid cell's own elevation as a top-level
   field (verified via search, not assumed) — the one piece of data the
   correction needs, and the only source with confirmed elevation data
   available. Not applied to SRF/meteoblue/Meteonomiqs, whose own grid/
   station elevation isn't currently captured. Outside report, confirmed.
10. **Health/degraded status could look fine while a source was actually
    down** — `DegradedBinarySensor` used to check only the 6 source
    coordinators' coarse `last_update_success` flags, which can't see
    one Open-Meteo model failing while the others succeed, or a
    Meteonomiqs failure (that coordinator catches every internal error
    and always returns normally). Now uses the same per-source health
    check `StatusSensor` already used correctly. Outside report,
    confirmed, combined with an independent-review variant of the same
    root issue.
11. **Elevation wasn't editable after initial setup** — added to the
    options flow with an explicit "clear to revert to auto-lookup"
    checkbox, rather than relying on fragile empty-string-to-float
    coercion to distinguish "cleared" from "left alone". Addendum,
    confirmed.
12. **Daily/twice-daily aggregation used UTC calendar-day boundaries**
    regardless of configured local timezone — `local_tz` now threaded
    through from `dt_util.now().tzinfo` (the same proven pattern already
    used for the meteoblue/Meteonomiqs local-time fixes), defaulting to
    UTC for any caller not yet passing a real timezone. Outside report,
    confirmed (this had also already been documented as a known
    simplification before the outside review flagged it).
13. **TOCTOU race in `BonusCallTracker`/`AnnualCallBudget`** — atomic
    `try_use_bonus_call()`/`try_call()` methods added. Used for
    meteoblue's bonus-call path (a clean fix, since that path solely
    owns its own recording); deliberately *not* used for Meteonomiqs's
    bonus-call path, since that coordinator's shared fetch method already
    records usage internally on success — using the atomic method there
    too would have double-counted every bonus call. Outside report,
    confirmed, with the double-counting risk caught and avoided during
    the fix itself rather than after.

**Explicitly not changed, and why:**
- The `asyncio.timeout` limitation (a genuinely stuck executor thread
  isn't freed by the coordinator-level timeout) is a real architectural
  constraint of the executor-job pattern itself, not a bug with a
  fix — documented, not "fixed".
- Non-`CoordinatorEntity` sensors relying on default polling — a minor
  staleness characteristic (up to ~30s lag), not a correctness issue,
  left as-is to avoid unnecessary risk in an already large batch of
  changes.

## v0.1.14 — an outside code review, checked and confirmed against the actual source

After the huawei_solar/Modbus theory was ruled out (a different Claude
instance working on that integration rejected it, and the person
confirmed every *other* integration kept working fine throughout the
freeze — ruling out a genuine system-wide event loop stall, which is what
would be needed for a Modbus hang elsewhere to explain this), that same
external review turned its attention to this project's own source and
produced a numbered list of concrete, checkable claims. Every one of them
was verified directly against the actual code before acting on it — not
assumed correct because it came with confident framing, and not dismissed
either. All four of the following were confirmed real:

1. **`ExpertWeightSensor` called `self._db.get_bucket_stats()` directly
   inside `native_value`** — a plain property with no `CoordinatorEntity`
   backing. Home Assistant polls such properties directly on the event
   loop, exactly the class of bug fixed for weather.py back in v0.1.5,
   but never caught here. This is the one fix in this batch most likely
   to be the actual root cause: it's the only confirmed bug genuinely
   *unique* to this integration — no other integration would have this
   specific pattern — which fits the reported symptom (only SwissWeather
   Fusion freezes, everything else keeps working) far better than any
   previous theory did. Worse, combined with v0.1.12's `threading.Lock`,
   a blocking `.acquire()` call sitting directly on the event loop while
   an executor job holds the lock is precisely the kind of thing that
   could produce exactly what's been observed. Fixed: the value is now
   computed for free during `ModelABlendCoordinator`'s existing bulk
   `bucket_stats` fetch (v0.1.13), and the sensor reads it as a cached
   `CoordinatorEntity` value — zero direct database access.
2. **Four of five HTTP clients (open_meteo, meteoblue, meteonomiqs,
   combiprecip) had no explicit request timeout at all** — only SRF did,
   from the v0.1.6 fix, because SRF was the one under active
   investigation at the time. The other four were simply never revisited
   with the same discipline. All four now have explicit
   `aiohttp.ClientTimeout` (30s each, 60s for CombiPrecip's actual file
   download).
3. **Only SRF's coordinator had an outer `asyncio.timeout` backstop.**
   All nine coordinators now have one, sized to what each actually does
   (30-120s).
4. **Startup was strictly sequential** — nine coordinators' first
   refreshes, awaited one after another, some involving multiple HTTP
   calls each (SRF's token+geolocation+forecast sequence alone). This
   could make `async_setup_entry` itself slow enough to risk interacting
   badly with Home Assistant's own setup-timing expectations — and,
   notably, this specific risk scales with coordinator *count*, which is
   again something close to unique to an integration with nine of them
   compared to a typical integration's one or two. Converted to two
   concurrent groups via `asyncio.gather` (source coordinators first,
   then the three that depend on the sources' output), preserving the
   real data dependency while no longer paying for it sequentially.

**Being honest about where this leaves things**: fix #1 is a genuinely
strong candidate, and fixes #2-4 are correct regardless of whether they
turn out to be *the* cause — unbounded HTTP calls and sequential startup
scaling with coordinator count are real risks on their own. But three
previous fix attempts (v0.1.12, v0.1.13) were also reasonable and didn't
resolve the reported freeze, so this is not being presented as confirmed
— it's the best-evidenced attempt yet, and whether it holds should become
clear from the next deployment.

## v0.1.13 — the lock fix didn't work; a real performance problem, fixed regardless

A 50-minute post-deploy diagnostics capture showed the v0.1.12 lock fix
did not resolve the freeze. The identical signature recurred exactly:
every coordinator succeeds once in the startup burst, then goes silent —
this time even the learning coordinator's `last_run_time` (new in
v0.1.11's wider diagnostics coverage) confirmed the same freeze instant.
Since serializing all database access didn't stop it from recurring, the
SQLite-connection-concurrency theory from v0.1.12 is not the (or not the
whole) explanation. Worth saying plainly rather than defending a theory
the evidence didn't support.

**What's fixed instead, independent of full certainty about the exact
mechanism**: `ModelABlendCoordinator._compute_blend` was doing up to
~8,400 individual sequential database round trips every single 10-minute
cycle — 168 forecast hours × 5 measurements × up to 5 sources, each
needing its own `get_forecast_values_for_valid_at` *and* `get_bucket_stats`
call. An executor job potentially taking minutes every cycle, holding a
thread the whole time, is a real problem regardless of whether it's the
full explanation for the reported freeze — plausible given every affected
coordinator shares the same executor pool, and worth fixing either way.

Replaced with two bulk queries per cycle
(`get_forecast_snapshots_in_window`, `get_all_bucket_stats`) that fetch
everything the whole 168-hour computation needs up front; `_blend_at`
becomes a pure in-memory dictionary lookup with zero database access.
Same blending math, same result — just no longer paying for a round trip
per individual (hour, measurement, source) combination. Confirmed with a
test that counts actual SQL statements executed (via sqlite3's own
`set_trace_callback`, since `Connection.execute` itself can't be
monkey-patched) against a synthetic 4,200-row dataset: exactly one query
per bulk fetch, regardless of data volume — not the roughly 8,400 queries
the old per-lookup approach would have made against the same data.

**Being honest about where this leaves the actual freeze question**: this
is a real, independently-justified fix, not a confirmed resolution.
Whether it's sufficient — or whether the true cause is something else
entirely — should become clear from the next deployment's diagnostics.

## v0.1.12 — a likely root cause for the multi-hour freeze, and the fix

A 5-hour diagnostics capture made the freeze question conclusive rather
than merely suspicious. All 8 recorded events — spanning CH1, CH2, D2,
SRF (including its own token/geolocation/forecast sequence), CombiPrecip,
and Meteonomiqs — landed within **3.1 seconds** of each other
(07:26:46.330 to 07:26:49.415), matching exactly the startup first-refresh
sequence. Then nothing, from any of them, for over 5 hours.
`internal_coordinators` (new in v0.1.11) showed the learning coordinator
(20-minute interval) at 3.56 hours since its last run — roughly 10-11
missed cycles — with its `last_run_time` landing in that exact same
3-second window as everything else. Four coordinators with four
completely different intervals (5, 15, 20, 45 minutes), all frozen at the
identical instant. That rules out a bug specific to any one source.

**Leading theory, and the fix applied regardless of full certainty**:
every one of those coordinators shares exactly one thing —
`SwissWeatherDB`'s single SQLite connection, accessed via
`hass.async_add_executor_job()` from whichever thread Home Assistant's
executor pool happens to assign. `check_same_thread=False` (set from the
start of this project) only disables Python's own same-thread safety
check; it does not make truly concurrent, simultaneous access to the same
Connection object from multiple threads safe. `busy_timeout` governs
SQLite-level lock contention between separate connections — it does
nothing for Python-level thread-safety of one shared connection object
being used from more than one thread at once. A burst of coordinators all
firing their first refresh within a few seconds of each other at startup
(exactly what the timestamps show) is precisely the condition most likely
to trigger this: multiple executor threads touching the same connection
object simultaneously. If that produces a genuine hang (rather than an
error `busy_timeout` would have caught), the affected coordinator's
`_async_update_data()` never returns — and since Home Assistant only
schedules a coordinator's next check after the current one completes,
that coordinator would appear to freeze forever, exactly as observed.

**Fix**: `threading.Lock` now serializes every access to the shared
connection, in `storage/db.py`. Every method's body — reads and writes
alike — runs inside `with self._lock:`. This is a defensively correct fix
independent of the exact mechanism: unsynchronized concurrent access to a
single mutable shared resource from multiple threads is always a risk
worth eliminating, whether or not it's confirmed as *the* cause here.

**Being honest about what this does and doesn't prove**: this can't be
verified against the actual failure without reproducing it live, which
wasn't practical here. What's confirmed instead is a dedicated
concurrency test (`test_db_concurrency.py`) simulating the closest
practical approximation — many threads hammering the same
`SwissWeatherDB` instance simultaneously, both write-heavy and mixed
read/write patterns — with a bounded join timeout so an actual deadlock
would fail the test loudly rather than hang the suite. All of it
completes correctly now. Whether this was truly the root cause of the
reported freeze, or just a fix for a related but distinct hazard, should
become clear from the next real deployment's diagnostics — worth
explicitly checking that the "everything succeeds once, then nothing"
pattern doesn't recur.

## v0.1.11 — the "frozen" question gets much stronger evidence, and diagnostics gets wider coverage

A follow-up detail changed the read on v0.1.10's finding significantly:
the diagnostic-logging toggle had been on for roughly 2 hours before that
file was downloaded, not seconds. With a 2-hour window, CombiPrecip alone
(5-minute interval) should have attempted roughly 24 more polls — and
left literally zero trace of any of them: no new success timestamp, no
incremented failure count, no new diagnostic event. That rules out
"downloaded too soon after reload" as the explanation and makes this
real, credible evidence of an actual scheduling problem, not an
artifact of when the file was captured.

**A real gap in what was being measured, found in the process of taking
this seriously**: `diagnostics.py` only ever reported the six source-
fetching coordinators. It said nothing about `ModelABlendCoordinator`,
`ModelALearningCoordinator`, or `ModelBCoordinator` — the exact three
whose apparent freezing raised this question in the first place, several
versions ago. They were invisible in every diagnostics capture so far.
Fixed: all three now appear under a new `internal_coordinators` key.

**Also added, so the next capture is self-explanatory rather than
requiring elapsed-time arithmetic by hand every time**: every coordinator
(source-fetching and internal alike) now reports Home Assistant's own
built-in `last_update_success` (a signal independent of this project's
own health bookkeeping, in case that bookkeeping itself has a bug) and a
computed `overdue` flag — comparing how long it's actually been since the
last success against the coordinator's own configured interval, with a
3x-interval margin before flagging anything (so ordinary jitter doesn't
get flagged as if it were the same problem this exists to catch). A
5-minute-interval coordinator sitting at 2 hours since its last success
will now show `"overdue": true` directly in the file, rather than needing
that conclusion worked out from raw timestamps each time.

This still doesn't explain *why* — that requires whatever the next
capture actually shows — but it's now positioned to actually show it.

## v0.1.10 — a real redaction gap, found in the first actual diagnostics download

The very first diagnostics file downloaded and shared back confirmed the
feature works (real health data, correctly redacted config), but also
caught a genuine gap in the redaction itself: an Open-Meteo 503 error's
message was literally the full request URL, which embeds latitude and
longitude as query parameters (`?latitude=...&longitude=...`). The
original `diagnostics.py` redacted `config_data`/`config_options` (this
project's own settings) but assumed `last_data_error`/`last_auth_error`
"needed no redacting" since they're short status strings, not structured
config — that assumption was wrong. Any error message built from
`str(err)` on an aiohttp exception can carry a full URL. Fixed: both
fields now go through the same coordinate-string redaction pass as
everything else, using the real (pre-redaction) coordinates extracted
from `entry.data` specifically for this purpose. This is the same class
of problem as SRF's own `geolocationId` being a bare coordinate string
under an innocuous key — value-embedded location data, not just a
structured field with an obviously sensitive key name.

**Also worth being honest about**: that same download showed
`diagnostics_events: []` despite SRF, CombiPrecip, and Meteonomiqs all
showing genuine successful poll timestamps in the same capture. Code
review of the recording wiring (SrfClient → SrfCoordinator →
DiagnosticsRecorder → diagnostics.py) shows it correctly connected — the
same shared recorder instance, enabled before any coordinator is
constructed, with recording calls in the right places. No bug was found
by inspection, but the observed behavior isn't explained either. The
timestamps suggest this capture was taken very close to a reload
(enabling the toggle itself triggers one), which is the most likely
factor, but this is flagged as unresolved rather than quietly assumed
fixed — worth downloading diagnostics again after letting more poll
cycles complete, to see whether events populate on a later capture.

## v0.1.9 — toggleable diagnostic logging, ending the screenshot-and-guess cycle

Every debugging round from v0.1.1 through v0.1.8 followed the same
pattern: a problem shows up, a few sensor states get screenshotted from a
phone, real progress only happens once an actual log file gets uploaded —
and even then, a fixed log-truncation length repeatedly cut off the
useful part of SRF's response before it could be seen (500 → 4000 → 20000
characters, three separate increases). Requested directly: a
toggleable, downloadable diagnostic log, off by default, that ends this
cycle.

**Two pieces working together**, deliberately using an existing HA
mechanism rather than inventing a new one:

1. **`DiagnosticsRecorder`** (`diagnostics_recorder.py`) — a bounded
   in-memory ring buffer (1000 events) that every coordinator writes
   structured events into (poll start/success/failure, and for SRF
   specifically, the full untruncated raw response body on both success
   and failure) when enabled. Off by default — an options-flow toggle
   (`diagnostic_logging_enabled`), which like every other options change
   triggers a full reload, so flipping it naturally starts the buffer
   fresh. Deliberately **not persisted to the database** — survives until
   the next restart/reload, not across one. The intended workflow is
   "enable it, let the problem happen, download before restarting," not
   "look back at last week" — persisting this would mean a new table, a
   purge policy, and real storage growth for what's meant to be a
   short-lived, active-debugging tool.
2. **`diagnostics.py`** implements Home Assistant's own
   `async_get_config_entry_diagnostics` hook — this is what makes a
   "Download Diagnostics" option appear natively in the integration's UI
   (Settings → Devices & Services → the three-dot menu). No custom
   download mechanism was built; HA already has one for exactly this.

**The redaction requirement turned out to be bigger than "hide the API
keys."** A real captured SRF response embedded `alarm_region_name`,
`district`, and a `geolocation_names` entry with `name`/`province` —
identifying location data from the *third-party API's own response
body*, not just this project's configuration. Since the whole point of
this feature is content meant to be shared (with Claude, in a GitHub
issue, wherever), `redaction.py` runs a combined pass **before anything
enters the buffer, not as a filter applied only at export time**:
key-name-based redaction (catching `lat`/`lon`/`elevation`/`district`/
`name`/etc. wherever they appear in arbitrary nested third-party JSON,
not just a fixed set of top-level config keys) plus a text-level
substitution of the exact configured coordinates in likely string
formats (catching SRF's own `geolocationId`, which is literally the
string `"46.9480,7.4474"` stored under the innocuous key name `"id"` —
key-based redaction alone wouldn't flag that). Deliberately over-inclusive
rather than precise: an occasionally-redacted harmless field costs far
less than missing a genuinely identifying one from an API whose exact
shape isn't fully known in advance. Tested directly against the real SRF
response structure that motivated this, not just synthetic examples.

Wired into all six data-fetching coordinators (Station, Open-Meteo, SRF,
meteoblue, CombiPrecip, Meteonomiqs) for at least lightweight success/
failure events — useful on its own for the open "is everything actually
updating" question from earlier — with SRF specifically getting full raw-
response capture, since it's the source under active investigation.

## v0.1.8 — SRF's real shape confirmed: daily, not hourly, and isolated accordingly

The v0.1.7 truncation increase (500→4000 characters) worked exactly as
intended: a production log finally showed the real response body instead
of getting cut off before the useful part. What it showed was a genuine
surprise, not another shape variant of the same data — SRG-SSR's own
documentation confirms they offer two structurally different response
types, "hourly forecasts for each day" or "one core statement per day (no
hourly progression)" — and what we're actually getting is the **daily-only**
variant: `forecast.day`, a list with `TX_C`/`TN_C` (day max/min
temperature), `RRR_MM` (day precip total), `FF_KMH` (day avg wind). No
humidity or pressure field appears anywhere. None of the originally
assumed field names (`temperature`, `relativeHumidity`,
`meanSeaLevelPressure`) exist in any real response — they were a
plausible-looking guess from documentation, never verified, and the
actual cause of every previous "zero usable data points" result.

**The fix isn't just correcting field names — it's making sure this data
can never quietly corrupt Model A.** A day's maximum temperature is not
the temperature at any specific hour; writing it into the same
"temperature" measurement CH1/CH2/D2/meteoblue use for hourly point
values would have silently corrupted bias-learning for whatever hour it
got assigned to. Instead, the parsed fields are stored under distinct
measurement names — `temperature_daily_max`, `temperature_daily_min`,
`precip_daily_total`, `wind_speed_daily_avg` — which simply never
participate in Model A's hourly blend at all. This means SRF currently
contributes nothing to Model A's blend or `expert_weight_srf` (which will
show "Unknown" indefinitely, not as a bug but as an accurate reflection
of "this data doesn't fit the hourly bucket system") — a real, open
design question about what to actually do with SRF's daily data (feed it
into the existing daily-aggregation as an independent cross-check?
something else?) rather than something resolved here.

**Still open**: a community-documented example of this same API family
shows day/three_hours/hour arrays can all appear together in one
response — genuine hourly data might exist further into the body than
the 4000-character capture reached (the day array alone, across 5-7 days,
consumed nearly the whole budget). Truncation raised again, to 20000
characters, to check for that possibility if another capture is needed.

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

## v0.1.24–v0.1.28 — architecture notes

Full remediation record: `swissweather_fusion_v0.2.1_release_audit.md`.
The design decisions worth knowing before reading the code:

### Model A blend weights are dimensionless (IND-01)

`blend()` used to give a cold-start source a hard-coded weight of `1.0`
and a trusted source `1 / (ema_abs_error + EMA_WEIGHT_EPSILON)`. Those
two numbers are not on the same scale, and the second carries the
measurement's units:

```
humidity, trusted source with MAE 5%      -> weight 0.20
pressure, trusted source with MAE 0.3 hPa -> weight 3.23
```

against a cold-start weight of `1.0` in both cases. For humidity and
precipitation, every well-characterised source was weighted *below* every
unvalidated one — a source with 200 samples outvoted roughly 5:1 by a
source with one. Learning made the blend worse.

Both weights are now drawn from the same scale: `_reference_weight()`
returns the median learned weight among this blend's trusted
contributors, which is what a cold-start source is worth (neither better
nor worse than typical), and `_clamp_learned_weight()` bounds the ratio
at `MAX_LEARNED_WEIGHT_RATIO` (8:1) so one transiently-lucky bucket
cannot dominate.

The 8:1 cap is judgement, not measurement. It is wide enough to let a
genuinely better source lead decisively while keeping the others audible.

### Model B's radar input is an hourly accumulation (P1-14)

MeteoSwiss's CPC product is "Combiprecip 60-minute total" — millimetres
accumulated over the preceding hour. It is *not* an instantaneous rate;
that is RZC/PRECIP, a different product in the same STAC collection. The
field is `precip_accum_mm_1h` throughout, and the threshold is
`RADAR_PRECIP_ACCUM_MM_THRESHOLD`.

The practical consequence for anyone extending Model B: an hourly
accumulation *lags* convective onset. Distance-graded upwind sampling
still carries real signal, but no arrival-time claim can be attached to
it, which is why the "~20/35/60 min" comments were removed rather than
corrected.

### Asymmetric handling of unknowns in radar gating (P1-13 / P1-16)

Deliberate and worth not "tidying":

- **Freshness unknown → exclude.** A reading whose age cannot be
  established gives no evidence of being current, and a stale echo
  causing a false storm warning costs more than ignoring one point.
- **Quality unknown → include.** The quality code comes from the CPC
  filename, but this project has never verified a real downloaded file.
  If it turns out never to parse, treating unknown as bad would silently
  disable the entire radar signal.

### Crossing state vs displayed value (P0-02)

`_previous_probability` stores the **unrefined base** score;
`current_probability` stays refined for display and history. They must
not be unified: crossing detection compares against the next cycle's
unrefined base, and mixing scales produced a spurious "upward crossing"
on essentially every cycle of any sustained signal.

### Station pressure reference (P1-22)

`CONF_STATION_PRESSURE_IS_SEA_LEVEL` exists because Netatmo publishes
both a normalised `Pressure` and a raw `AbsolutePressure`, and Home
Assistant gives both the same device class. No heuristic can distinguish
them. Default `False` (station-level) — the physically honest reading.
Every provider reports MSL, so without reduction Model A absorbs a fixed
elevation offset as bias.

### Storm-event ground truth (P2-08)

`StormEventReconciliationCoordinator` reuses Model B's *own* v0
thresholds when deciding whether a prediction verified. Inventing a
second definition of "a storm signature" would mean the training labels
described a different phenomenon from the one the model predicts. The
honest reading of `storm_events` is therefore "the v0 signature was
observed", not "a meteorologist would call this a storm".

---


### CombiPrecip: never trust the naming documentation (v0.1.28)

MeteoSwiss documents the CPC filename convention in uppercase. The API
serves lowercase. v0.1.24 encoded the documented form into a
case-sensitive regex and every real file was rejected — radar was dead
for 56 consecutive polls before anyone noticed.

Two rules follow, and `tests/test_v0_1_28_real_fixtures.py` enforces both:

1. **Asset fixtures are copied verbatim from a live capture**, not
   written from the spec. Where documentation and response disagree, only
   the response matters.
2. **Never select a STAC item by `properties.datetime`.** It is an update
   timestamp, not the data date — MeteoSwiss's 8-day reanalysis rewrites
   old files and refreshes it, so a two-week-old item routinely sorts
   first. Items are date-stamped in their id (`YYYYMMDD-ch`); address the
   one you want.

### clients/srf.py is out of scope (v0.1.28)

**Do not modify how the SRF geolocation is resolved.** The API key is
bound to a single registered coordinate, and changing the resolution path
risks invalidating it and requiring a new registration. IND-07
(persisting the geolocation id) is closed as "will not fix" and its
scaffolding deleted, not left dormant.

The v0.1.28 coordinate-pair redaction (SWF-P1-006) touches only what is
written into the diagnostics export. It does not change any request, any
coordinate sent, or the id used at runtime.

### Entities must not touch the database (v0.1.28)

`native_value` is a property Home Assistant polls on the event loop.
Querying SQLite from one is blocking I/O on the loop — the accuracy
sensor did it for four releases. Derived figures belong in the
coordinator that already owns the data and already runs in an executor
job; entities read the cached result.

Relatedly: **no blanket `except Exception` in an entity property.** It
converted a hard `AttributeError` into a silently blank sensor that
looked implemented.


### Forecast parameter registry (v0.2.0)

`forecast_parameters.py` is the single source of truth for every fusable
parameter: its class, unit, bounds, minimum contributing sources and
fusion strategy. Add a parameter there, not in an `if` chain.

**Class A vs Class B is about ground truth, not importance.** Class A
(temperature, humidity, pressure) can be reconciled against the local
station, so the EMA bias machinery applies. Class B cannot, so it gets a
fused *consensus* and no learned correction. Giving a Class B parameter
an `ema_bias` would fabricate a number indistinguishable from a real one.

**Do not default new parameters to the arithmetic mean.** Precipitation
is zero-inflated (mean of [0, 0, 8] invents drizzle no model forecast);
snowfall is near-binary at the margin; gusts are an extreme statistic;
wind bearing is an angle where the linear mean of 350° and 10° is 180°,
exactly backwards. Each parameter declares its own strategy and each has
a test that fails if it reverts to a mean.

### Condition resolution order (v0.2.0)

`resolve_condition()` prefers stated evidence over inference:

1. provider WMO weather code — the model's own considered answer, and
   the only way to reach `fog`, `lightning` or `pouring`
2. explicit snowfall — settles precipitation type without guessing from
   temperature
3. measured cloud cover — replaces the humidity proxy
4. `derive_condition()` — the v0 inference, last resort only

Keep `derive_condition()`. Sources that provide none of the newer fields
still depend on it.

## Known gaps (the honest list, updated for v0.2.0)

**Closed since v0.1.1:**

- `forecast_accuracy` now computes a real sample-count-weighted mean
  absolute error from `bucket_stats.ema_abs_error`, with its methodology
  disclosed in entity attributes (P3-02).
- `last_learning_a` reports the learning coordinator's real heartbeat;
  `last_learning_b` now explicitly discloses that it is not applicable to
  a fixed v0 heuristic and is hidden by default (P3-01).
- `weather.async_forecast_hourly` returns a genuine multi-hour blended
  forecast.
- `storm_events` finally has a writer (P2-08).

**Still open:**

- **`purge_days` on existing installations.** The 90-day default added in
  v0.1.24 applies to new installs only; entries created earlier keep
  `purge_days = 0` ("keep forever"). Deliberately not migrated — silently
  changing a user's retention policy is worse than leaving it — but worth
  setting under Configure.
- **Meteonomiqs as a Model A expert.** Considered and declined for now.
  Its hourly endpoint returns only pressure and precipitation, and Model
  A reconciles temperature/humidity/pressure, so the intersection is
  pressure alone — the measurement where all models already agree. Its
  real strength is short-range radar nowcasting, which is why it feeds
  Model B. The stored `meteonomiqs_*` rows are still unread (see
  IND-10); measuring its pressure error via bucket_stats *without*
  promoting it to the blend is the sensible next step, and turns an
  impression into a number.
- **Repairs and service actions (IND-11).** No `async_create_issue`
  usage, no `services.yaml`. Exhausted quota, a revoked key or an
  oversized database cannot raise a user-visible repair, and there is no
  supported way to force a learning run or reset a poisoned bucket
  without opening the SQLite file by hand. First item for v0.1.25 —
  deferred from v0.1.24 deliberately, because these are new features
  rather than defect fixes and would have expanded the untested surface
  during a release aimed at shrinking it.
- **The elevation lapse-rate pre-correction** is implemented in
  `models/model_a.py` but still not wired into the live blend — it needs
  each source's own grid elevation (`HSURF` for CH1/CH2/D2, `height` for
  meteoblue) threaded through from the forecast responses.
- **CombiPrecip's HDF5 internal layout is unverified** against a real
  downloaded file. The filename/product contract is now grounded in
  MeteoSwiss's published documentation; the in-file group structure is
  not.
- **Cross-source pressure semantics (IND-12).** Open-Meteo requests
  `pressure_msl` and meteoblue maps `sealevelpressure`, both explicitly
  MSL. Whether SRF's `PRESSURE_HPA` is MSL or station-level is
  undocumented. If it is station-level, SRF's learned bias silently
  absorbs a fixed elevation offset — learnable, so nothing breaks
  visibly, but it makes that bias number physically meaningless.
  `srf_probe.py` can settle this with one live response.
- **Dynamic (wind-direction-based) upwind sampling**, wind-direction and
  cloud-cover EMA bucket dimensions, and Model B's winter/frontal
  classifier remain deliberately deferred — see the relevant sections
  above for why each is a "later" decision rather than a gap.
- **Model B v1** is now *possible* (predictions and events both
  accumulate) but not built. The natural first step is extending Model
  A's EMA-learned source weighting to Model B's signal combination,
  replacing the unjustified 50/50 Meteonomiqs blend.
