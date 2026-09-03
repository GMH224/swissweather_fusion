# SwissWeather Fusion — v0.1.24 → v0.1.28 Remediation Audit

**Release:** v0.1.23 → v0.1.28
**Scope:** all 63 findings from the consolidated defect log — 50 from the
second external ICS-style audit, 13 from an independent audit performed
during triage.

**Result:** 62 defects fixed, 1 audit finding refuted and deliberately
left unchanged. Test suite 198 → **469 passing**. pyflakes **clean**
(the five long-standing unused-import warnings were also cleared).

> **v0.1.28** closes seven further defects found by running the
> integration against live APIs for the first time — including a total
> CombiPrecip outage. See §9. It is the first release validated against
> captured real provider responses rather than published documentation.
>
> **v0.1.27** closes six defects found by a third external
> ICS-quality audit run against the v0.1.26 package — three P1 (two of
> them introduced by this very remediation) and three P2. See §8.
>
> **v0.1.26 was the first build that actually loads.** The defect content
> of the remediation is unchanged from v0.1.24; v0.1.25 and v0.1.26 fix
> three setup-blocking bugs introduced *by* that remediation, and each
> removes the structural gap that allowed it. See §7.1, §7.2 and §7.3 —
> all three are documented in full, including why a 361-test suite passed
> with two of them in place.

---

## 1. What changed at a glance

| | v0.1.23 | v0.1.24 |
| --- | --- | --- |
| Tests | 198 | 469 |
| pyflakes warnings | 5 | 0 |
| Files with executable test coverage | pure-logic modules only | + coordinator, config flow, entities, lifecycle |
| Database schema | v2 | **v3 (rebuild — see §3)** |
| Config entry version | 1 | **2 (migration handler added)** |
| Production callers of `insert_storm_event` | **0** | 1 |
| Coordinators | 10 | 11 |

---

## 2. Product decisions taken during this pass

Five questions could not be answered from the code. Each was decided
explicitly and the reasoning lives in the source, not only here.

### 2.1 CombiPrecip: keep CPC, fix the semantics properly

MeteoSwiss's open-data documentation is unambiguous:

| Product | Long name | Unit | Temporal aggregation |
| --- | --- | --- | --- |
| `CPC` | Combiprecip 60-minute total | mm | accumulation over 1 hour |
| `RZC` | PRECIP | mm/h | instantaneous rain rate |

This project fetches CPC while calling the value `precip_rate_mmh` and
comparing it against a threshold chosen as if it were a rate.

**Decision: stay on CPC, but correct the semantics through the whole
stack** rather than documenting around them. `precip_rate_mmh` is renamed
to `precip_accum_mm_1h` in `RadarPixelValue`, `RadarPointReading` and the
`radar_observations` column; the constant became
`RADAR_PRECIP_ACCUM_MM_THRESHOLD`.

Switching to RZC was seriously considered — it is genuinely an
instantaneous rate, which would make the existing threshold semantics
correct as written and arguably suits nowcasting better. It was rejected
because CPC's gauge correction is a real accuracy advantage, and because
a product switch cannot be validated without a real downloaded file,
which this project still lacks.

The earlier reconstruction document proposed a documentation-only fix
here. That was judged insufficient: the threshold's *numeric meaning* was
wrong, not just its label.

### 2.2 Station pressure: default to station-level, and ask

Netatmo — the reference station for this project — publishes **both**
values. `Pressure` is normalised to mean sea level using the altitude its
app captures via GPS during setup; `AbsolutePressure` is the raw
measurement. Home Assistant's Netatmo integration exposes both, and both
carry `device_class: atmospheric_pressure`, so the entity selector cannot
distinguish them and neither can any runtime heuristic.

**Decision:** ask explicitly via `CONF_STATION_PRESSURE_IS_SEA_LEVEL`,
defaulting to `False` (station-level, needs reduction) — the physically
honest reading and the one this project's own installation uses. Every
forecast provider reports MSL (`pressure_msl`, `sealevelpressure`), so
without reduction Model A absorbs a fixed elevation-dependent offset as
"bias" — roughly 60 hPa at 500 m.

This new key is what forced the config-entry migration handler (IND-05).

### 2.3 Learned history: discard and relearn

Confirmed with the maintainer that no meaningful learning history exists
yet. That unlocked the honest option rather than the compatible one.

Three of this release's fixes change what stored data *means*:

1. **IND-01** changes how learned weights relate to one another. Every
   stored `ema_weight` was produced on a unit-dependent scale the new
   blend does not use.
2. **P1-14** changes what the radar value *is*. Accumulated millimetres
   and an instantaneous rate are not inter-convertible.
3. **P0-01 / P0-02** mean some historical reconciliation results and
   storm predictions were produced by logic now known to be wrong
   (double-counted EMA samples; spurious repeated crossings).

Carrying any of that forward would poison the corrected models with data
generated by the uncorrected ones. See §3.

### 2.4 Retention: 90 days, asked at setup

`DEFAULT_PURGE_DAYS` was `0` — keep forever — and the setup flow never
asked. 90 days is comfortably longer than both the 14-day learning
warm-up and the 168-hour forecast horizon, so nothing the models consume
is ever purged from under them. `0` still means forever, now as an
explicit opt-in.

### 2.5 The reconstruction document was used as reference, not specification

It is demonstrably wrong in five places (§6). Every fix here was
re-derived against the actual source. Where its design was sound — the
atomic reconciliation batch, the shared lock, the storm reconciliation
coordinator — that design was followed and credited.

---

## 3. Schema v3 — a rebuild, not a migration

`_migrate_to_v3()` discards and recreates the **derived** tables while
preserving the **factual** ones.

| Table | Action | Why |
| --- | --- | --- |
| `forecast_snapshots` | **Preserved**, recent rows re-opened as `pending` | Raw provider forecasts are facts |
| `station_observations` | **Preserved** | Raw sensor readings are facts |
| `bucket_stats` | **Cleared** | Weights were on a scale the new blend does not use (IND-01) |
| `radar_observations` | **Dropped and rebuilt** | Column renamed; old values mean a different physical quantity (P1-14) |
| `storm_predictions` | **Dropped and rebuilt** | Gained `reconciled`; old rows contain P0-02 duplicates |
| `storm_events` | **Dropped and rebuilt** | Was always empty — it had no writer |

Because the learning loop rebuilds `bucket_stats` from the preserved
`forecast_snapshots`, the real cost is a warm-up period, not lost
history. A warning is logged at migration explaining exactly what is
discarded and why.

Migration detection itself was rewritten (P2-01): it now inspects actual
table shape via `PRAGMA table_info` rather than trusting the presence of
a `schema_meta` row, with metadata absence demoted to a secondary signal.

---

## 4. Architecture additions

### 4.1 `unit_conversion.py` (new)

Pure functions, no HA imports, matching the `models/` convention.
Temperature (°C/°F/K), pressure (hPa/mbar/Pa/kPa/inHg/mmHg/psi/bar),
humidity, and the standard barometric sea-level reduction. A missing unit
means "already canonical" (preserves behaviour for entities that don't
populate it); an *unrecognised* unit returns `None` — explicit rejection
rather than a silent guess.

### 4.2 `provider_validation.py` (new)

One shared physical-bounds and finiteness check applied by all three
provider coordinators immediately before `insert_forecast_snapshots_bulk`.
A rejected value becomes `None` and the row is kept, reusing the
representation every consumer already handles for "provider had no data".

### 4.3 `StormEventReconciliationCoordinator` (new)

The first production caller of `insert_storm_event()`. Runs every 30
minutes; for predictions whose 90-minute follow-up window has elapsed and
whose probability crossed the reporting threshold, it checks real station
and radar observations against Model B's *own* v0 thresholds. Confirmed
predictions are promoted with **observed** peak values, never predicted
ones. Every checked prediction is marked `reconciled` regardless of
outcome — an unconfirmed prediction is a negative training example.

### 4.4 Shared concurrency lock

One `asyncio.Lock()`, constructed on the event loop in `__init__.py` and
injected into both `ModelALearningCoordinator` and `RetentionCoordinator`.
Two independently-created locks would serialize each coordinator against
itself and nothing against the other, which is exactly the race being
closed. Both constructors fall back to a private lock so each stays
independently constructible in tests.

### 4.5 Entry lifecycle

`async_remove_entry` (deletes the database and its `-wal`/`-shm`
sidecars) and `async_migrate_entry` (v1 → v2) were added. Neither existed.

