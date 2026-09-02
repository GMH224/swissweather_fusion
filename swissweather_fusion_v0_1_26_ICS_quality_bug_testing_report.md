# ICS-Quality Software Bug Testing Report
## SwissWeather Fusion v0.1.26

**Assessment date:** 2026-09-02  
**Assessment target:** `swissweather-fusion-v0.1.26.zip`  
**Assessment type:** Independent source/package audit with targeted adversarial execution  
**Disposition:** **FAIL — remediation required before an ICS-quality release gate**

---

## 1. Executive Summary

The package was reviewed as an operationally critical weather-data integration, with emphasis on:

- deterministic behavior and data integrity;
- failure containment and recovery;
- quota/account protection;
- asynchronous/event-loop safety;
- persistence, migration, and concurrency;
- malformed/stale/out-of-range input handling;
- less-traveled error and boundary paths;
- diagnostic/security behavior;
- packaging and static integrity.

The review **excluded all test files and all audit/defect-log files as requested**. The shipped production/documentation surface contained **37 non-test, non-audit files**; all were inventoried, and all Python production modules were parsed/compiled.

### Release-gate result

| Gate | Result |
|---|---|
| Package inventory | PASS |
| Python syntax/AST integrity | PASS |
| JSON metadata integrity | PASS |
| Image file integrity | PASS |
| Event-loop/blocking-I/O review | PASS with residual risks |
| SQLite transaction/concurrency review | PASS with residual risks |
| Boundary/error-path review | **FAIL** |
| Provider input validation | **FAIL** |
| Quota/accounting invariants | **FAIL** |
| Overall ICS-quality release gate | **FAIL** |

### Confirmed defects

**3 confirmed defects were reproduced or directly demonstrated:**

1. **P1 — Meteonomiqs annual quota is consumed without an API call before the seasonal noon window.**  
   The scheduled path records an annual call immediately, then can decide not to make any call because it is before local noon. Repeated 6-hour coordinator checks can therefore burn quota credits without provider traffic.

2. **P1 — CombiPrecip out-of-grid coordinates can still be mapped to the edge pixel.**  
   `_pixel_indices()` converts fractional negative indices using `int()`, which truncates toward zero. A coordinate just outside the lower-left grid boundary can therefore become column/row `0` and be treated as valid radar data instead of no-data.

3. **P1 — Meteonomiqs risk values are not range/type validated before probability refinement.**  
   The code documents a 0–9 scale but accepts arbitrary values. A reproduced risk value of `99` produced a refined score of `5.9`, i.e. **590%** when exposed by the sensor.

These defects violate core operational invariants: **one provider credit must correspond to one intended provider call; out-of-coverage telemetry must not become valid-looking data; a probability/risk score must remain within its declared domain.**

---

## 2. Scope and Coverage

### Included

All non-test, non-audit package files were inventoried:

- integration lifecycle/configuration;
- coordinator scheduling and orchestration;
- SQLite storage/migrations;
- all provider clients;
- Model A and Model B;
- unit conversion and physical validation;
- diagnostics and redaction;
- sensors/weather/binary sensor/device;
- metadata and translations;
- SRF probe;
- documentation/package metadata;
- icons.

### Explicitly excluded

Per instruction:

- `tests/**`;
- files whose names identify remediation audits;
- consolidated defect-log/audit artifacts.

Generated `__pycache__` files were not treated as source targets.

### Inventory

**37 files** were in the requested audit scope.

**26 Python source files** were present under the integration plus `srf_probe.py`; all parsed successfully.

---

## 3. Test Methods

The following independent checks were performed:

1. ZIP extraction and complete source inventory.
2. Python `ast.parse()` across production Python source.
3. Python `compileall` across production code.
4. JSON parsing of:
   - `manifest.json`;
   - `translations/en.json`;
   - `hacs.json`.
5. PNG integrity verification.
6. Importability check for the local environment.
7. Static inspection for:
   - broad exception handling;
   - unsafe execution primitives;
   - network calls/timeouts;
   - blocking filesystem/database access;
   - concurrency/locking;
   - transaction boundaries;
   - state persistence;
   - numeric/domain validation;
   - timezone handling;
   - boundary arithmetic.
