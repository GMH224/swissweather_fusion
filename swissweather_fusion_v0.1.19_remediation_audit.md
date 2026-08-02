# SwissWeather Fusion — v0.1.19 Remediation Audit

Scope: the fixes applied in v0.1.19 against every finding raised across
three independent v0.1.18 audit reports (two general code audits, one
focused specifically on the `expert_weight_srf: Unknown` symptom).

Prepared: 2026-08-01

Method: each finding was independently re-confirmed against the actual
v0.1.18 source before being fixed (not taken on faith from the audit
reports), the fix was implemented, a regression test was added that
reproduces the original failure mode and proves the fix, and the full
suite was re-run. Test count: 144/144 (v0.1.18 baseline) → **158/158**
(v0.1.19, all passing) — 14 new regression tests added, 0 removed, 0
existing tests weakened.

## Summary table

| # | Finding | Original severity | Status | Fix location |
|---|---|---|---|---|
| 1 | Reconciliation retry watermark drops rows | High (all 3 reports) | **Fixed** | `coordinator.py` `_reconcile` |
| 2 | Meteoblue scheduler can miss every slot | Critical (report 2) | **Fixed** | `clients/meteoblue.py` `is_scheduled_poll_time` |
| 3 | SRF forecastpoint merge drops fields | High (all 3 reports) | **Fixed** | `clients/srf.py` `parse_forecastpoint_response` |
| 4 | SRF daily fallback timestamps not UTC-normalized | High (report 1) | **Fixed** | `clients/srf.py` `parse_forecast_response` |
| 5 | Diagnostics redaction misses coordinate formats | High (report 1) | **Fixed** | `redaction.py` |
| 6 | Open-Meteo dedup ineffective (DEF-02) | High (report 2) | **Fixed** | `clients/open_meteo.py`, `coordinator.py` |
| 7 | Open-Meteo array-length mismatch silent | Medium (report 1) | **Fixed** (visibility) | `clients/open_meteo.py`, `coordinator.py` |
| 8 | Geolocation "closest" claim vs. first-result behavior | Low / follow-up risk (report 2/3) | **Docstring corrected**, behavior unchanged | `clients/srf.py` |
| 9 | Model A doesn't learn precip/wind (SRF weight gap) | Medium (report 1/3) | **Not changed** — documented design gap | `DEVELOPER.md` |
| 10 | Forecast accuracy / Model B training sensors are stubs | Low | **Not changed** — documented, out of scope | — |
| 11 | `SwissWeatherDB` crashes if `.storage/` doesn't yet exist | Not in original audits — found via real HA functional testing | **Fixed** | `storage/db.py` `SwissWeatherDB.__init__` |

---

## 1. Reconciliation retry watermark (High)

**Original claim**: the retry path sets the watermark to the earliest
unreconciled row's `valid_at`, but the query is `valid_at > watermark`
(strict), excluding the very row meant to be retried.

**Independent verification before fixing**: reproduced directly with a
standalone simulation (not just re-reading the code):

```
Pass 1 query: valid_at > 2026-08-01T06:00:00+00:00 <= 2026-08-01T12:00:00+00:00
row in window pass 1: True
new watermark set to exactly the row valid_at: 2026-08-01T11:30:00+00:00
row eligible pass 2 (valid_at > since_ts)? False
```

This is a stronger claim than "delayed retry within `RETRY_GIVE_UP_AGE`"
— the row got **zero** additional retries, not a bounded number. The
`RETRY_GIVE_UP_AGE = timedelta(hours=48)` machinery implied repeated
chances; in practice a row got exactly one chance, ever.

**Fix**: back the new watermark off by one microsecond before the
earliest retryable row's `valid_at`, so it stays on the correct side of
the strict `>` comparison on the next pass, without re-including
anything genuinely already processed (nothing can legitimately sit in a
1-microsecond gap).

**Regression tests added**:
- `test_reconciliation_retries_unmatched_row_on_next_pass` — proves an
  unmatched row is (a) still returned by the reconciliation query on the
  very next pass, and (b) actually gets reconciled once a matching
  station observation later arrives.