---

## 5. Findings by disposition

### P0 — all six fixed

| ID | Fix |
| --- | --- |
| P0-01 | `apply_reconciliation_batch()` — one transaction for every EMA write and status transition, with explicit rollback and in-batch bucket sequencing |
| P0-02 | Crossing state stores the **unrefined base** probability; display stays refined |
| P0-03 | `async_on_unload` shutdown loop removed; `async_unload_entry` awaits every coordinator before `db.close()` |
| P0-04 | Open-Meteo inserts rows before recording the fingerprint (in-memory **and** persisted) |
| IND-13 | CombiPrecip selects by documented `CPC…_00060` filename contract, product time from the filename |
| IND-01 | Blend weights normalised to a dimensionless scale with a bounded ratio |

### P1 — 30 of 31 fixed, 1 refuted

Credentials: P1-01 (`ConfigEntryAuthFailed` now genuinely raised by all
four coordinators), P1-02, P1-03, P1-04, P1-05.
Quota and restart state: P1-06, P1-07, P1-08, P1-09, P1-10, P1-11 (docs),
P1-12, IND-07.
Radar: P1-13, P1-14, P1-15, P1-16, P1-17, P1-18. **P1-19 refuted.**
Validation: P1-20, P1-21, P1-22, P1-23, P1-24, P1-25.
Config flow: P1-26, P1-27, P1-28, P1-29, P1-30.
Independent: IND-02, IND-03, IND-05, IND-06, IND-08.

### P2 / P3 — all fixed

P2-01 … P2-13 (P2-13's valid half — entity `unique_id`s were already
correct), P3-01, P3-02, P3-03, IND-04, IND-09, IND-10, IND-12.

### Deferred, with reason

**IND-11 (repairs / service actions).** Not implemented. The
issue-registry and service surfaces are genuinely useful but are new
*features* rather than defect fixes, and adding them would expand the
untested surface at the same moment this release is trying to shrink it.
Recorded as the first item for v0.1.25.

---

## 6. Where the external documents were wrong

Documented because silently "fixing" a non-defect is itself a defect.

| Claim | Reality |
| --- | --- |
| **P1-19** — CombiPrecip publishes every ~10 min | MeteoSwiss documents CPC's update frequency as **5 minutes**. The "60 minute" in the product name is the accumulation window. The proposed change would have halved the radar update rate *and* corrupted `RADAR_FRESHNESS_LIMIT`, which derives from it. **Pinned by a test.** |
| **P1-17 fix** — raise `ValueError` on multiple `.h5` assets | Multiple `.h5` assets per item is the documented *normal* case. This would have converted a silent wrong-product bug into a permanent hard outage. |
| **P1-01 evidence** — `UpdateFailed` hides the status from `classify_exception` | False. `record_error(err)` runs on the original exception; classification was always correct. The real gap was that nothing ever raised `ConfigEntryAuthFailed`. |
| **P2-13** — entities lack durable `unique_id`s | False. All three entity classes anchor on `entry.entry_id`. Only config-entry duplication was real. |
| **P2-01 sub-claim** — a bare `UPDATE` writes zero rows | Not reachable in v0.1.23; the fresh path used `INSERT`. Would have been *introduced* by the proposed restructuring. Fixed pre-emptively as an upsert anyway. |
| **P2-05** — 3 of 9 delta fields persisted | It was **2**. |
| **P1-16 fix** — parse the ODIM quality sub-group | Workable, but the quality code is in the **filename**, which is always present; the sub-group is optional even within the spec. |

---

## 7. Bugs introduced during this pass

### 7.1 The one that reached a real installation

The first v0.1.24 build failed at setup on every upgrading installation:

```
sqlite3.OperationalError: no such column: reconciled
  File "custom_components/swissweather_fusion/storage/db.py", in _ensure_schema
    self._conn.executescript(_SCHEMA_SQL)
```

**Cause.** The new index on `storm_predictions(reconciled)` was placed in
`_SCHEMA_SQL`, which runs first and unconditionally, *before* migration
detection. Its `CREATE TABLE IF NOT EXISTS storm_predictions` is a silent
no-op against the existing v0.1.23 table, so the index then referenced a
column that did not exist yet — and raised before any migration could add
it. Setup aborted; the integration could not load at all.

**This is the exact failure mode P2-01 exists to describe.** Worse, the
v0.1.23 author had already hit it for `reconciliation_status`, split that
one index into a separate post-migration script, and left a comment
explaining precisely this hazard. The new index was added into
`_SCHEMA_SQL` anyway, a few lines above that comment.

**Why the test suite did not catch it.** The migration test hand-built a
*partial* database — schema_meta, forecast_snapshots and bucket_stats
only. With no `storm_predictions` table present, `_SCHEMA_SQL` genuinely
created it, complete with the new column, and the index succeeded. The
test was exercising a database shape no real installation has ever had.
It gave the appearance of covering the upgrade path while covering
something else entirely, which is a more dangerous state than having no
test at all.

**First fix (v0.1.24a), and why it was not enough.** The offending index
was moved into a post-migration script and the surrounding comment was
promoted from a note into a stated rule. That is the same fix v0.1.23
applied to the same class of bug — move the one index, write down the
rule — and v0.1.24 proved the rule does not survive contact with the next
person adding an index, because v0.1.23's comment was sitting directly
below where the new one was added.

**Structural fix (v0.1.25).** A convention that must be recalled at the
moment of writing an index is not a control. So:

1. **Every index moved out of the table script**, including the four that
   were always safe. `_SCHEMA_SQL` became `_TABLE_SQL` (CREATE TABLE
   only) and `_INDEX_SQL` holds all six indexes.
2. **`_ensure_schema` has one strict order**: create tables, inspect
   actual shape, migrate if needed, *then* create indexes. Step 4 cannot
   precede step 3, so an index physically cannot execute against a
   pre-migration table shape. There is no judgement call left to get
   wrong.
3. **Two structural tests** now fail at authoring time rather than on a
   user's installation: one asserts `_TABLE_SQL` contains no
   `CREATE INDEX` at all, the other asserts the migrate-before-index
   ordering in the source of `_ensure_schema`. A third checks the inverse
   mistake — an index on a table the table script never creates.
4. **Version logging at setup.** The failure was reported as "still
   broken" when the fixed files had never actually been installed, and
   the only way to tell the two builds apart was comparing traceback line
   numbers. `async_setup_entry` now logs the manifest version and schema
   version at INFO on every start.

**Test coverage added.** `tests/test_v0_1_24_storage.py` gained a
`V0_1_23_SCHEMA` fixture — the **complete** prior schema, all eight
tables, populated — and six tests over it: that it opens at all, that
every index is created, that facts survive while derived tables rebuild,
that a second open is idempotent, that the migrated database is
immediately usable, and that a database left **half-migrated by the
failed v0.1.24 build** recovers without intervention. All were confirmed
to fail against the broken build with the exact production error.

**Lessons recorded.**

- A migration test must build the real prior schema **in full**. A
  partial fixture tests the fresh-install path wearing an upgrade path's
  name, which is worse than no test because it is cited as coverage.
- When the same class of bug recurs, fix the *class*, not the instance.
  v0.1.23 fixed the instance and wrote a comment; v0.1.24 walked straight
  past the comment.
- If a user cannot tell which build is running without reading traceback
  line numbers, that is a defect in its own right.
- **A test suite that never runs a constructor is not testing the code
  that runs first.** Three consecutive setup-blocking failures in this
  remediation — the index, the keyword argument, the missing attribute —
  were all on paths that executed before any tested method was reached.
  Volume of tests said nothing about whether startup worked.
- The honest summary of §7.1–§7.3: the remediation itself was sound, and
  every one of the 62 original defects was correctly diagnosed and
  fixed. What repeatedly failed was verifying that the *result still
  starts*. That is now covered by the migration fixture, the construction
  smoke tests and the blocking-I/O scan.

### 7.2 The second and third: constructors nothing ever called

v0.1.25 loaded far enough to migrate the database successfully, then died
at coordinator construction:

```
TypeError: AnnualCallBudget.__init__() got an unexpected keyword
           argument 'max_calls_per_year'
```

P1-06 reused the `AnnualCallBudget` class already built for Meteonomiqs
and passed it `max_calls_per_year=`. The class's parameter is
`annual_budget`, positional. The call was wrong from the moment it was
written.