8. Targeted execution of pure business logic.
9. Targeted execution of the CombiPrecip grid-index calculation.
10. Targeted execution of Model B risk refinement.
11. Review of storage migration logic and transaction atomicity.
12. Review of less-traveled branches including fallback, retry, corruption, quota, stale-data, and out-of-range paths.

### Environment limitation

The uploaded package's production integration depends on Home Assistant, but **Home Assistant is not installed in the audit runtime**. Therefore a full live HA lifecycle test — config-flow UI, entity registration, DataUpdateCoordinator behavior inside HA, actual service setup/unload, and real provider network calls — could not be executed.

This is a **test-environment limitation, not a PASS**. It is explicitly carried into the release recommendation.

The absence of Home Assistant was independently confirmed by the import check:

`ModuleNotFoundError: No module named 'homeassistant'`

The local environment did contain `aiohttp`, `h5py`, and `pyproj`, allowing several pure/provider parsing paths to be exercised.

---

# 4. Detailed Findings

## SWF-P1-001 — Meteonomiqs annual quota can be consumed without making a provider call

**Severity:** P1 / High  
**Status:** Confirmed  
**Category:** Resource/accounting integrity  
**Affected file:** `custom_components/swissweather_fusion/coordinator.py`  
**Primary path:** `MeteonomiqsCoordinator._async_update_data()`

### Expected invariant

An annual provider-call counter must increment **only when an actual provider call is initiated/reserved**, and the annual allowance must never be consumed by a scheduling check that ultimately performs no request.

### Observed behavior

The scheduled path executes:

- daily gate check;
- `self._budget.record_call(today=today)`;
- then determines whether the local time is after the seasonal forecast-call hour.

In the seasonal period, when the coordinator runs before noon:

- the budget is incremented;
- neither `_async_fetch_hourly_forecast()` nor `_async_fetch_nowcast()` is called;
- the coordinator returns and waits for its next check.

Because the coordinator checks every six hours, multiple pre-noon checks can consume multiple annual credits without an API request.

### Impact

This causes:

- false quota depletion;
- misleading diagnostics/state;
- earlier-than-expected exhaustion of the vendor's annual allowance;
- reduced availability of legitimate storm-triggered bonus calls later in the year;
- divergence between persisted quota state and actual provider traffic.

The defect is particularly important because the code explicitly describes the value as a **1000-calls/year** budget.

### Reproduction reasoning

For a seasonal day with checks at 00:00 and 06:00 local time:

- check 1: budget +1, no API call;
- check 2: budget +1, no API call;
- noon check: budget +1, actual API call.

A single daily forecast service can therefore consume three budget units while producing one provider request.

### Corrective action

Move the annual accounting reservation into the exact branch that is about to perform the request, e.g.:

- determine whether a call is actually due;
- perform an atomic `try_call()`/reservation;
- only then invoke the provider;
- do not increment the budget on a no-op scheduling check.

Also add an invariant test:

> For every coordinator execution, `calls_used_delta == number_of_provider_call_reservations`.

Do not allow a "keepalive must never be skipped" policy to bypass the hard annual quota without an explicit product decision.

---

## SWF-P1-002 — CombiPrecip lower-bound grid containment check is mathematically incorrect

**Severity:** P1 / High  
**Status:** Confirmed and reproduced  
**Category:** Data integrity / geospatial boundary handling  
**Affected file:** `custom_components/swissweather_fusion/clients/combiprecip.py`  
**Primary function:** `_pixel_indices()`

### Expected invariant

A point outside the raster grid must produce no data.

### Observed behavior

The code computes a floating-point pixel coordinate and then performs:

`int(fractional_index)`

Python's `int()` truncates toward zero.

For a point slightly outside the lower-left edge:

- true column coordinate may be `-0.1`;
- `int(-0.1)` becomes `0`;
- the subsequent check `0 <= col < xsize` succeeds;
- the edge pixel is returned.

### Reproduction

Using a synthetic 100×100 grid and a point just outside the lower-left easting boundary, the function returned:

`(50, 0, 100, 100)`

The correct result for an out-of-grid point is:

`(None, None, ...)`

### Impact

The affected point can be:

- the local point;
- near point;
- mid point;
- far/upwind point.

The resulting edge pixel is indistinguishable downstream from valid radar data. This can produce a false precipitation signal and therefore a false storm-risk score.

