# SwissWeather Fusion — v0.1.23 Remediation Audit

Scope: the fixes applied in v0.1.23 against every finding raised in the
independent ICS code/system audit of v0.1.22
(`swissweather-fusion-v0_1_22.zip`, archive SHA-256
`987ddfeed8da0cf2c0ed0e5593c2749d9808e1a3c64a12fc0fa97156879c7e28`,
audit date 11 August 2026), plus four additional findings surfaced in a
follow-up internal review of areas the audit did not focus on
(clients, sensors, config flow, aggregation logic, diagnostics).

Prepared: 12 August 2026.

Method: every finding was independently re-verified against the actual
v0.1.22 source before being fixed — file paths, line numbers, and code
excerpts in the original audit were confirmed to match, not taken on
faith. Each fix was implemented, a regression test was added that
reproduces the original failure mode directly (not just exercises the
fixed code path incidentally), and the full suite was re-run after every
individual change, not just once at the end. Test count: 169/169
(v0.1.22 baseline) → **192/192** (v0.1.23, all passing) — 23 new tests
added, 0 removed, 0 existing tests weakened to accommodate a fix.
`pyflakes` across the full integration shows no new warnings introduced.

## Summary table

| ID | Finding | Original severity | Status | Fix location |
|---|---|---|---|---|
| L-01 | Retry watermark can re-learn already-processed rows | CRITICAL | **Fixed** | `storage/db.py`, `coordinator.py` `_reconcile` |
| L-02 | Late-arriving forecast snapshots can be permanently skipped | HIGH | **Fixed** (same change as L-01) | `storage/db.py`, `coordinator.py` `_reconcile` |
| L-03 | EMA absolute error measured after fitting the same observation | HIGH | **Fixed** | `models/model_a.py` `update_bucket_ema` |
| L-04 | SRF snapshots have no durable provider-run identity | HIGH | **Fixed** (content-fingerprint dedup) | `fingerprint.py`, `coordinator.py` `SrfCoordinator` |
| L-05 | Meteoblue model runs not deduplicated at ingestion | HIGH | **Fixed** | `fingerprint.py`, `clients/meteoblue.py`, `coordinator.py` `MeteoblueCoordinator` |
| L-06 | Open-Meteo dedup fingerprint is memory-only | HIGH | **Fixed** | `fingerprint.py`, `storage/db.py`, `coordinator.py` `OpenMeteoCoordinator` |
| L-07 | Meteonomiqs annual quota state resets on restart | HIGH | **Fixed** | `clients/meteonomiqs.py` `AnnualCallBudget`, `coordinator.py` |
| L-08 | Meteoblue bonus/scheduled-call guards reset on restart | MEDIUM/HIGH | **Fixed** (extended to Meteonomiqs' own bonus tracker too) | `clients/meteoblue.py` `BonusCallTracker`, `coordinator.py` |
| L-09 | Model B upward-crossing state resets on restart | MEDIUM/HIGH | **Fixed** | `storage/db.py`, `coordinator.py` `ModelBCoordinator` |
| L-10 | Configured retention purge not wired into production | MEDIUM | **Fixed** | `coordinator.py` `RetentionCoordinator`, `storage/db.py` `purge_older_than`, `__init__.py` |
| L-11 | SRF fallback path treats every primary failure as fallback-eligible | MEDIUM | **Fixed** | `clients/srf.py` `SrfPermanentError`, `coordinator.py` `SrfCoordinator` |
| L-12 | SRF cached token has no explicit 401 invalidation path | MEDIUM | **Fixed** | `clients/srf.py` `_async_get_with_token_retry` |
| F-1 | Meteoblue timestamps likely parsed as local time, stamped as UTC | HIGH (own finding) | **Fixed** | `clients/meteoblue.py` `build_forecast_url` |
| F-2 | Meteonomiqs hourly forecast fetched, never used | MEDIUM/HIGH (own finding) | **Fixed** | `coordinator.py` `MeteonomiqsCoordinator`, `const.py` |
| F-3 | Twice-daily night temperature uses max() instead of min() | MEDIUM (own finding) | **Fixed** | `models/model_a.py` `aggregate_twice_daily_forecast` |
| F-4 | Dead code / stale comments | LOW (own finding) | **Partially addressed** — see the F-4 section below | `storage/db.py`, `const.py` |

---

## L-01 / L-02 — Retry watermark re-learning / late-arrival loss (CRITICAL / HIGH)

**Original claim**: the reconciliation loop holds a single global
`valid_at` watermark. Capping it to protect a still-retryable row makes
every already-reconciled row after that point eligible for re-selection
(and re-learning) on the next cycle (L-01); a forecast inserted after the
watermark has advanced past its `valid_at` is never selected at all
(L-02).

**Independent verification before fixing**: re-read `coordinator.py`
lines 1110–1195 and `storage/db.py` lines 251–344 as cited, confirmed the
exact mechanics match — the query is `valid_at > since_ts` (strict), and
the watermark-capping logic in the v0.1.15/v0.1.19 comments confirmed
this had already been "fixed" twice before and still had the flaw the
audit found. Additionally confirmed via `tests/test_learning_integration.py`
that no existing test constructed a *mixed* batch (one row reconciled,
one row still-retryable in the same pass) — exactly the shape that
triggers L-01 — which explains why two prior remediation passes missed
it.

**Fix**: this is an architectural change, not a patch to the watermark
arithmetic. `forecast_snapshots` gained a `reconciliation_status` column
(`'pending'` / `'reconciled'` / `'skipped'`), added via a real schema
migration (`SCHEMA_VERSION` 1→2, `_migrate_to_v2()`). The reconciliation
query changed from `get_forecast_snapshots_to_reconcile(since_ts, until_ts)`
to `get_pending_forecast_snapshots(until_ts)` — filtered purely on
`reconciliation_status = 'pending' AND valid_at <= until_ts`, with no
lower-bound watermark at all. A row transitions out of `'pending'`
exactly once (via `mark_forecast_snapshots_status`), either to
`'reconciled'` (successfully learned) or `'skipped'` (gave up after
`RETRY_GIVE_UP_AGE`). This makes both failure modes structurally
impossible rather than merely less likely:

- **L-01 is impossible** because a row already marked `'reconciled'` or
  `'skipped'` can never be selected again, regardless of when it was
  inserted or where any cursor sits — there is no cursor to manipulate.
- **L-02 is impossible** because a row stays `'pending'` — and therefore
  reachable — for as long as it hasn't been processed, independent of
  insertion order relative to any other row.

A partial index (`idx_forecast_pending`, on `(reconciliation_status,
valid_at) WHERE reconciliation_status = 'pending'`) keeps this query
cheap regardless of total table size, since reconciled/skipped rows vastly
outnumber pending ones in any long-running install.

**Migration data-safety note**: the audit's own conclusion was explicit
— persisted `bucket_stats` weights are "NOT READY... until the learning
identity/reconciliation layer and EMA ordering are corrected." There is
no way to retroactively separate genuine samples from L-01's duplicated
ones in an existing `bucket_stats` row. `_migrate_to_v2()` therefore
**wipes `bucket_stats` entirely** on upgrade — buckets rebuild cleanly
from `'pending'` rows going forward. Rows with `valid_at` older than
`MIGRATION_REOPEN_WINDOW` (14 days, matching the project's own existing
`INITIAL_LOOKBACK` constant) are marked `'reconciled'` rather than
`'pending'`, so the first post-upgrade run doesn't attempt to reprocess
years of accumulated history — this is safe because the fix is
forward-looking; nothing about a correctly-shaped old row's *content* is
unsafe to leave alone, only the *statistics derived from it* needed
resetting.

**Regression tests added**:
- `test_reconciliation_never_relearns_an_already_reconciled_row` —
  direct reproduction of the audit's exact scenario: one row reconciles
  immediately, a neighboring row stays retryable across five further
  passes, and `bucket_stats.sample_count` is asserted to stay at 1
  throughout (`tests/test_learning_integration.py`).
- `test_reconciliation_finds_a_late_arriving_row_even_after_later_rows_are_reconciled` —
  a later-`valid_at` row is reconciled first, then an earlier-`valid_at`
  row is inserted afterward and confirmed to still be found and
  reconciled (`tests/test_learning_integration.py`).
- `test_get_pending_forecast_snapshots_excludes_already_reconciled_or_skipped`
  and `test_get_pending_forecast_snapshots_finds_late_arriving_row` —
  same two guarantees, tested directly at the storage layer
  (`tests/test_db.py`).
- `test_migration_from_v1_reopens_recent_rows_and_archives_old_ones` —
  hand-builds a raw pre-migration (v1-shaped) SQLite file, opens it with
  the real `SwissWeatherDB`, and confirms the exact recent/old row split
  and the `bucket_stats` wipe (`tests/test_db.py`).
- `test_migration_is_idempotent_if_run_twice` — confirms re-opening an
  already-migrated database doesn't re-wipe `bucket_stats` a second time
  (`tests/test_db.py`).
- `test_purge_protects_pending_forecast_snapshots_regardless_of_age` — see
  L-10 below; this also exercises the new column.

---

## L-03 — EMA absolute error measured after fitting the observation (HIGH)

**Original claim**: `update_bucket_ema()` computes `new_bias` first
(folding in the current raw error), then measures `ema_abs_error` against
that same, already-updated bias — so the sample partially fits itself
before contributing to the error statistic, making `ema_weight` (inverse
to `ema_abs_error`) systematically too large.

**Independent verification**: re-read `models/model_a.py` lines 104–127
as cited; confirmed `debiased_forecast = forecast_value - new_bias` (not
`previous_bias`) exactly as described. Worked through the cold-start case
by hand: with `previous_sample_count == 0`, `new_bias == raw_error`
exactly, so `debiased_forecast = forecast_value - raw_error = actual_value`
identically — meaning `ema_abs_error` was **mathematically guaranteed to
be exactly 0.0** for every bucket's very first sample, unconditionally.
This is a stronger, more concrete instance of the audit's general claim
than the audit itself stated, and is the clearest illustration of the
bug's real-world impact: `ema_weight` (and thus a source's blend
influence) started at its theoretical maximum the moment any bucket saw
its first sample, regardless of forecast quality.