**A second bug of the same kind was found while fixing it.** P2-09's
future-dated-sample fix referenced `self._diagnostics` inside
`ModelBCoordinator._async_update_data_inner` — but `ModelBCoordinator`
had no diagnostics recorder at all, and never had one. That would have
raised `AttributeError` on the first scoring cycle that encountered a
future-dated row: not at setup, but later, intermittently, under a
condition nobody could reproduce on demand. It is now an optional
constructor keyword, wired from `__init__.py` like every other
coordinator.

**Why 361 tests passed with both in place.** Every coordinator test in
this project builds its subject with `object.__new__(cls)` and hand-sets
the few attributes the method under test reads. That is a legitimate way
to test a *method* without a running Home Assistant — the pattern was
inherited from `test_coordinator_state_persistence.py` and it works well
for what it was built for. But it means `__init__` itself was never
executed by anything, so no constructor call it makes was ever checked,
and no attribute it fails to set was ever missed.

Both bugs sat on lines that no test could reach by construction.

**Fix: `tests/test_v0_1_26_construction.py`.** Twenty-three deliberately
shallow tests that really instantiate every coordinator with realistic
arguments. They assert almost nothing about behaviour; their entire job
is to run each `__init__` to completion so that a wrong keyword, a
renamed parameter or a missing attribute fails in CI rather than on a
user's installation. Two further guards:

- `test_meteoblue_annual_budget_is_usable_not_merely_constructed` — the
  budget must actually gate calls and round-trip through the persistence
  shape, not just exist.
- `test_every_coordinator_class_is_covered_by_this_file` — reflects over
  the module and fails if a coordinator is added without a construction
  test, so the gap cannot silently reopen.

A static sweep for `self._X` referenced but never assigned was also run
across every coordinator; apart from the `_diagnostics` bug above it came
back clean.

### 7.3 Blocking I/O on the event loop, in the diagnostic helper

v0.1.25's version-logging helper read `manifest.json` with a plain
`open()` from `async_setup_entry`. Home Assistant detected it
immediately:

```
Detected blocking call to open with args
('/config/custom_components/swissweather_fusion/manifest.json',)
inside the event loop
```

Particularly poor in a helper whose only purpose is to make problems
easier to see. It was also unnecessary work: Home Assistant parses every
custom integration's manifest during startup and keeps it in the loader
cache, so v0.1.26 reads the already-in-memory value and touches no disk.

A scan of every `async def` in the package for blocking filesystem calls
came back otherwise clean — `async_remove_entry`'s `os.remove` is
correctly dispatched to an executor.

**A related note on the version line.** It is logged at INFO, and the
reporting installation's logger is filtered to WARNING and above, so it
was invisible there. Anyone wanting it should add:

```yaml
logger:
  logs:
    custom_components.swissweather_fusion: info
```

### 7.4 Bugs caught by the suite during development

Ordinary development mistakes, listed because a suite that never catches
anything is not evidence of quality.

1. **Missing `_LOGGER` import in `db.py`** — caught within seconds by the
   project's own `test_no_undefined_names` static guard.
2. **`derive_condition` applied to the wrong aggregation site** — the
   twice-daily variant (referencing `is_daytime`) landed in the daily
   function. Caught by the same guard plus two aggregation tests.
3. **`voluptuous` was never actually installed.** The INFRA-02 fix made
   `conftest.py` prefer the real package, but the new validator tests
   still failed — revealing the package was absent from
   `requirements-test.txt` entirely. This is *why* the stub shadowed it
   unnoticed for so long, and it is now a declared test dependency with
   an assertion that the real package is in use.
4. **Wrong column name in a storm-event test** (`peak_pressure_drop_hpa`
   vs `peak_pressure_drop`) — caught immediately.
5. **Unused `RADAR_QUALITY_MINIMUM_CODE` import** in `coordinator.py`
   after the gate moved into `model_b` — caught by pyflakes.

None would have been caught by review alone; each was found because a
test exercised the real call path.

---

## 8. Tests that were rewritten rather than kept

Five existing tests asserted the defective behaviour. Each rewrite
carries a comment explaining why the old assertion was wrong.

| Test | Why it had to change |
| --- | --- |
| `test_blend_debiases_before_weighting` | Asserted the hard-coded `1.0` cold-start weight — the IND-01 defect itself. Rewritten to preserve its real intent (debiasing) under the new scale. |
| `test_parse_stac_items_response_sorts_newest_first` | Used invented filenames (`old.h5` / `new.h5`) and sorted on `properties.datetime`. It passed *because* selection was suffix-only. Rewritten against the documented convention. |
| `test_meteonomiqs_hourly_forecast_is_persisted…` | Asserted budget accounting at the racy location (inside the fetch, after an await). |
| `test_fresh_database_…_v2_schema` | Hard-coded schema version 2; now asserts `SCHEMA_VERSION`. |
| `test_reconciliation_watermark_roundtrip` | Tested dead code; now asserts its **absence** so reinstating it is deliberate. |

---

## 9. Verification record

```
python -m pytest tests/ -q                                   # 469 passed
python -m pyflakes custom_components/swissweather_fusion/     # clean, exit 0
```

- Suite re-run after every individual change, not only at the end.
- Every fix has at least one regression test reproducing the original
  failure mode. Where the distinction is checkable — P0-01, P0-02, P1-13,
  P1-16, P1-18, P2-08, P2-10, P2-11, IND-01, IND-02, IND-13 — the test
  asserts the specific behavioural difference, not merely that the new
  code runs.
- The v3 migration is tested against a hand-built **complete** v0.1.23
  database — all eight tables, populated — exercising the real upgrade
  path rather than only a freshly-created database. A partial fixture is
  what allowed §7.1 to ship.
- `coordinator.py`, `__init__.py`, `config_flow.py`, `sensor.py` and
  `binary_sensor.py` now have executable coverage for the first time.

---

## 10. Honest caveats carried forward

- **CombiPrecip's HDF5 internal layout remains unverified** against a
  real downloaded file. The *filename and product contract* is now
  grounded in MeteoSwiss's published documentation, which is a
  significant improvement, but the in-file group structure is still
  best-effort. `clients/combiprecip.py`'s docstring continues to say so.
- **The radar detection threshold is a v0 heuristic.** 0.5 mm accumulated
  over the preceding hour is a reasoned floor, not a calibrated figure.
- **P2-08's confirmation thresholds reuse Model B's own v0 thresholds**
  for consistency with the live scorer. What `storm_events` records is
  therefore "the v0 signature was observed", not "a meteorologist would
  call this a storm". Worth revisiting once real events accumulate.
- **P2-10's "cloudy" branch is a humidity proxy**, not cloud-cover data.
  No provider is queried for cloud cover today.
- **IND-01's normalisation is principled but unvalidated.** Median-relative
  weighting with a bounded ratio is defensible and demonstrably fixes the
  unit-scale inversion, but the specific 8:1 cap has no empirical basis
  beyond judgement.
- **SRF's content-only fingerprint** remains a necessity, not a choice —
  no run identifier exists in either confirmed response shape.
- **IND-12 (cross-source pressure semantics)** is documented but not
  resolved: whether SRF publishes MSL or station pressure still needs one
  live response to confirm. `srf_probe.py` can answer it.


---

# 8. Third external audit (against v0.1.26) — six further defects

An independent ICS-quality audit was run against the shipped v0.1.26
package, excluding all test and audit files. It returned **FAIL** with
three confirmed P1 defects and three P2 static findings. All six were
independently reproduced here before any code was changed, and all six
are fixed in v0.1.27.

**Two of the three P1s were introduced by this remediation.** That is the
uncomfortable and important part of this section.

## SWF-P1-001 — quota consumed without a provider call

*Introduced by v0.1.24's own P1-07 fix.*

P1-07 correctly moved the Meteonomiqs annual reservation to be
synchronous rather than after an awaited call, closing a TOCTOU race. It
moved it **one branch too far out**: `record_call()` ran before the
if/elif that decides whether to call anything. In forecast season before
`METEONOMIQS_FORECAST_CALL_HOUR_LOCAL`, neither branch fires — so the
counter incremented and no request was made. With a 6-hourly check that
is up to two phantom credits per seasonal day before the real noon call,
roughly tripling the recorded cost of a once-daily service.

The invariant it violates is the one the whole quota system exists for:
**one credit must correspond to one intended provider call.**

**Fix.** The reservation now happens inside each branch that actually
performs a request, still synchronously before the awaited fetch, so the
P1-07 TOCTOU fix is preserved intact.