This is exactly the class of error that is dangerous in a boundary/less-traveled path: it does not crash, and it produces plausible-looking data.

### Corrective action

Use mathematically correct containment/indexing:

- explicitly reject coordinates outside the continuous `[0, size)` grid domain before conversion; or
- use `math.floor()` for index conversion and then validate;
- independently validate raster dimensions and geographic extents.

Recommended invariant:

> Any point with a continuous pixel coordinate `< 0` or `>= size` must return no-data.

Add tests immediately outside **all four** raster boundaries, including values within one pixel of each edge.

---

## SWF-P1-003 — Meteonomiqs risk scale is not enforced; Model B can emit impossible probabilities

**Severity:** P1 / High  
**Status:** Confirmed and reproduced  
**Category:** Input validation / safety of derived telemetry  
**Affected files:**  
`custom_components/swissweather_fusion/clients/meteonomiqs.py`  
`custom_components/swissweather_fusion/models/model_b.py`  
`sensor.py`

### Expected invariant

The Meteonomiqs risk value is documented as a **0–9** scale, and the resulting Model B risk/probability value should remain in its declared domain.

### Observed behavior

`parse_nowcast_response()` directly stores:

`precrisk.get("value")`

with no:

- type validation;
- integer validation;
- finite-value validation;
- 0–9 range validation.

`refine_with_meteonomiqs()` then performs:

`risk / 9`

and averages it with the base score.

### Reproduction

A deliberately invalid Meteonomiqs risk value of `99` produced:

- refined score = `5.9`;
- exposed sensor value = approximately **590%**.

This was executed directly against the production Model B function.

### Impact

Possible consequences include:

- impossible automation thresholds;
- invalid dashboard values;
- false storm activation;
- downstream template logic receiving values outside the documented range;
- corrupted future training data in `storm_predictions`.

The issue is amplified because the sensor advertises `%`.

### Corrective action

Validate at the parser boundary:

- value must be an integer or safely coercible numeric;
- `0 <= risk <= 9`;
- malformed values become `None`.

Also enforce a final Model B invariant:

`0.0 <= probability <= 1.0`

before persistence and sensor exposure.

The persisted training record should never accept a score outside the declared domain.

---

# 5. Additional Risks / P2 Findings

## SWF-P2-001 — Persisted Meteonomiqs budget state lacks semantic validation

**Severity:** P2 / Medium  
**Status:** Static finding

`AnnualCallBudget.load_state()` accepts persisted `year` and `calls_used` values without validating:

- year type/range;
- `calls_used` integer semantics;
- non-negativity;
- upper bound relative to annual budget.

A syntactically valid but semantically corrupt database state could therefore restore an invalid quota counter.

### Recommendation

Validate and clamp/reject persisted state:

- `year` must be an integer year;
- `calls_used` must be an integer;
- `0 <= calls_used <= annual_budget`;
- invalid state should be discarded with a diagnostic event.

---

## SWF-P2-002 — Provider parsers trust numeric JSON types too heavily

**Severity:** P2 / Medium  
**Status:** Static finding

Several provider parsers pass response values directly into downstream calculations/storage. The shared physical-bounds validator is helpful, but it does not consistently protect parser-level operations that perform arithmetic before validation.

Examples include SRF wind conversion and Meteonomiqs-derived values.

A malformed provider payload using a string or unexpected object can therefore fail with a type error before the common validation layer gets a chance to classify the value.

### Recommendation

Normalize provider numerics through one defensive helper:

- `None` → no-data;
- numeric finite → accepted candidate;
- numeric string → explicitly decide whether coercion is allowed;
- everything else → rejected with source-specific diagnostics.

---

## SWF-P2-003 — CombiPrecip grid metadata has insufficient defensive validation

**Severity:** P2 / Medium  
**Status:** Static finding

`_pixel_indices()` assumes valid, nonzero raster extents and valid geographic bounds. A malformed HDF5 product could cause:

- division by zero;
- inverted extents;
- invalid dimensions;
- nonsensical coordinate transforms.

### Recommendation

Validate:

- `xsize > 0`;
- `ysize > 0`;
- `UR_easting > LL_easting`;
- `UR_northing > LL_northing`;
- finite geographic metadata.

Reject the file as malformed instead of allowing arithmetic exceptions or misleading extraction.

---