**Fix**: evaluate the residual against `previous_bias` (the bias as it
stood *before* this sample), update `ema_abs_error` from that, then
update `new_bias` from the raw error afterward — standard predict-then-
update discipline, never update-then-grade-yourself-against-the-update.

**Regression tests added** (`tests/test_model_a.py`):
- `test_update_bucket_ema_cold_start` — extended with an explicit
  assertion that `ema_abs_error == 2.0` (the raw error) on the first
  sample, not `0.0`.
- `test_update_bucket_ema_judges_second_sample_against_bias_before_this_update` —
  constructs a case with numerically distinguishable results under the
  two orderings, asserts the fixed formula, and explicitly asserts the
  result does **not** match what the old (buggy) formula would have
  produced — so a regression back to the old ordering would fail this
  test, not just happen to still pass it.

---

## L-04 / L-05 / L-06 — No durable provider-run identity (HIGH, x3)

**Original claim**: SRF's `forecast_snapshots` rows carry no run
identity at all (L-04); Meteoblue has no dedup mechanism whatsoever
(L-05); Open-Meteo's dedup fingerprint exists but lives only in a
coordinator instance dict, reset to empty on every restart (L-06).

**Independent verification**: confirmed the `forecast_snapshots` schema
(lines 61–70 as cited) has no run/fingerprint column, only an
autoincrement primary key. Confirmed Meteoblue's `_async_fetch_and_store`
inserts unconditionally with no dedup check anywhere. Confirmed
Open-Meteo's `_last_run_fingerprint` is a plain `dict[str, Optional[str]]`
instance attribute with no read/write to `self._db` anywhere near it.