The audit also fairly challenged the "keepalive is never skipped" policy
as an unbounded bypass of a hard quota. The arithmetic that makes it safe
is now stated in the code and asserted by a test rather than left
implicit: the keepalive fires at most once per day and the bonus path at
most once per day, so the worst case is 730 calls against a 1000/year
budget. The unconditional keepalive cannot exhaust the quota.

## SWF-P1-002 — out-of-grid points still resolving to edge pixels

*Introduced by v0.1.24's own P1-18 fix.*

P1-18 correctly replaced edge-clamping with a bounds check. It placed
that check **after `int()`**. Python's `int()` truncates toward zero, so a
point 0.1 pixels outside the LEFT edge computes a continuous column of
-0.1, becomes column 0, and then passes `0 <= col < xsize` — returning
the very edge pixel the fix was written to reject.

Only the two negative-going boundaries were affected. The right and
bottom edges happened to be caught correctly, and that partial
correctness is precisely why P1-18's own tests missed it: they tested one
out-of-grid point, and it was on a boundary that worked.

The consequence is the one P1-18 set out to eliminate — out-of-coverage
telemetry becoming valid-looking radar data, and therefore a false storm
signal.

**Fix.** Containment is tested on the **continuous** coordinate, before
any integer conversion, and `math.floor()` replaces `int()` so the
conversion is direction-independent rather than correct-by-precondition.
Tests now cover all four boundaries, inside and outside.

## SWF-P1-003 — unenforced risk scale producing a 590% probability

`parse_nowcast_response()` stored `precrisk["value"]` with no type,
finiteness or range check, and `refine_with_meteonomiqs()` divides it by
9. The audit reproduced a risk value of 99 yielding a refined score of
5.9 — published by `StormOnsetProbabilitySensor`, which advertises `%`,
as **590%**.

Past the absurd dashboard number, the real damage is that any automation
thresholding on that sensor fires unconditionally, and the value is
persisted into `storm_predictions` — Model B v1's training set. Corrupt
training data is the expensive kind of wrong, because it is not obviously
wrong later.

**Fix, in two layers.** The parser rejects anything that is not an
integer 0-9 (rejecting rather than clamping: turning 99 into 9 would
invent a maximum-risk reading out of a response we demonstrably do not
understand; and 3.7 is rejected rather than truncated to 3, for the same
silent-guess reason `unit_conversion.py` rejects unrecognised units).
`refine_with_meteonomiqs()` additionally clamps its result to its
declared [0, 1] domain, because it is public, its result reaches a user
sensor and durable storage, and one validation layer between a
third-party payload and a published percentage is not enough.

## P2 findings, all fixed

- **SWF-P2-001** — `AnnualCallBudget.load_state()` accepted semantically
  invalid persisted state. Well-formed JSON is not the same as
  meaningful: a negative count hands out free calls for a year, an
  inflated one starves the source. Invalid state is now discarded (not
  clamped), leaving the same state as "never persisted", which every
  caller already handles.
- **SWF-P2-002** — SRF's wind conversion did `entry[key] * KMH_TO_MS`
  directly on raw JSON. A string raises TypeError *inside the parser*,
  aborting the entire SRF parse and discarding every other variable in
  the response — a total source outage from one bad field. Both wind call
  sites now go through a defensive `_kmh_to_ms()`, and Meteonomiqs' radar
  amount got the same treatment.
- **SWF-P2-003** — `_pixel_indices()` assumed valid raster extents.
  Malformed metadata could cause division by zero or a silently mirrored
  grid. Dimensions, finiteness and extent direction are now validated,
  and a malformed product is rejected rather than computed with.
- **SWF-P2-004** — full HA runtime verification remains outstanding, and
  is correctly outside what any offline audit can promote to PASS. This
  is the user's install-and-exercise step.

## What this says about the remediation process

Two P1 defects in this release were **introduced by fixes in this same
remediation**, and both share a shape: the diagnosis was right, the
intent was right, and the placement was wrong by one line or one branch.
Neither was catchable by reading the diff, because the diff looks exactly
like the correct fix.

What would have caught them:

- **SWF-P1-002** — testing *all four* boundaries rather than one. A
  single out-of-grid test passed because it happened to land on a working
  edge. Symmetric conditions need symmetric tests.
- **SWF-P1-001** — an accounting invariant test
  (`credits_consumed == calls_made`) rather than a test that the
  reservation happens. Testing that a thing occurs is weaker than testing
  the property it is supposed to preserve.

Both are now in `tests/test_v0_1_27_ics_audit.py`, and all sixteen
relevant tests were confirmed to fail against the v0.1.26 code before the
fixes were restored.


---

# 9. Live-operation findings (v0.1.28)

v0.1.27 was the first build to run against real providers for an
extended period. Seven defects surfaced that no offline audit had found —
which is the point: SWF-P2-004 in the third audit correctly refused to
promote runtime behaviour to PASS without it.

**Five of the seven were introduced by this remediation.** The pattern is
now unmistakable and is addressed directly in §9.8.

## SWF-P1-004 — total CombiPrecip outage from filename casing

*Introduced by v0.1.24's IND-13 fix.*

The reporting installation logged 56 consecutive failures of
`No CombiPrecip assets found in STAC response`. Radar was completely
dead.

IND-13 replaced suffix-based asset selection with a filename contract
built from MeteoSwiss's published naming convention, which documents
`CPCyyjjjHHMMQ_nnnnn.XYZ.h5` in uppercase. The API serves lowercase:

```
cpc2623100000_00060.001.h5      <- real
rzc262310000vl.001.h5
```

The regex was case-sensitive, so every genuine file was rejected. The
v0.1.23 code accepted any `.h5` and never noticed.

**The contract was built from documentation and never validated against a
single real response.** Every test passed; every real poll failed.

**Fix.** `re.IGNORECASE`, plus `tests/test_v0_1_28_real_fixtures.py`,
whose STAC fixtures are copied verbatim from a live capture — lowercase
filenames, real quality codes, real duplicate product times.

## SWF-P1-005 — the wrong STAC item, for two weeks

*Pre-existing since v0.1.x; found while fixing the above.*

The client requested `/items?limit=1&sortby=-datetime`.
`properties.datetime` on these items is **not** the data date — it is an
update timestamp, refreshed whenever any file in the item changes, and
MeteoSwiss rewrites old hourly CPC files with an 8-day-delayed
reanalysis.

Observed live: the newest-by-datetime item was `20260819-ch` — 14 days
old and about to expire — carrying `properties.datetime` of
2026-09-02T04:00Z. Every radar reading came from 19 August while being
treated as current.

This strengthens IND-13's original finding: sorting on
`properties.datetime` is not merely imprecise, it is actively wrong.

**Fix.** Item ids are date-stamped (`YYYYMMDD-ch`), so today's item is
addressed directly with yesterday's as fallback — the only case needed,
covering the minutes after UTC midnight before that day's first file
lands. No reliance on `properties.datetime` at all, and far cheaper than
paging.

Worth noting the freshness gate from P1-13 would have caught the stale
data: a 14-day-old scan time is far outside the 10-minute limit, so the
symptom would have been "radar never contributes" rather than "phantom
precipitation". The gate worked; the fetch never reached it.

## SWF-P1-006 — household location leaked into diagnostics

`redact_coordinate_strings()` substitutes the **configured** latitude and
longitude wherever they appear as text, and was written specifically to
catch SRF's `geolocationId`. It does — but only when the id equals the
configured coordinates, and it does not: SRF resolves a request to its
**own nearest grid point** and returns that.

A real diagnostics export contained
`id='47.5536,8.9120'` — a neighbouring grid point roughly a kilometre
from the configured position. Key-based redaction missed it (the key is
`id`); the value sweep missed it (different numbers). The household's
location was written verbatim into a file this project's own note invites
the user to share.

**Fix.** A second, shape-based pass: any `<number>.<decimals>,<number>.<decimals>`
pair is redacted regardless of value. In a weather diagnostics payload
such a pair is essentially never innocuous — no temperature, pressure or
humidity field serializes that way — whereas a value sweep can only ever
catch coordinates already known. Ordinary telemetry is verified
unaffected.

**This changes only what is written into the export.** `clients/srf.py`,
the coordinates sent to any provider, and the geolocation id used at
runtime are untouched — see §9.7.

## SWF-P1-007 — the forecast accuracy sensor was never implemented

*Introduced by v0.1.24's P3-02 fix.* Two defects in one.