## SWF-P2-004 — Full HA runtime/lifecycle test remains outstanding

**Severity:** P2 / Verification gap  
**Status:** Not executable in supplied environment

The package relies heavily on Home Assistant behavior for:

- DataUpdateCoordinator listener scheduling;
- setup/unload ordering;
- config-flow migration;
- entity registration;
- executor-job behavior;
- reload semantics;
- timezone behavior.

The source contains extensive defensive lifecycle logic, but without the HA runtime these paths cannot be promoted to a full PASS.

### Recommendation

Run the release candidate in a disposable HA instance and exercise:

1. first install;
2. upgrade from v0.1.23;
3. upgrade from v0.1.24;
4. upgrade from v0.1.25;
5. options reload;
6. coordinate reconfigure;
7. credential reauth;
8. failed setup at every coordinator construction point;
9. unload during active network request;
10. restart during SQLite transaction;
11. restart after provider call reservation;
12. database corruption;
13. missing `.storage`;
14. provider outage;
15. provider HTTP 401/403/429/5xx;
16. stale radar feed;
17. DST spring/fall transitions.

---

# 6. Areas That Passed Review

## 6.1 Python source integrity

**PASS**

All production Python source files successfully parsed through the Python AST and compiled through `compileall`.

No syntax defects were found.

## 6.2 Metadata/package integrity

**PASS**

Successfully parsed:

- `manifest.json`;
- `hacs.json`;
- `translations/en.json`.

Both shipped PNG icons passed image integrity verification.

## 6.3 Unsafe execution primitives

**PASS**

No production use was found for:

- `eval()`;
- `exec()`;
- `os.system()`;
- unsafe pickle loading;
- shell execution.

## 6.4 SQLite connection serialization

**PASS with qualification**

The storage layer uses a process-local `threading.Lock` around SQLite connection access, and the integration consistently moves blocking database operations into executor jobs.

The reconciliation write path also uses an explicit transaction/rollback boundary, which is a strong design choice for preventing a learned EMA update from being committed without the corresponding forecast-row status transition.

## 6.5 Forecast physical bounds

**PASS for the providers routed through the common validation layer**

`provider_validation.py` establishes finite/domain bounds for temperature, humidity, pressure, precipitation, and wind speed.

The review specifically verified that Open-Meteo and SRF forecast rows pass through this layer.

The Meteonomiqs Model B risk path is a separate domain and is not protected by this validator — hence SWF-P1-003.

## 6.6 Coordinate and unit handling

**Generally PASS**

The package contains explicit handling for:

- Celsius/Fahrenheit/Kelvin;
- pressure units;
- humidity;
- station pressure-to-sea-level reduction;
- Open-Meteo wind speed in m/s;
- SRF km/h-to-m/s conversion.

Boundary testing confirmed the unit conversion functions reject unknown units rather than silently guessing.

## 6.7 Diagnostic redaction design

**Generally PASS**

The diagnostic layer has substantially stronger protection than a simple top-level credential scrub:

- recursive sensitive-key redaction;
- coordinate-string redaction;
- secret-value replacement;
- nested payload handling.

The implementation is appropriately defensive for shareable diagnostic material.

---

# 7. Less-Traveled Path Assessment

The following paths were explicitly reviewed because they are unlikely to receive normal happy-path traffic:

| Path | Assessment |
|---|---|
| Provider HTTP 401 | Reviewed; explicit auth classification exists |
| Provider HTTP 403/4xx | Reviewed; SRF permanent-error path exists |
| Provider 5xx | Reviewed; transient/fallback behavior exists |
| SRF token invalidation | Reviewed; one forced token refresh/retry exists |
| SRF fallback endpoint | Reviewed |
| Open-Meteo array length mismatch | Reviewed |
| Meteoblue array length mismatch | Reviewed |
| Persisted state corruption | Reviewed; syntax-level corruption handled |
| SQLite transaction failure | Reviewed; rollback exists |
| DB schema loss/version metadata loss | Reviewed |
| Duplicate provider-run fingerprints | Reviewed |
| Forecast reconciliation retry | Reviewed |
| Forecast reconciliation give-up | Reviewed |
| Radar stale timestamp | Reviewed |
| Radar low quality code | Reviewed |
| Radar out-of-grid boundary | **FAIL — SWF-P1-002** |
| Meteonomiqs bonus budget | Reviewed |
| Meteonomiqs scheduled budget | **FAIL — SWF-P1-001** |
| Meteonomiqs risk scale | **FAIL — SWF-P1-003** |
| Future-dated station samples | Reviewed |
| DST scheduling | Reviewed statically |
| Setup partial failure cleanup | Reviewed statically |
| Unload during active coordinators | Reviewed statically; HA runtime unavailable |
| Database removal/WAL/SHM cleanup | Reviewed statically |