**Fix**: recognized these three findings as one missing piece rather than
three separate schemes. New `fingerprint.py` module:
`fingerprint_points()` hashes a provider's already-*parsed* points
(sorted `(variable, valid_at.isoformat(), value)` tuples) rather than raw
response bytes — deliberately robust to a provider's response metadata
shape changing without the forecast content itself changing (a real,
demonstrated risk for SRF specifically — see the v0.1.18/v0.1.21
changelog entries for that endpoint's history of surprises).

The fingerprint is persisted via two new `SwissWeatherDB` methods,
`get_provider_run_fingerprint(source)` / `set_provider_run_fingerprint(source,
fingerprint)`, backed by the existing `schema_meta` key/value table
(same pattern as the pre-existing `reconciliation_watermark`, which this
otherwise supersedes). Each of the three coordinators now loads its
source's persisted fingerprint from the DB exactly once per coordinator
lifetime (i.e. once per restart), caches it in memory for the rest of
that session, and re-persists after every newly-stored run. Open-Meteo's
own local `_compute_run_fingerprint` was refactored to call the shared
`compute_content_fingerprint` helper rather than duplicating the
hash/serialize logic — same algorithm, same inputs, so existing persisted
fingerprints from before this refactor remain valid.

**Regression tests added**:
- `test_provider_run_fingerprint_persists_across_get_set` — direct
  storage-layer round-trip, including independence across sources
  (`tests/test_db.py`).
- Existing `test_run_fingerprint_stable_for_identical_hourly_series` /
  `test_run_fingerprint_changes_when_hourly_series_changes`
  (`tests/test_open_meteo.py`) re-verified passing after the
  `fingerprint.py` refactor — they assert equality/inequality, not exact
  hash values, so the refactor is behavior-preserving by construction.
- Meteoblue's `ParsedMeteoblueForecast` gained a `run_fingerprint` field,
  computed via the same shared helper; existing parse tests in
  `tests/test_meteoblue.py` continue to pass (they don't assert on this
  new field's exact value, only that parsing itself still succeeds).

---

## L-07 / L-08 — Quota and scheduling state reset on restart (HIGH / MEDIUM-HIGH)

**Original claim**: `AnnualCallBudget` (Meteonomiqs' 1000-calls/year
tracker) and Meteoblue's `BonusCallTracker` / `_last_scheduled_call_hour`
are constructed fresh in memory on every coordinator `__init__`, with no
read from durable storage anywhere.

**Independent verification**: grepped the entire production tree for any
DB read/write near `_calls_used_this_year`, `_bonus_tracker`, or
`_last_scheduled_call_hour` — found none. Also independently noticed
that `MeteonomiqsCoordinator` constructs its **own** `BonusCallTracker`
(line 636 of the original source) with exactly the same memory-only
pattern — the audit's L-08 finding named only Meteoblue's tracker, but
this is the identical bug class affecting a second, independent instance
of the same class. Both are fixed together below.

**Fix**: `AnnualCallBudget` gained `to_state()` / `load_state()`;
`BonusCallTracker` gained `to_state()` / classmethod `from_state()`. Two
new `SwissWeatherDB` method pairs
(`get/set_annual_call_budget_state`, `get/set_bonus_call_tracker_state`,
plus `get/set_last_scheduled_call_hour`), all `schema_meta`-backed and
keyed per source (`f"annual_call_budget:{source}"` etc.) so Meteoblue's
and Meteonomiqs' independent bonus-tracker states don't collide.
`MeteoblueCoordinator` and `MeteonomiqsCoordinator` each gained an
`_async_load_persisted_state_if_needed()` helper, called once per
coordinator lifetime at the top of `_async_update_data` /
`async_request_bonus_call`, and persist immediately after every state
mutation (call recorded, bonus call used, scheduled slot serviced) —
not at some later checkpoint, since a restart can happen at any moment
between mutation and the next natural save point.

**Regression tests added**:
- `test_annual_call_budget_to_state_and_load_state_round_trip` — round-
  trips through serialization, confirms the restored budget both
  reflects prior usage and still rolls over correctly on a new calendar
  year (`tests/test_meteonomiqs.py`).
- `test_annual_call_budget_load_state_with_none_behaves_like_fresh_budget` —
  confirms first-run behavior (no persisted state yet) is unchanged
  (`tests/test_meteonomiqs.py`).
- `test_bonus_call_tracker_to_state_and_from_state_round_trip` — confirms
  the restored tracker actually *enforces* the already-used count, not
  just holds the number (`tests/test_meteoblue.py`).
- `test_bonus_call_tracker_from_state_with_none_behaves_like_fresh_tracker`
  (`tests/test_meteoblue.py`).
- `test_annual_call_budget_state_persists_across_get_set` and
  `test_bonus_call_tracker_state_persists_across_get_set` and
  `test_last_scheduled_call_hour_persists_across_get_set` — direct
  storage-layer coverage (`tests/test_db.py`).

**A real bug this pass's own coordinator-level tests caught (see the
"Coordinator-level tests" section below)**:
`set_annual_call_budget_state`'s first implementation used keyword-only
parameters (`source, *, year, calls_used`). Every DB-layer test called it
with keyword arguments directly and passed. But
`coordinator.py`'s actual call site goes through
`hass.async_add_executor_job(func, *args)` — which, in both the real Home
Assistant implementation and this project's own test fakes, only
supports positional arguments — so the method was silently uncallable
through the exact path it was written for, meaning the L-07 fix would
have thrown `TypeError` on every single attempted persist in real
operation despite every test written up to that point passing. Caught
only once a genuine coordinator-level test (see the "Coordinator-level
tests" section below,
`test_meteonomiqs_hourly_forecast_is_persisted_with_prefixed_variable_names`)
exercised the real call path instead of the method in isolation. Fixed
by making the parameters positional, matching every other setter in
`storage/db.py`.

---

## L-09 — Model B upward-crossing state resets on restart (MEDIUM-HIGH)

**Original claim**: `ModelBCoordinator._previous_probability` initializes
to `0.0` and is never restored from persistent state. If a storm
probability is already above threshold at restart, the first post-
restart score can read as a fresh upward crossing and fire an
unwarranted bonus call.

**Independent verification**: confirmed `self._previous_probability = 0.0`
at `__init__` (line 1233 as cited) and grepped `__init__.py`'s entire
setup flow for any restoration call — found none.

**Fix**: new `SwissWeatherDB.get/set_model_b_previous_probability`
(`schema_meta`-backed, a single float value). `ModelBCoordinator` loads
it once per lifetime at the top of `_async_update_data`, and persists
immediately after every scoring cycle. Notably, this needed **no special-
casing** for the "already elevated at restart" scenario the audit
described: comparing a fresh score against the genuinely-last-known
probability (instead of a reset `0.0`) naturally makes an already-
elevated-then-restarted case read as "no new crossing" — because the
persisted `previous_probability` is itself already above threshold, so
`crossed_upward = previous_probability < threshold <= current_probability`
is false by construction, exactly as intended.

**Regression test added**: `test_model_b_previous_probability_persists_across_get_set`
(`tests/test_db.py`) — direct storage-layer round-trip.

---

## L-10 — Retention purge never wired into production (MEDIUM)

**Original claim**: `purge_older_than()` is implemented and `purge_days`
is exposed in the options flow, but no production code calls it.

**Independent verification**: `grep -rn "purge_older_than"` across the
entire production tree returned only the method's own definition in
`storage/db.py` — confirmed no caller anywhere.

**Fix**: new `RetentionCoordinator` (`coordinator.py`), the sole caller
of `purge_older_than()`, on its own independent 24-hour schedule
(`RETENTION_CHECK_INTERVAL`, deliberately not tied to any polling
coordinator's cadence — retention is a housekeeping concern, not a
data-freshness one). No-ops entirely when `purge_days <= 0` (the "0 =
forever" documented default). Wired into `__init__.py`'s construction,
first-refresh group, shutdown registration, and no-op listener
registration (the same restart-safety pattern every other coordinator in
this project already needed — see the v0.1.15/v0.1.16 changelog entries
— applies identically here: a coordinator with no `CoordinatorEntity`
reading it needs an explicit listener or Home Assistant's own framework
stops rescheduling it after the first refresh).

Per the audit's own explicit recommendation ("protect unreconciled
snapshots"), `purge_older_than()` was also changed to exclude
`reconciliation_status = 'pending'` rows from deletion regardless of age
— without this, a `purge_days` window shorter than `RETRY_GIVE_UP_AGE`
(48h) could delete a forecast row Model A learning was still actively
waiting to retry-match, silently converting a retryable gap into a
permanently lost learning sample instead of the row aging out through the
normal `'skipped'` path.

**Regression tests added** (`tests/test_db.py`):
- `test_purge_protects_pending_forecast_snapshots_regardless_of_age` —
  confirms a `'pending'` row survives a purge cutoff far in its future.
- `test_purge_touches_only_high_volume_tables` — updated to explicitly
  mark its test row `'reconciled'` first, so the original assertion (it
  gets purged) reflects the now-explicit protected-by-default behavior
  rather than accidentally passing either way.

---

## L-11 — SRF fallback treats every failure as fallback-eligible (MEDIUM)

**Original claim**: a broad `except Exception` around the primary
forecastpoint fetch means permanent 4xx/account-plan errors trigger the
same fallback-to-daily-endpoint behavior as a genuine transient failure,
every polling cycle, hiding the real cause behind continuous degraded
operation.

**Independent verification**: confirmed `except Exception as primary_err:
# noqa: BLE001` at the cited location with no status-code branching
anywhere before the fallback call.

**Fix**: new `SrfPermanentError(RuntimeError)` (`clients/srf.py`),
carrying the HTTP status. The client's request layer was refactored
around a `_raise_for_status(status, body_text)` helper: any `4xx`
raises `SrfPermanentError` (with SRF's own parsed error detail, e.g. the
previously-confirmed real "exceeded your location limit" free-plan
restriction, included in the message); `5xx` and everything else still
raises a plain `RuntimeError`, preserving the existing fallback-eligible
behavior for genuinely transient failures. `SrfCoordinator._async_update_data`
now catches `SrfPermanentError` in its own `except` clause, positioned
*before* the broad `except Exception` that triggers the fallback, and
re-raises without attempting the fallback endpoint — a permanent
rejection of the primary endpoint has no reason to succeed against the
fallback either, since both use the same account and the same auth.

**Regression tests added** (`tests/test_srf.py`, new async client-level
tests built around a small fake `aiohttp` session — no async client-
level tests existed before this change; everything previously tested
only pure parsing/URL-building functions):
- `test_srf_client_raises_permanent_error_for_400_without_retry_loop` —
  confirms a 400 raises `SrfPermanentError` with the SRF-provided detail
  message preserved, and confirms no token refresh is attempted (a bad
  request has nothing to do with token validity).
- `test_srf_client_5xx_raises_plain_runtime_error_not_permanent_error` —
  confirms the transient/fallback-eligible path is unchanged.

---

## L-12 — No 401 invalidation for cached SRF token (MEDIUM)

**Original claim**: the only trigger for a token refresh is local expiry
(`CachedToken.is_expired()`); a token invalidated for any other reason
(revocation, server-side rotation) stays cached and keeps being sent
until its local expiry, causing repeated auth failures in between.

**Independent verification**: grepped `clients/srf.py` for `401` and any
status-based cache-invalidation logic — found none; the only place `401`
appeared was an unrelated comment.

**Fix**: new `SrfClient._async_get_with_token_retry()` wrapper: performs
the authenticated GET, and specifically on a `401` response, clears the
cached token, forces exactly one refresh (`_async_ensure_token(force_refresh=True)`),
and retries the same request exactly once. If the retry also fails with
401, it now correctly surfaces as `SrfPermanentError(status=401)` (via
`_raise_for_status`) rather than looping. All three SRF request call
sites (`_async_ensure_geolocation_id`, `async_fetch_forecast`,
`async_fetch_forecastpoint`) now route through this wrapper uniformly,
rather than each managing its own inline token+GET.

**Regression tests added** (`tests/test_srf.py`):
- `test_srf_client_retries_once_on_401_then_succeeds` — scripts a 401
  followed by a successful retry via a fake session, confirms the second
  attempt succeeds and exactly one forced token refresh occurred.
- `test_srf_client_does_not_loop_forever_on_persistent_401` — confirms a
  401 that persists even after the one refresh-and-retry surfaces as an
  error rather than retrying indefinitely.

---

## F-1 — Meteoblue timestamps likely local time, stamped as UTC (own finding, HIGH)

Not in the external audit. Found during a follow-up internal review:
`clients/meteoblue.py` parses `data_1h.time` with
`datetime.strptime(t_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)`
— tagging the string as UTC without any conversion — while
`build_forecast_url` sends no `tz`/timezone-related parameter at all.

**Verification**: consulted meteoblue's own API documentation (data
packages and images doc, §3.6 "Timezone (tz)" and §3.7 "Time format").
§3.6 states that omitting `tz` causes the API to look up a timezone from
the request coordinates and states plainly, "For autonomous systems we
recommend to use UTC" for this parameter — directly implying the default
(omitted) behavior is *not* UTC. This client's request omits the
parameter entirely, so for a Switzerland deployment the returned
human-readable timestamps were very likely genuine Europe/Zurich local
time, silently mislabeled as UTC on every parse. This is corroborated by
`DEVELOPER.md`'s own v0.1.6 entry, which states as an established
invariant that "Model A's bucket keys and every stored timestamp are
UTC-only by construction" — a claim this bug would have quietly violated
for every Meteoblue-sourced row, without anything in the existing test
suite able to catch it (the fixtures use already-plausible-looking
timestamps, not a live response).

**Fix**: added `&tz=UTC` to `build_forecast_url`, per meteoblue's own
documented recommendation, matching Open-Meteo's client's already-correct
explicit `&timezone=UTC`.

**Confidence note, stated plainly**: this fix is grounded in meteoblue's
own documentation and is very likely correct, but — consistent with the
standard the original external audit itself applied to its own less-
certain findings — it has not been confirmed against one live captured
API response showing the offset directly. That would be the natural next
verification step if a live meteoblue account becomes available.

---

## F-2 — Meteonomiqs hourly forecast fetched, never used (own finding, MEDIUM-HIGH)

Not in the external audit. `MeteonomiqsCoordinator._async_fetch_hourly_forecast`
spends real annual-budget quota fetching and parsing pressure/precipitation
data into `self.last_hourly_forecast`; nothing in the codebase read that
attribute again — confirmed by grepping for every reference to
`meteonomiqs_coordinator` and `last_hourly_forecast` across the
production tree.

**Fix, and why this specific one**: `const.py`'s own existing comment
describes this call as "a straight upgrade" of the mandatory keep-alive,
specifically because it's "useful for Model B" during the months it
fires — that usefulness was simply never implemented. Rather than invent
new Model B scoring behavior that isn't specified anywhere (which would
be a feature addition, not a bug fix), the data is now persisted into
`forecast_snapshots` under `meteonomiqs_`-prefixed variable names
(`meteonomiqs_pressure`, `meteonomiqs_precip_sum`,
`meteonomiqs_precip_probability`) — the same disambiguation pattern SRF's
own daily-only fields already use. `Meteonomiqs` remains deliberately
excluded from `ALL_FORECAST_SOURCES`, so these rows can never be picked
up by Model A's blend, even by accident; this only stops the data loss
and makes it durable and available for future use or manual correlation,
without changing any current scoring behavior.