**It crashed on every call.** `get_all_bucket_stats()` returns a list of
`sqlite3.Row`; the sensor iterated it with `.items()` as though it were a
dict, raising `AttributeError` immediately. A blanket
`except Exception: return None` swallowed it, so the sensor looked
implemented and behaved like a stub for four releases.

Through v0.1.23 this sensor returned None **by design**, with a docstring
saying so. P3-02 replaced an honest stub with a dishonest one — strictly
worse.

**Its test could not have caught it.** The test asserted "None when
nothing is learned". None-because-nothing-learned and
None-because-it-crashed are indistinguishable, so it passed against
permanently broken code.

**It also did database I/O on the event loop.** `native_value` is a
property Home Assistant polls roughly every 30 seconds, and it queried
SQLite directly — the same class as the blocking manifest read v0.1.25
shipped and v0.1.26 removed.

**Fix.** The computation moved into `ModelALearningCoordinator`, which
already reads `bucket_stats` inside an executor job every 20 minutes; the
sensor reads the cached result. The blanket except is gone. New tests
assert an **exact expected value** from seeded buckets, and a structural
test asserts the sensor holds no database handle and contains no blanket
except.

## SWF-P2-005 — sun icons at 2am

`derive_condition()` returned `"sunny"` for any clear hour, with no
concept of time of day, and `"clear-night"` appeared nowhere in the
integration.

Home Assistant defines `sunny` and `clear-night` as distinct conditions
and performs no automatic substitution — the provider integration must
emit the right one. So a clear night hour rendered a bright sun in the
hourly forecast.

**Fix.** `derive_condition` gained an optional `is_daytime`. Wired at the
three sites that can know it: the current condition (from HA's `sun.sun`
entity), the hourly forecast (per forecast hour, via HA's astral helpers
— a fixed cutoff would be wrong for Switzerland, where sunset moves about
three hours between June and December), and the twice-daily aggregation,
which already computed `is_daytime`. The daily aggregation deliberately
passes nothing: a daily summary is a daytime summary.

Only the sunny branch gains a night variant. `partlycloudy` has night
variants in some icon sets but not in Home Assistant's condition list,
and this project's `cloudy` branch is a humidity proxy — inventing more
would claim precision the model does not have.

## SWF-P2-006 — "at local noon" was off by up to six hours

`const.py` states the seasonal Meteonomiqs call happens "at local noon",
and the hour was a meteorological choice. But noon was only ever a
**gate** (`local_now.hour >= 12`), while the coordinator woke every six
hours counted from Home Assistant start-up. The real call time was "the
first check after noon" — anywhere from 12:00 to nearly 18:00, silently
drifting on every restart.

The reporting installation started at 08:14 and made its daily call at
14:14.

Nothing was harmed: the call still happened once daily and quota stayed
correct. But the code did not do what its own documentation claimed.

**Fix.** Hourly checks, so the call lands within an hour of noon
regardless of restart time. It costs nothing — the daily and noon gates
still apply, so 23 of 24 checks return immediately without touching the
network and, since v0.1.27's SWF-P1-001 fix, without reserving quota.

## SWF-P2-007 — IND-07 closed as "will not fix"

IND-07 proposed persisting SRF's resolved geolocation id across restarts.
The database accessors were added in v0.1.24 but never wired into
`clients/srf.py`, so they were dead code from the start.

The maintainer has ruled `clients/srf.py` out of scope: **the SRF API key
is bound to a single registered coordinate**, and any change touching how
that geolocation is resolved risks invalidating the key and requiring a
new registration. Saving one HTTP lookup per reload does not come close
to justifying that.

The accessors are **deleted**, not left pending — unwired scaffolding
only invites a future contributor to connect it. A test asserts their
absence with the reasoning attached.

## 9.8 What five self-inflicted defects across five releases actually say

Counting honestly: the index bug (v0.1.24), the constructor keyword and
missing attribute (v0.1.25), the quota branch and the grid boundary
(v0.1.27), and now the filename casing and the accuracy sensor. Every one
was introduced by a fix, and every one shares a shape: **the diagnosis
was right and the verification was against the wrong thing.**

- The index was verified against a partial database fixture no real
  installation has ever had.
- The constructors were never executed by any test.
- The grid boundary was verified on one edge out of four, and it happened
  to be a working one.
- The filename contract was verified against documentation instead of a
  response.
- The accuracy sensor was verified by a test whose passing condition was
  identical to the failure mode.

The common failure is not carelessness in the fix. It is that the
**test's notion of success was satisfiable without the code working**.
That is a specific, checkable property, and it is the standard this
project should now hold new tests to: *if I break the fix, does this test
fail?* Every regression test added in v0.1.28 was confirmed against the
broken code first — ten of them fail when the four defects are
reintroduced.

Second lesson, narrower but sharp: **a fixture copied from a real
response is worth more than one derived from a specification.** Providers
do not always serve what their documentation describes, and where the two
disagree, only the response matters.

---

# 10. v0.2.0 — Model A Forecast Expansion (Stage 1a)

First feature release after the v0.1.24–v0.1.28 remediation. Implements
Stage 1a of the Model A Expansion architecture, incorporating the six
findings of the accompanying architecture review.

**Test suite 469 → 507. pyflakes clean.**

## 10.1 Governing principle

> Do not infer a value when an upstream model provides it directly.

Until v0.2.0 the integration requested five variables from Open-Meteo and
then *inferred* snow from temperature and cloud from humidity — guessing
at answers the models were willing to state. Meanwhile SRF was already
parsing eleven extra fields, storing them under an `srf_` prefix, and
nothing ever read them.

## 10.2 What changed

**Acquisition.** Open-Meteo's hourly request expanded from 5 to 17
variables (rain, showers, snowfall, snow depth, precipitation
probability, gusts, wind direction, dew point, apparent temperature,
cloud cover, visibility, WMO weather code). All are free-tier variables
on the same request: **no additional API calls, no quota cost.**

Four SRF fields already being parsed were promoted from the `srf_`
namespace into the common vocabulary: dew point, apparent temperature,
precipitation probability, wind bearing. `FRESHSNOW_MM` was deliberately
**not** promoted — it is millimetres while the common `snowfall`
parameter is centimetres, and a rename is not a unit conversion.

*No change was made to `clients/srf.py`'s request path, geolocation
resolution, or registered coordinate.* Those remain out of scope.

**New module: `forecast_parameters.py`.** A registry declaring each
parameter's class (A/B/C/D per architecture §6), unit, physical bounds,
minimum contributing sources, and fusion strategy.

**Per-parameter fusion strategies (review finding AR-03).** The
architecture document proposed one arithmetic blend for all of Class B.
That is right for continuous quantities and wrong for the three
parameters users look at most:

| Parameter | Strategy | Why not the mean |
| --- | --- | --- |
| precip, rain, showers | median | zero-inflated: mean of [0, 0, 8] invents 2.67 mm of drizzle no model forecast |
| snowfall | median, ≥2 sources | near-binary at the margin; one dissenting model must not create marginal snow |
| wind_gust_speed | max | a gust is already a peak; the mean of peaks is not a peak and understates hazard |
| visibility | min | the worst case is the operationally relevant one |
| wind_bearing | circular mean | linear mean of 350° and 10° is 180° — exactly backwards |
| precip_probability | mean | a genuine probability; averaging is defensible |

**Condition resolution.** New `resolve_condition()` with explicit
precedence: provider WMO code → stated snowfall → measured cloud cover →
the old inference as last resort. This unlocks `fog`, `lightning`,
`lightning-rainy`, `pouring` and `partlycloudy` — conditions that cannot
be derived from precipitation and temperature at all.

The headline case: 2 cm of snowfall at +3 °C (wet snow, entirely real)
was previously reported as **rain**, because the old rule required
`temperature <= 0`. Stated snowfall now settles it.

## 10.3 Findings from the architecture review

| ID | Status in v0.2.0 |
| --- | --- |
| AR-01 — card cannot consume custom forecast fields | **Open.** Verification pending on a live install. Most target fields are standard `Forecast` members and now flow through unchanged. |
| AR-02 — storage volume | **Partially closed.** Measured at **389 bytes/row**: ~1.6 GB (today) → ~6.5 GB (expanded) at 90-day retention. Retention set to 90 days by the operator. Size sensor still outstanding. |
| AR-03 — arithmetic blend wrong for Class B | **Closed.** Per-parameter strategies above. |
| AR-04 — HA `Forecast` gap overstated | **Closed.** Confirmed: only nine parameters are genuinely non-standard. |
| AR-05 — Meteonomiqs promotion | **Deferred**, correctly. Both preconditions (IND-12 pressure datum; unmeasured forecast horizon) remain unmet. |
| AR-06 — visibility is not a forecast field | **Noted.** Fused and stored; reaches the card only as a sensor entity. |