---

# 8. Defect Priority Summary

| ID | Severity | Finding | Release blocker |
|---|---|---|---|
| SWF-P1-001 | P1 | Meteonomiqs quota consumed without actual call | **Yes** |
| SWF-P1-002 | P1 | CombiPrecip edge point can map to valid edge pixel | **Yes** |
| SWF-P1-003 | P1 | Invalid Meteonomiqs risk can create >100% score | **Yes** |
| SWF-P2-001 | P2 | Persisted quota state lacks semantic validation | Recommended |
| SWF-P2-002 | P2 | Parser numeric typing is inconsistent | Recommended |
| SWF-P2-003 | P2 | Radar grid metadata lacks defensive validation | Recommended |
| SWF-P2-004 | P2 | Full HA lifecycle execution unavailable | **Verification blocker** |

---

# 9. Required Remediation Before Release

### Mandatory

1. **Fix Meteonomiqs call accounting**
   - never increment annual budget on a no-op schedule check;
   - reserve exactly once per real provider call;
   - preserve an explicit hard annual limit.

2. **Fix raster index math**
   - reject continuous coordinates outside the grid;
   - do not use truncation-toward-zero as an out-of-range test.

3. **Validate Meteonomiqs risk**
   - enforce numeric/integer semantics;
   - enforce 0–9 range;
   - enforce final Model B score `[0.0, 1.0]`.

### Strongly recommended

4. Validate persisted quota state semantically.
5. Harden all provider numeric parsing before arithmetic.
6. Validate HDF5 geographic/raster metadata.
7. Install Home Assistant in the verification environment and execute a real lifecycle matrix.

---

# 10. Recommended Regression Suite

The following tests should be added to the release gate, independently of the existing excluded test suite:

### Quota invariant

- Before-noon seasonal coordinator check → **0 calls used**.
- Second pre-noon check → **0 calls used**.
- Actual noon request reservation → **+1**.
- Failed HTTP request → accounting behavior explicitly matches provider billing semantics.
- Budget at limit → no further provider call.
- Restart after reservation → no double reservation.

### Radar boundary invariant

Test points:

- just inside west;
- exactly west edge;
- just outside west;
- just inside east;
- exactly east;
- just outside east;
- same six cases for north/south;
- corner combinations.

Expected result for all outside points: `None`.

### Risk invariant

For risk:

- `None`;
- `0`;
- `9`;
- `-1`;
- `10`;
- `99`;
- `9.5`;
- `"9"`;
- `"invalid"`;
- NaN/Infinity.

Expected final score must always be `None` or `[0, 1]`.

### Persistence invariant

Test:

- negative `calls_used`;
- calls_used > annual budget;
- non-integer calls_used;
- invalid year;
- corrupted JSON;
- restart after valid state.

### HA lifecycle

Execute the complete setup → first refresh → steady state → options reload → unload → reload → remove lifecycle with active network and database activity.

---

# 11. Final ICS-Quality Assessment

**FINAL STATUS: FAIL**

The package demonstrates a strong remediation-oriented architecture and contains many good defensive mechanisms, especially around SQLite transactionality, provider fault isolation, physical bounds, diagnostics redaction, and lifecycle cleanup.

However, the current release **does not meet an ICS-quality release gate** because three independently confirmed defects remain in operationally significant paths:

- resource/quota accounting;
- geospatial data integrity;
- derived risk-domain integrity.

The defects are particularly important because they are **silent or plausible-data failures rather than obvious crashes**. In an operational system, those are more dangerous: the integration can remain apparently healthy while producing incorrect decisions or consuming a finite external resource.

**Release recommendation: HOLD.**

Remediate SWF-P1-001 through SWF-P1-003, execute the HA runtime verification matrix, and repeat the boundary/adversarial audit before declaring v0.1.26 release-ready.