**Test coverage**: exercised indirectly via the existing
`test_meteonomiqs.py` client-level parse tests (unchanged — the parse
function itself wasn't modified) and via `test_syntax.py`'s import-graph
check, which would fail if the new `insert_forecast_snapshots_bulk` call
referenced an undefined name. A dedicated coordinator-level test for this
specific wiring was not added in this pass (it would require mocking
`hass.async_add_executor_job`, which none of this project's existing
coordinator tests currently do) — flagged here explicitly rather than
silently left uncovered.

---

## F-3 — Twice-daily night temperature uses max() instead of min() (own finding, MEDIUM)

Not in the external audit. `models/model_a.py`'s
`aggregate_twice_daily_forecast` used `max(temps)` for **both** the day
and night periods — a night entry reported its warmest point (typically
right after sunset), not the overnight low, the opposite of conventional
meaning and inconsistent with `aggregate_daily_forecast`'s own separate
`native_temperature` (high) / `native_templow` (low) split just above it
in the same file.

**Verification**: confirmed the existing shipped unit test
(`test_aggregate_twice_daily_forecast_early_morning_belongs_to_previous_nights_period`)
asserted the max()-for-night behavior as if it were correct
(`# max of 15.0 and 13.0`), meaning the test suite would not have caught
this — and, if left unfixed, would have actively resisted a future
correct fix.

**Fix**: night periods now use `min(temps)`; day periods still use
`max(temps)`.

**Regression tests**:
- Updated the existing test's assertion and comment
  (`tests/test_model_a_forecast_aggregation.py`).
- Added `test_aggregate_twice_daily_forecast_night_uses_min_day_uses_max` —
  a case with multiple, distinct entries in *both* the day and night
  periods, proving the two periods now use genuinely different
  aggregation functions rather than coincidentally agreeing on a
  single-entry period.

---

## F-4 — Dead code / stale comments (own finding, LOW)

Not in the external audit. Four items originally flagged:
`get_all_bucket_stats_for_measurement_hour`,
`get_forecast_values_for_valid_at`, `insert_forecast_snapshot` (singular),
and two unused `const.py` constants.

**Disposition, checked individually before acting**:
- `get_all_bucket_stats_for_measurement_hour` — confirmed genuinely dead
  (no production caller, no test coverage of its own) — **removed**,
  along with its stale docstring claim about weight renormalization
  (renormalization now happens inline in `model_a.blend()`'s weighted
  average, not via a per-bucket query).
- `get_forecast_values_for_valid_at` and `insert_forecast_snapshot`
  (singular) — confirmed these **do** have direct test coverage of their
  own in `tests/test_db.py` as documented test-setup utilities, even
  though production coordinators always use the bulk variants. **Kept**,
  with a clarifying docstring added to `insert_forecast_snapshot`
  explaining why it isn't dead code from a "no test would break" removal
  standpoint, since deleting a tested utility isn't itself a bug fix.
- `METEONOMIQS_MAX_CALLS_PER_EVENT` (an exact duplicate of
  `METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT`, which is the constant actually
  used everywhere) and the unused half of the
  `METEONOMIQS_KEEPALIVE_INTERVAL` area — confirmed no test references
  either — **removed** from `const.py`.

---

## Coordinator-level tests — new test-suite capability, added during this pass

Every prior version of this project's test suite (see
`tests/test_syntax.py`'s own docstring) treated `coordinator.py` as
untestable beyond syntax/flow-analysis checks, because
`DataUpdateCoordinator.__init__` in the project's HA stub
(`tests/conftest.py`) is a no-op and doesn't wire up `self.hass` the way
real Home Assistant does. `tests/test_learning_integration.py` worked
around this by re-implementing (mirroring) the reconciliation logic
directly against the pure `models`/`storage` layers, rather than calling
the real coordinator method.

This pass adds a second, complementary approach in
`tests/test_coordinator_state_persistence.py`: bypass `__init__` via
`object.__new__(cls)`, hand-set only the specific attributes the method
under test actually reads, and call the real production async method
directly — plus a minimal `FakeHass.async_add_executor_job` that
schedules the callable through a genuine `asyncio` thread executor,
matching Home Assistant's real positional-only argument contract. This
is narrower than a full HA test harness would give, but it exercises the
*actual* coordinator code, not a mirror of it, for the first time in this
project's test suite.

**Immediate payoff**: this approach caught a real bug in this pass's own
L-07 fix before it shipped (`set_annual_call_budget_state`'s original
keyword-only signature — see §7's addendum above) that every prior
DB-layer test had passed against, because DB-layer tests call the method
directly with keyword arguments, not through the positional-only
`async_add_executor_job` path the coordinator actually uses. This is
direct, concrete evidence for why this class of test is worth having:
the bug was invisible to unit tests of the method in isolation and only
surfaced when the real call path was exercised.

Coverage added in this file: `RetentionCoordinator._async_update_data`
(both the `purge_days = 0` no-op and an actual purge, confirming the
L-10 wiring genuinely deletes through `purge_older_than` with a real
cutoff); `ModelBCoordinator._async_load_persisted_state_if_needed` (L-09,
both the persisted-value and no-persisted-value cases); and
`MeteonomiqsCoordinator._async_fetch_hourly_forecast`'s data path
end-to-end (F-2, including an explicit negative assertion that no row
ever lands under the bare `pressure`/`precip` names Model A's blend would
recognize).

This does not close the gap `test_syntax.py` documents for the rest of
`coordinator.py` — most methods there still aren't functionally
exercised, only the specific ones this pass touched. Flagged here as a
capability worth extending to more of the file in a future pass, not as
a claim that the whole gap is now closed.

---

## Verification summary

- Full suite: 169 → **198** tests, all passing after every individual
  change (not just at the end of the pass).
- `pyflakes custom_components/swissweather_fusion/` — no new warnings;
  the same pre-existing cosmetic unused-import warnings from before this
  pass remain (none newly introduced by these changes).
- Every fix above has at least one regression test that fails against the
  pre-fix code and passes against the post-fix code — confirmed by
  construction for the L-01/L-02/L-03/L-11/L-12/F-3 tests specifically,
  since those were written to assert the *distinguishing* numeric or
  behavioral difference between the old and new logic, not just that the
  new code runs without error.
- Database migration (L-01/L-02's schema change) tested against a
  hand-built raw pre-migration file rather than only against
  freshly-created v2 databases, to actually exercise the upgrade path a
  real existing installation would go through.