## 10.4 Verification

Following §9.8's standard — *if I break the fix, does the test fail?* —
the fusion tests were confirmed against deliberately reverted code:

- replacing precipitation's median with a mean → `test_precipitation_uses_median_not_mean` fails
- replacing gusts' max with a mean → `test_wind_gusts_use_max_not_mean` fails

Each fusion test states what the mean would have produced and asserts the
result differs. A test asserting only "a number came out" would pass for
every strategy including the wrong one.

Two seam tests guard the acquisition/fusion boundary: every mapped
provider name must exist in the registry, and every registered parameter
must declare bounds and a strategy. Both catch the silent failure where a
field is stored but never fused.

## 10.5 Honest caveats

- **The fusion strategies are reasoned, not calibrated.** Median for
  precipitation and max for gusts are defensible on distributional
  grounds; the specific choices have not been validated against outcomes.
- **Class B parameters carry no learned correction and never will**
  without local sensors. They are a forecast *consensus*, not a
  bias-corrected estimate. This distinction should survive into any UI.
- **The WMO code mapping is coarse.** HA's condition vocabulary is much
  smaller than WMO 4677, so distinctions (freezing drizzle vs drizzle)
  are lost. Unrecognised codes return None rather than guessing.
- **Storage grew ~3.4×.** Bounded retention is now a practical
  requirement, not a nicety.
- **AR-01 is unresolved.** No card behaviour has been verified. Stage 2
  should not be designed further until it is.

---

# 11. v0.2.1 — wiring, plausibility and presentation

**Test suite 507 → 538. pyflakes clean.**

Six fixes, three of them defects introduced by v0.2.0 itself.

## 11.1 SWF-P1-008 (Critical) — Class B fusion was never called

v0.2.0 added `_fuse_class_b()` and `_resolve_categorical()`, tested both
directly, and **never wired either into the blend**. Every measurement
still went through `_blend_at()`, the learned-bias path. Class B
parameters have no `bucket_stats`, so each source fell into the
cold-start branch and the result was a plain arithmetic **mean**.

The entire AR-03 fix was inert in production:

- precipitation was still averaged — mean of [0, 0, 8] giving 2.67 mm of
  drizzle no model forecast
- gusts were still means of peaks
- wind bearing was still linearly averaged — 350° and 10° giving **180°**,
  exactly backwards

Verified by grep: zero callers.

**This is the fifth occurrence of one pattern** (see §9.8): the tests
exercised the strategies directly and passed, because the test's notion
of success was satisfiable without the code being reachable. Testing a
function is not testing that anything calls it.

**Fix.** A single `_blend_by_class()` entry point dispatching on
parameter class, used by both the current-conditions dict and the hourly
forecast loop. Two structural tests now guard it: one asserting the
routing itself, one asserting every measurement belongs to exactly one
class — so a new parameter cannot silently fall through to the wrong
path.

## 11.2 SWF-P1-009 (High) — implausible station pressure was learned

Found in a live installation. A Netatmo entity reporting **normalised**
pressure (1024.2 hPa) was configured as station-level, so it was reduced
to sea level a second time: **1089.8 hPa**, a fabricated +65.6 hPa. Model
A learned that as a −66.8 hPa forecast bias and began dragging blended
pressure upward — the card showed 1042 hPa and climbing as buckets
crossed the trust threshold.

Every individual step worked exactly as designed. The setting was simply
wrong, and its consequences were invisible.

The situation was **self-diagnosing and nothing was looking**: the
station's own value disagreed with every provider's MSL forecast by 66
hPa. A checkbox with invisible consequences needs a check with visible
ones.

**Fix.** After any reduction, the value is bounds-checked against
`PRESSURE_PLAUSIBLE_MIN/MAX_HPA` (870–1085 hPa — wider than any recorded
terrestrial MSL extreme, so only a configuration error can trip it).
Out-of-range readings are **discarded with a warning naming the likely
cause**, not stored. Temperature and humidity from the same cycle are
unaffected.

Deliberately narrower than `provider_validation`'s 800–1100 storage
bounds: that range must accommodate raw station pressure at altitude,
whereas by this point the value should already be sea-level normalised.

## 11.3 SWF-P2-007 — current-condition properties never published

v0.2.0 fused dew point, apparent temperature, cloud cover, visibility and
gusts, but the weather entity implemented only four properties. A card
configured to show them displayed nothing — correctly, because the entity
provided nothing.

All are documented `WeatherEntity` members, so they need **no custom
sensor entities**. This resolves review finding **AR-01** better than
expected: the standard contract carries them.

Also added to the hourly forecast as standard `Forecast` fields
(`native_dew_point`, `native_apparent_temperature`,
`native_wind_gust_speed`, `wind_bearing`, `cloud_coverage`, `uv_index`,
`precipitation_probability`), and the forecast now uses
`resolve_condition()` — so `fog`, `lightning` and `pouring` finally reach
the UI.

## 11.4 SWF-P2-008 — once-daily sources reported as Degraded

The v0.1.24 rule "never succeeded means not working" was right for a
source polling every five minutes and wrong for one that runs daily by
design. Meteonomiqs and meteoblue counted as unhealthy from every restart
until their next scheduled slot, so the integration showed **Degraded**
for hours with nothing wrong — the way a health indicator becomes ignored.

**Fix.** A never-succeeded source is *pending* rather than failed until a
per-source grace period elapses (26 h for the once-daily sources, 30 min
for CombiPrecip). After that it is unhealthy, so a genuinely dead source
still surfaces the same day.

## 11.5 UV index

Now requested, mapped and published. Kept in a **separate optional
variable set**: `uv_index` is a derived product and this client restricts
each request to one model, so whether every model accepts it is
unverified against the live API. The whole variable list goes in one URL,
and three sources dying at once to gain one nice-to-have is a bad trade —
the same reasoning as the v0.1.28 CombiPrecip lesson.

## 11.6 Database size sensor (AR-02)

`get_storage_stats()` existed since v0.1.24 and nothing surfaced it.
Now cached by `RetentionCoordinator` (already hourly, already in an
executor job) and exposed as a diagnostic sensor in MB with per-table row
counts. The stats read sits **outside** the `purge_days` gate, since size
reporting is most useful when retention is disabled.

## 11.7 INFRA-04 — weather.py was untestable

`homeassistant.components.weather` was never stubbed, so no test could
import `weather.py`. That is why SWF-P2-007 survived two releases: a
module no test can import is a module no test covers — the same cause as
the v0.1.25 constructor bugs. Stub added.

## 11.8 Verification

All three main defects were reintroduced and the suite re-run:
**eight tests fail**, covering the routing of all four fusion strategies,
the pressure discard, and both halves of the health grace rule. Restored
and re-verified at 530 passing.

## 11.9 Learning reset control

A new `button` platform hosts a single diagnostic control, **Reset
learning**, for recovering from a poisoned learned state.

**A per-measurement version was built first and rejected.** Resetting
only pressure leaves the learned state at mixed vintages — pressure from
zero while temperature and humidity carry history from before the problem
was understood. Bucket confidence then means different things for
different measurements, which is subtler and longer-lived than simply
relearning everything. With only hours of samples at stake, consistency
is worth more.

The reset does three things in one transaction, and the third is what
makes it cheap:

1. deletes every `bucket_stats` row;
2. NULLs **physically implausible** stored observations — without this the
   poisoned readings are still inside the reconciliation window and would
   re-teach the same bias. Valid readings are never touched;
3. re-opens forecasts within `MIGRATION_REOPEN_WINDOW` as `pending`, so
   learning rebuilds from data already held rather than waiting for new
   forecasts. Recovery is hours, not the days a cold start would take.

Raw forecasts and valid observations are preserved throughout — only the
derived interpretation is discarded. The button is `DIAGNOSTIC` and hidden
by default (a recovery control, not a routine one), and reports what the
last press actually did, since a control with no feedback leaves the user
unable to distinguish success from a no-op.

`INFRA-05`: the button component was added to the test stubs, and a test
asserts the platform is actually forwarded — guarding against the same
"implemented but not wired" failure as SWF-P1-008.