- `test_reconciliation_gives_up_on_retry_after_max_age` — confirms the
  give-up-after-48h behavior still works (the fix didn't remove it).

**Test-infrastructure note**: the existing `test_learning_integration.py`
mirror function (`_reconcile_once`) didn't implement the retry-watermark
logic at all before this pass — it unconditionally advanced the
watermark every call. That means this entire code path had **zero** test
coverage in v0.1.18, exactly as the original audits stated ("not covered
by existing tests"). The mirror now matches production logic.

**Practical impact**: this is very likely the primary explanation for
`expert_weight_srf` showing `Unknown` in the reported screenshots despite
healthy SRF polling — any SRF row that missed a station match on its
first reconciliation attempt was silently and permanently excluded going
forward, not genuinely retried.

## 2. Meteoblue scheduler minute-alignment (Critical)

**Original claim**: the coordinator polls every 5 minutes relative to
startup; the gate requires `local_dt.minute == 0`, which those relative
ticks may never land on.

**Independent verification**: confirmed `MeteoblueCoordinator` uses
`update_interval=self.CHECK_INTERVAL` (`timedelta(minutes=5)`) on a
`DataUpdateCoordinator`, which HA implements via `async_track_time_interval`
— fixed-interval-since-creation, not wall-clock aligned. Confirmed
`is_scheduled_poll_time` required exact `minute == 0`. Confirmed the
existing test suite only ever calls `is_scheduled_poll_time` /
`should_fire_scheduled_call` with hand-picked datetimes, never simulating
the actual 5-minute-relative tick sequence — so the misalignment bug
could not have been caught by the pre-v0.1.19 suite.

**Fix**: `is_scheduled_poll_time` is now a whole-hour window check (true
for the entire scheduled hour, any minute). Duplicate-fire prevention
within that hour was already handled separately by
`should_fire_scheduled_call`'s `last_scheduled_call_hour` guard, which
this fix does not touch.

**Regression tests added**:
- `test_scheduled_call_fires_even_when_never_checked_at_minute_zero` —
  simulates a coordinator whose checks land on `:17/:22/:27/...` and
  confirms a scheduled call still fires.
- `test_scheduled_call_fires_only_once_per_hour_at_non_zero_minutes` —
  confirms removing the minute-0 requirement doesn't introduce duplicate
  fires within the same hour.
- Updated `test_is_scheduled_poll_time` (the third assertion previously
  asserted `False` for a non-zero minute; that was asserting the bug).

## 3. SRF forecastpoint merge (High)

**Original claim**: the merge is a dict `.update()` keyed by `valid_at`
only, so `hours` replaces `three_hours`' entire point list for a shared
timestamp, dropping any three_hours-only field.

**Independent verification**: confirmed directly in
`_points_from_hourly_entries` / `parse_forecastpoint_response` — the
merge was exactly `merged.update(_points_from_hourly_entries(...))`,
dict-keyed by `valid_at` alone, value being a flat list per timestamp.

**Fix**: `_points_from_hourly_entries` now returns
`dict[datetime, dict[str, SrfForecastPoint]]` (keyed by variable within
each timestamp). The merge in `parse_forecastpoint_response` combines
per `(valid_at, variable)`: `three_hours` populates first as the base
layer, `hours` then overwrites only the specific variables it itself
provides at that timestamp.

**Regression test added**:
`test_parse_forecastpoint_response_merges_per_field_at_shared_timestamp`
— constructs an `hours` entry missing `PRESSURE_HPA` and a `three_hours`
entry at the same timestamp providing it; confirms `hours`' temperature
wins the real conflict while `three_hours`' pressure survives (would
have been silently dropped pre-fix).

## 4. SRF daily-fallback UTC normalization (High)

**Original claim**: `parse_forecast_response`'s daily fallback only
normalizes naive timestamps to UTC; offset-aware ones keep their
original offset, breaking exact-string matching against the UTC keys
used elsewhere.

**Independent verification**: confirmed the code only did
`if valid_at.tzinfo is None: valid_at = valid_at.replace(tzinfo=timezone.utc)`
with no `else` branch, unlike `_parse_entry_datetime` (used by the
hourly/forecastpoint path) which unconditionally calls
`.astimezone(timezone.utc)`. Confirmed `storage/db.py` does exact ISO
string equality (`valid_at = ?`) and lexical range comparisons
(`valid_at > ? AND valid_at <= ?`), both of which are sensitive to
inconsistent offset representations.

**Fix**: added an `else: valid_at = valid_at.astimezone(timezone.utc)`
branch, matching `_parse_entry_datetime`'s existing behavior.

**Regression test added**:
`test_parse_forecast_response_normalizes_offset_aware_timestamps_to_utc`
— feeds a `+02:00` daily timestamp through the parser and confirms the
result has zero UTC offset and the exact expected UTC ISO string.

## 5. Diagnostics redaction coordinate coverage (High)

**Original claim**: only 3 hardcoded string formats
(`str(value)`/`.4f`/`.2f`) are matched; other decimal precisions or
embedded formats leak through.

**Independent verification**: confirmed `_coordinate_string_variants`
returned exactly those 3 formats.

**Fix**: widened to decimal precisions 2 through 8, plus `str()`/`repr()`,
tried longest-first. Matches are guarded on both sides against an
adjacent digit or decimal point (not just a digit) so a match can't
clip part of a longer, unrelated number.

**A finding from the fix process itself, not the original audits**: an
initial version of this fix also generated 0- and 1-decimal variants
(e.g. `"7"` for a longitude of `7.4474`). A test written specifically to
check for false positives caught that this short variant matched the
leading digits of an unrelated, longer number
(`7.44740001` → `[LON_REDACTED].44740001`). Fixed by raising the minimum
precision to 2 decimals and adding a decimal-point boundary guard on
both sides of the match (not just a digit-boundary guard).

**Regression tests added**: precision coverage (2/3/5/6/8 decimals),
bracketed `[lat, lon]` format, longest-match-first behavior, and a
false-positive guard test (`does_not_clobber_unrelated_longer_number`)
directly encoding the collision found during development.

## 6. Open-Meteo dedup ineffective / DEF-02 (High)

**Original claim**: `issued_at` is always `datetime.now()`, so the
coordinator's `parsed.issued_at <= previous_issued` dedup check can
essentially never suppress a repeated poll.

**Independent verification**: confirmed `issued_at = datetime.now(timezone.utc)`
is set unconditionally on every `parse_forecast_response` call, and the
coordinator's comparison used exactly that field.

**Fix**: added `run_fingerprint` to `ParsedForecast` — a SHA-256 hash of
the response's `time` array plus every mapped hourly variable's value
array (sorted-key JSON, so key ordering doesn't affect it). The
coordinator now dedups on fingerprint equality instead of the
always-advancing `issued_at`.

**Regression tests added**: fingerprint stability across two parses of
an identical payload despite `issued_at` differing between them, and
fingerprint change when the underlying series changes.

## 7. Open-Meteo array-length mismatch visibility (Medium)

**Original claim**: `zip(times, values)` silently truncates to the
shorter array with no signal.

**Independent verification**: confirmed the raw `zip()` call with no
length check anywhere in the function.

**Fix**: added `ParsedForecast.array_length_mismatches`, populated per
variable when its value array length differs from the time axis length.
The coordinator logs a warning and records a `parse_warning` diagnostics
event when this is non-empty. **The truncation behavior itself is
unchanged** — this is visibility only, per the original recommendation
("raise a parse error or record a diagnostic event"); changing the
actual data-shape behavior was judged out of scope for a fix-forward
release given the project's stated preference for graceful degradation
over hard failures on transient upstream issues.

**Regression tests added**: mismatch detection and the (unchanged)
truncation behavior; a companion "no mismatch" test for the normal case.

## 8. Geolocation "closest" claim (Low / follow-up risk)

**Original claim** (report 2, "follow-up risk"; report 3, similar):
`parse_geolocation_response`'s docstring says "take the closest one" but
the implementation takes `results[0]`.

**Independent verification**: confirmed — `first = results[0]` with no
distance comparison anywhere.

**Decision**: corrected the docstring only; did **not** change the
selection logic. None of the three confirmed SRF geolocation response
shapes in this codebase include a documented lat/lon or distance field
per result entry, and every other SRF fix in this project's history
(v0.1.1, v0.1.4, v0.1.8) exists specifically because an earlier version
guessed at an unconfirmed response shape and was wrong. Implementing a
distance calculation now would repeat exactly that mistake. This is
recorded as a follow-up risk requiring a live multi-result capture, not
implemented speculatively.

## 9 & 10. Not changed in this pass

Both are explicitly framed as design decisions / feature gaps (not
correctness defects) by all three source audits:

- Model A only reconciles temperature/humidity/pressure — SRF's
  precip/wind measurements have no local ground truth (no rain/wind
  station sensors) to reconcile against, so they cannot get a learned
  weight regardless of the watermark fix. This means even after fix #1,
  `expert_weight_srf` becoming numeric depends on the *temperature*
  bucket specifically reconciling — the fix addresses the mechanism that
  was silently preventing that, but doesn't add new ground-truth
  measurements.
- `ForecastAccuracySensor` and the Model B training-timestamp sensor are
  intentional stubs (`return None`), flagged Low severity by all three
  reports, with an explicit "implement or hide" recommendation left as a
  product decision rather than a bug fix.

Both are now called out explicitly in `DEVELOPER.md`'s v0.1.19 entry so
they aren't mistaken for having been silently fixed by this release.

---

## 11. Found via functional testing, not the original audits: missing parent-directory creation in `SwissWeatherDB`

While closing the "coordinator.py was never functionally exercised"
test gap (see Verification section below), running the real
`async_setup_entry` against an actual Home Assistant test instance
surfaced a genuine defect none of the three static audits caught:
`SwissWeatherDB.__init__` calls `sqlite3.connect(self._db_path, ...)`
without first ensuring the parent directory exists. `sqlite3.connect()`
does not create missing directories. The real call site
(`__init__.py`) points at HA's `.storage/` directory, which in a normal
production HA install already exists (core creates it very early during
its own startup, before any integration's `async_setup_entry` runs) —
which is exactly why static review never flagged it and why it likely
has never actually bitten a real deployment. But it's still an
unhandled crash path with no defensive check of its own, and it's
inconsistent with the graceful-degradation stance the rest of
`__init__.py` (and this whole project) otherwise goes out of its way to
maintain for every other failure mode. Reproduced directly: a fresh test
`hass.config.config_dir` without a pre-existing `.storage/` raised
`sqlite3.OperationalError: unable to open database file` immediately,
before any coordinator even got the chance to isolate a failure.

**Fix**: `SwissWeatherDB.__init__` now calls
`os.makedirs(parent_dir, exist_ok=True)` before opening the connection.

**Regression test added**: `test_creates_missing_parent_directory` in
`tests/test_db.py` — constructs a `SwissWeatherDB` pointed at a
multi-level nonexistent path and confirms it succeeds and is usable.

## Verification

Closed the test-suite gap documented in `tests/conftest.py`/
`tests/test_syntax.py` (coordinator.py, __init__.py, config_flow.py,
weather.py, sensor.py, and binary_sensor.py were previously only
syntax-checked, never functionally exercised, because `homeassistant`
wasn't installed when this project was built). Installed the real
`homeassistant` package (2025.1.4) and
`pytest-homeassistant-custom-component` in an isolated virtualenv and
ran two additional functional tests against the actual production
classes (not test mirrors):

1. **`ModelALearningCoordinator._reconcile` (the real method)** — same
   retry scenario as the shipped `test_reconciliation_retries_unmatched_row_on_next_pass`
   unit test, but calling the actual production class through a real
   `hass` instance rather than the hand-written mirror. Passed. As a
   control, the fix was temporarily reverted and the same test was
   re-run — it failed with exactly the predicted symptom
   (`watermark == row's valid_at`), confirming the test genuinely
   discriminates between the fixed and buggy behavior rather than
   passing regardless.
2. **Full `async_setup_entry` against a real HA test instance, with
   zero real network access** (this environment cannot reach the actual
   SRF/meteoblue/Open-Meteo/Meteonomiqs/CombiPrecip APIs) — every source
   coordinator's first refresh failed as expected, `__init__.py`'s
   per-source isolation (`asyncio.gather(..., return_exceptions=True)`)
   caught every one of them individually, and setup still completed:
   all 9 coordinators constructed and stored, all `weather`/`sensor`/
   `binary_sensor` entities registered (40+ entities), and
   `async_unload_entry` completed cleanly afterward. This is exactly
   the graceful-degradation behavior `__init__.py`'s own v0.1.1/v0.1.14
   comments claim, now confirmed by actually running it rather than
   reading the code. This run is what surfaced finding #11 above.

Both functional tests live outside the shipped `tests/` directory (they
require the full `homeassistant` package + `pytest-homeassistant-
custom-component`, a heavy dependency this project's own `conftest.py`
deliberately avoided requiring for the fast unit suite) — they were run
as a one-off deeper verification pass for this release, not added to
the standard CI-style suite. The `tests/` directory's own coverage
below remains the fast, dependency-light suite contributors run day to
day.

## Final verification

```
$ python3 -m pytest tests/ -q
........................................................................ [ 45%]
........................................................................ [ 90%]
...............                                                         [100%]
159 passed in 1.50s
```

144 (v0.1.18 baseline) → 159 (v0.1.19 final): 15 new tests, all passing,
0 existing tests removed or weakened.