## 11.10 Carried forward
- **AR-05 (Meteonomiqs promotion)** remains blocked on IND-12 and the
  unmeasured forecast horizon.
- **The fusion strategies remain reasoned, not calibrated.**


---

# 12. v0.2.2 — third-party ICS audit remediation

An independent ICS-quality audit of the v0.2.1 package returned **NOT
PASSED**: 1 Critical, 5 High, 12 Medium. All 18 are fixed.

**Test suite 538 → 564. pyflakes clean.**

Several were defects this project introduced. The recurring shape — now
its fourth consecutive release — is code that is implemented, tested in
isolation, and never reached.

## 12.1 SWF-021-006 (Critical) — CombiPrecip persistence was broken

`coordinator.py` called `local.precip_rate_mmh`. That attribute was
renamed to `precip_accum_mm_1h` in v0.1.24, when P1-14 established that
CombiPrecip reports a one-hour accumulation rather than an instantaneous
rate. This one call site was missed, so **every radar cycle raised
AttributeError**.

It failed in the most misleading possible place: **after**
`health.record_success()` and **after** the "N points extracted"
diagnostics event. So telemetry reported CombiPrecip healthy and
succeeding while `radar_observations` stayed empty and the coordinator's
`.data` was never updated. Model B received no radar signal at all — the
storm score had only its station-tendency half, which is why it sat at
0% through conditions that should have moved it.

The reporting installation's own diagnostics showed exactly this: two
`poll_success` events, `consecutive_failures: 0`, and no radar rows.

**Fix.** Correct attribute, plus the parsed `quality` code (also being
discarded), plus `record_success()` moved **after** the write. Success
must not be recorded until the work is actually complete — a regression
test asserts that ordering.

## 12.2 SWF-021-001/002/003/004 — condition resolution

v0.2.0 added `resolve_condition()` and wired it into the hourly forecast
only. The current condition and both aggregations kept inferring from
precipitation and humidity.

The user-visible symptom was the entity **contradicting itself**: "Sunny"
displayed beside a published `cloud_coverage` of 89%, because the
humidity proxy read 39% humidity as clear while the measured cover said
overcast.

Also fixed:

- **SWF-021-003** — the daily aggregation inferred snow from the daily
  *maximum* temperature, so a day with a +6 °C afternoon and overnight
  snow was classified rain. Now uses stated snowfall and the minimum.
- **SWF-021-004** — a stated clear-sky WMO code outranked contradictory
  measured cover. Clear-sky codes are now reconciled against cover;
  codes describing rain, fog or thunder are never second-guessed,
  because cover cannot contradict them.

## 12.3 SWF-021-009/010/011 — UV was collected and thrown away

`uv_index` was registered, requested, mapped, and published on the
entity — and **omitted from `FUSED_MEASUREMENTS`**, which is what the
blend actually queries. So it was collected, stored, and never fused or
exposed. Same for `sunshine_duration`. The user could not find UV because
it genuinely was not there.

The **optional-variable fallback was also dead code**: v0.2.1 set
`_include_optional_variables` and never read it, so the protection its
own comment described did not exist.

**Fix.** Both parameters added to the fusion set; the fallback wired end
to end. A test now asserts that **every fusable registry parameter
reaches the blend**, closing the class rather than the two instances.

*Ozone is a separate matter: the card offers it, but no provider this
project uses supplies it. Documented rather than left to be hunted for.*

## 12.4 SWF-022-001 / SWF-021-013 — precipitation was never validated

`provider_validation` keyed its bounds on `"precipitation"` while the
project's vocabulary — and therefore every stored row — uses `"precip"`.
Precipitation fell through to the unknown-variable path and received a
finiteness check only. None of the twelve parameters added in v0.2.0 had
bounds either.

**Fix.** Bounds are now **derived from the parameter registry**, so a
parameter cannot be fusable without also being validated and the names
cannot drift. Two deliberate exceptions are documented in place: pressure
keeps wider storage bounds (a station at 2000 m legitimately reads
~795 hPa), and categorical codes are finiteness-checked only.

## 12.5 The remaining findings

- **SWF-021-005** — the storm reconciliation coordinator was not
  listener-registered. `DataUpdateCoordinator` only schedules recurring
  refreshes while it has a listener, so its 30-minute cycle never ran
  after the initial refresh. `storm_events` could still only fill on a
  restart.
- **SWF-021-007** — the Degraded binary sensor did not pass the source
  name to `is_source_healthy`, so the per-source grace periods added in
  v0.2.1 did not apply. The symptom SWF-P2-008 fixed survived on the
  entity most likely to drive an automation.
- **SWF-021-008** — schema detection checked three hand-picked sentinel
  columns. A subset of the real requirements will eventually declare a
  partially-migrated database current.
- **SWF-021-012** — coordinator *construction* failures propagated with
  the SQLite connection open. Home Assistant retries setup, opening a
  second connection each time.
- **SWF-021-014** — duplicate provider runs omitted the source from
  `results`, making the coordinator claim a healthy source was
  unavailable.
- **SWF-021-015** — meteoblue's response already carried eight of the
  expanded parameters and they were discarded. No extra call, no extra
  credit.

## 12.6 Reset button re-verified

Re-checked end to end against a **reproduced** poisoned state — a
−66.8 hPa learned bias and a 1089.8 hPa observation. The bucket is
discarded, the implausible observation nulled, relearning triggered.

It is now **visible by default**. v0.2.1 hid it, which was caution
applied on the user's behalf to a control they had explicitly asked for,
and it meant the recovery button could not be found when needed.

## 12.7 Verification

Four defects were reintroduced and the suite re-run: **six tests fail**,
covering the radar field contract, UV reachability, precipitation
validation and duplicate-run reporting. Restored and re-verified at 564.

## 12.8 The pattern, stated plainly

Of the 18 findings, the ones that mattered most share one shape:
**implemented, unit-tested, never reached.** The radar rename missed a
call site. UV was fused nowhere. The fallback flag was assigned and never
read. The resolver was wired into one of four sites.

Unit tests cannot catch this class, because the unit is correct. What
catches it is asserting **reachability**: that every registry parameter
appears in the blend's measurement set, that every fusable parameter has
validation bounds, that the coordinator is in the listener tuple, that
the flag is read as well as written. Those tests are now present, and
they are written to fail on the class rather than the instance.


---

# 13. v0.2.3 — station pressure cross-check

**Test suite 564 → 577. pyflakes clean.**

Cumulative with v0.2.2; that build was never deployed.

## 13.1 SWF-023-001 — detection by luck, not by design

v0.2.1 added a plausibility check bounding the processed station reading
at 870–1085 hPa. Correct for *"is this physically possible"*, useless for
*"is this correctly configured"*.

A sea-level-normalised reading reduced to sea level a second time gains
about **65 hPa at 540 m**:

| Raw reading | Stored after double reduction | Caught by the 1085 ceiling? |
| --- | --- | --- |
| 1024.2 | 1090.7 | yes |
| 1020 | 1086.3 | yes |
| 1015 | 1080.9 | **no** |
| 1010 | 1075.6 | **no** |
| 1000 | 1065.0 | **no** |

**The error is identical in every row; only the weather differs.** The
reporting installation was caught only because it happened to be a
high-pressure day. On an ordinary day the same misconfiguration would
have silently poisoned the learning exactly as before.

The same gap affected recovery: `reset_all_learning()` nulled
observations by the same absolute bounds, so sub-1085 corrupted readings
survived a reset and would re-teach the identical bias on the next
reconciliation.

## 13.2 Fix — compare against the providers

Every provider reports mean-sea-level pressure, and models agree on it
closely — typically within about 2 hPa for the same hour. Their consensus
is therefore a reliable yardstick, and it was available the whole time.

`get_reference_pressure_hpa()` returns the **median** provider pressure
for the current hour (median, not mean, so one absurd provider value
cannot drag the reference far enough to mask a genuine station error).
`StationCoordinator` compares the processed reading against it and
discards anything beyond `STATION_PRESSURE_REFERENCE_TOLERANCE_HPA`
(25 hPa) with a warning naming the likely cause.

25 hPa is roughly a 200 m altitude error — far outside legitimate model
spread, and an order of magnitude below the ~65 hPa signature of a double
reduction.

**The reset now clears by disagreement too**, via a correlated subquery
against provider forecasts for the same hour, so residue that survived
the absolute-bounds sweep is caught. Observations with no provider
reference are left alone: absence of evidence is not evidence of
misconfiguration.

## 13.3 A new diagnostic sensor

`Pressure vs providers` exposes station-minus-consensus in hPa, with
`within_tolerance` and an interpretation string.

The relationship was self-diagnosing for a full day and **nothing was
looking**. This sensor is the looking. Near zero means the datum
configuration is right; a persistent offset of tens of hPa means the
sea-level option is set wrongly. A few hPa is normal.

## 13.4 Verification

The cross-check was removed and the suite re-run: the ordinary-day test
fails with *"a 65 hPa datum error passed because it did not exceed a
world record"*. A companion test asserts explicitly that the same value
**would** have passed the v0.2.1 bounds check, so a future reader cannot
mistake the two checks for redundancy.

## 13.5 Note on the reset button

Unchanged in scope, and worth restating because the wording in earlier
notes was misleading: **it resets ALL learning** — every bucket, every
measurement, every source. Pressure was the *cause* of the incident, not
the scope of the remedy. Verified against a seeded 15-bucket state across
three measurements and five sources.


---

# 14. v0.2.4 — learning model improvements

**Test suite 577 → 604. pyflakes clean.**

Three changes to the learning model, plus one production bug the new
tests exposed.

## 14.1 SWF-024-001 — the fusion is now falsifiable

Until this release nothing measured whether fusion helps. `bucket_stats`
recorded how wrong each individual **provider** was; the blended answer —
the number the user actually sees — was never compared against anything.
The project's central claim was untested and untestable.

The blend now records its own output as a pseudo-source, at six
representative lead offsets (1, 3, 6, 12, 24, 48 h), and is reconciled by
the normal learning loop like any provider. `SOURCE_BLEND` is
deliberately **not** in `ALL_FORECAST_SOURCES` — including it would feed
the blend its own output and make the fusion self-referential.

The forecast-accuracy sensor now reports `blend_mae`, `best_source`,
`best_source_mae` and **`blend_beats_best_source`**. If the blend does not
undercut the best single source, the learned bias correction is not
earning its complexity — and that is the single most valuable thing this
project could discover about itself. A test asserts the comparison is
reported honestly when the blend *loses*, because a scoreboard that can
only show a win is not a scoreboard.

## 14.2 SWF-024-002 — the station is no longer assumed infallible

v0.2.3 added a provider cross-check for pressure. It existed for pressure
only, because pressure happened to be the measurement that broke first.

Temperature and humidity are learned from the same single station with
the same total trust. A domestic sensor in afternoon sun reads several
degrees high; Model A would conclude all five providers forecast cold and
warm the blend to match a badly-sited thermometer. Nothing would have
flagged it.

`get_reference_value()` generalises the pressure lookup, and every
learned measurement is now checked against the provider median.

**The tolerances are deliberately wide** (20 K, 40 pp, 25 hPa). They
reject gross configuration errors — an undeclared Fahrenheit sensor, a
stuck humidity element, a doubly reduced pressure — not calibration drift
or genuine microclimate. A thermometer above a patio legitimately reads
several degrees above a 1 km grid cell, and that difference is precisely
the signal Model A exists to learn. Rejecting narrowly would destroy it.

So: **reject only the implausible, but expose the delta always.** The
per-measurement deltas are recorded on every cycle regardless, so slow
drift stays visible even when it is not rejected.

## 14.3 SWF-024-003 — freshness weighting, centred to avoid double-counting

Model runs age at different rates: ICON-CH1 every 3 h, CH2 every 6 h,
ICON-D2 every 3 h, SRF roughly hourly, meteoblue effectively every 8 h
because **our** credit budget, not its model, sets our staleness.

A naive decay would have been wrong, and the reason came out of the
design discussion rather than the code. `ema_abs_error` is learned from
samples spread across each source's cadence, so the **average staleness
penalty is already inside the learned weight** — CH2 already scores
slightly worse partly because its data is typically older. A curve that
only ever reduced the weight would penalise the same staleness twice.

So the curve is **centred on each source's mean run age** (cadence / 2):
1.0 at typical staleness, above when fresher, below when staler. `E[f]`
over a cycle is approximately 1, verified by a test, so the learned
weight keeps its meaning and only *deviations* are corrected. It also
halves the oscillation amplitude, since the swing is symmetric about
current behaviour rather than always downward.

Amplitude is capped conservatively at +20 % / −20 %. Neither the true
skill-decay curve nor its magnitude is known; `bucket_stats` will
eventually measure it, and under-correcting is the right error until
then. The shape is linear for the same reason — it is the honest choice
in the absence of evidence.

Beyond twice its cadence a source is **failing, not ageing**, which the
historical average does not cover. The factor then continues below the
symmetric floor toward 0.3, approaching cold-start trust.

Applied to the learned branch only: a cold-start source has no learned
weight to modulate, and its neutral weight is defined relative to the
trusted set (IND-01).

**On oscillation.** The blend output now varies with each source's run
cycle, at different periods. This is bounded and cannot destabilise
anything — the blend is stateless per cycle, with no integrator and no
feedback. Critically, it does **not** propagate into learning:
`update_bucket_ema` takes only forecast, actual and lead-time bucket, and
the blend output is never written back into the provider rows it learns
from. Learned biases stay slow and monotone regardless.

## 14.4 SWF-024-004 — a production crash found by the new tests

Writing the blend-verification test surfaced a `KeyError` that would have
taken down the entire blend cycle.

v0.2.1 rewrote the hourly forecast builder to strip keys whose value is
None, so optional parameters do not surface as nulls in the Forecast
dict. That silently made the aggregations' direct
`e["native_precipitation"]` lookups unsafe: an hour with no precipitation
value has no such **key**, and the lookup raised — failing the whole
cycle rather than that one hour.

Every existing aggregation test builds complete entries, so nothing
caught it. Fixed with `.get()` in both aggregations, plus tests using
deliberately sparse entries.

This is the fourth time a v0.2.x change has been correct in isolation and
wrong at a seam. The pattern holds: unit tests verify the unit, and the
defects live between units.

## 14.5 SWF-024-005 — learning progress must be chartable

The three numbers added in 14.1 were first placed on
`ForecastAccuracySensor` as **attributes**. Home Assistant records
long-term statistics for a sensor's **state** and never for its
attributes — so today's value was visible in Developer Tools and the
*trend*, which is the entire question, could not be plotted.

Four sensors now carry them as states, each with
`state_class = MEASUREMENT`:

| Sensor | What it answers |
| --- | --- |
| **Blend accuracy (MAE)** | Is the corrected output getting better? |
| **Best source accuracy (MAE)** | The honest benchmark to beat |
| **Learning progress (%)** | Has learning started, or finished? |
| **Trusted buckets** | Is the denominator sane? 100% of four buckets is not convergence |

**A distinction worth stating**, because it changes how the numbers read:
a provider's `ema_abs_error` is the residual *after* debiasing, so it
drops once a bias is found and then measures how noisy that model
inherently is. No amount of learning changes that. The curve that should
bend downward is the **blend's**, because the blend rows recorded since
14.1 contain bias-corrected values.

Expect learning progress to climb toward a plateau and then **step down
at each season boundary** — buckets are keyed by season, so a transition
empties the seasonal ones. That is expected, not a regression, and it is
documented on the entity so it is not misread later.

The percentage denominator is buckets that **exist**, not the ~4,300 the
key space allows. Most of that space is unreachable — ICON-CH1 forecasts
only 33 hours, so it can have no long-lead buckets — and dividing by an
unreachable total would report a permanently low number that means
nothing.

## 14.6 Verification

The three changes plus the KeyError fix were reverted and the suite
re-run: **six tests fail**, covering blend self-recording, the tolerance
table, temperature and humidity rejection, and both aggregation paths.
Restored and re-verified at 599.

## 14.7 Still outstanding

- **Season-boundary cold start.** Buckets are keyed by season, so every
  seasonal transition empties them and bias correction lapses until they
  refill — four times a year, imminent for autumn.
- **No outlier rejection in reconciliation.** A single legitimate-looking
  residual folds into the EMA at full weight and cannot be forgotten.
- **Lapse-rate pre-correction remains unwired** — it needs each source's
  grid elevation threaded from the responses.
- **True model initialisation time.** `issued_at` is first-seen
  (publication-observation) time. Open-Meteo's metadata API exposes
  `last_run_initialisation_time` and `update_interval_seconds`, free of
  rate limits; adopting it would improve both the freshness signal and
  the existing lead-time attribution. Endpoint shape needs live
  verification first.
