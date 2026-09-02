# SwissWeather Fusion v0.2.1 — ICS Quality Bug Report

## 1. Executive Summary

**Component:** `swissweather-fusion-v0.2.1`  
**Audit basis:** source package and its supplied automated test suite  
**Assessment date:** 2026-09-02  
**Overall ICS status:** **NOT PASSED**

This document is a standalone quality/defect record for the v0.2.1 package. It identifies defects that affect runtime correctness, data integrity, lifecycle behavior, feature reachability, persistence, and provider/fusion contracts.

### Severity summary

| Severity | Findings |
|---|---:|
| Critical | 1 |
| High | 5 |
| Medium | 12 |
| **Total** | **18** |

The most serious defect is a runtime-breaking CombiPrecip field-contract mismatch. Several other defects can produce incorrect weather conditions, incomplete persistence protection, missing scheduled work, or silently unavailable forecast parameters.

The integration should **not be considered ICS-quality compliant** until all Critical/High findings are fixed and the Medium findings are either fixed or explicitly accepted with regression coverage and documented behavior.

---

# 2. Defect Register

## SWF-021-001 — Current weather entity uses the obsolete condition resolver

**Severity:** High  
**Category:** Functional correctness / stale API  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/weather.py`

**Lines 155–172**

```python
@property
def condition(self) -> Optional[str]:
    ...
    return model_a.derive_condition(
        self._current.get("precip"),
        self._current.get("temperature"),
        self._current.get("humidity"),
        is_daytime=_is_daytime_now(self.hass),
    )
```

### Defect

The current weather entity uses `derive_condition()` even though the integration has a richer `resolve_condition()` implementation that accepts:

- provider weather code
- precipitation
- snowfall
- temperature
- humidity
- cloud coverage
- day/night state

The current call therefore discards provider weather-code and explicit snow/cloud evidence before the final Home Assistant condition is produced.

### Impact

Examples include:

- snow being inferred from temperature/precipitation rather than explicit snowfall evidence;
- fog/thunderstorm/drizzle classifications being lost;
- cloud coverage being ignored;
- richer provider classification being replaced by a simpler heuristic.

### Required correction

Route current-condition resolution through `model_a.resolve_condition()` and provide all available current measurements.

### Regression test

Verify current weather for at least:

- WMO snow code;
- explicit snowfall;
- 90% cloud coverage;
- fog;
- thunderstorm;
- clear night.

---

## SWF-021-002 — Daily and twice-daily aggregation uses the obsolete condition resolver

**Severity:** High  
**Category:** Data aggregation / information loss  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/models/model_a.py`

**Daily path: lines 387–416**

**Twice-daily path: lines 464–498**

The aggregation retains temperature, precipitation and humidity, then calls:

```python
derive_condition(...)
```

### Defect

Hourly evidence is reduced to aggregate precipitation, representative temperature, and humidity. Weather code, snowfall, and cloud coverage are not retained for condition resolution.

### Impact

The aggregation layer can no longer distinguish precipitation type or richer conditions after the hourly records have been collapsed.

A period containing snow followed by warmer temperatures can be reported as rainy.

### Required correction

Retain the relevant categorical/rich measurements while aggregating, including:

- weather code;
- snowfall;
- cloud coverage;
- precipitation.

Then use `resolve_condition()`.

### Regression test

Construct daily and twice-daily periods containing:

1. snowfall followed by above-freezing hours;
2. rain;
3. snow throughout;
4. high cloud cover with no precipitation;
5. mixed WMO weather codes.

---

## SWF-021-003 — Daily/twice-daily aggregation can misclassify snow as rain

**Severity:** High  
**Category:** Weather semantics / information loss  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/models/model_a.py`

**Daily:** lines 390–413  
**Twice-daily:** lines 466–495

### Defect

The aggregation computes:

```python
total_precip = sum(precips) if precips else None
```

and then derives precipitation type from the total plus representative temperature.

This loses the temporal precipitation-type information present in the hourly data.

### Impact

A period such as:

- early hours: snowfall;
- later hours: rain or no precipitation;
- representative temperature above 0°C

can become `rainy`, despite genuine snowfall occurring during the period.

### Required correction

Do not infer the period's precipitation type solely from aggregate precipitation and representative temperature. Preserve explicit snowfall/weather-code evidence.

### Regression test

Include mixed-temperature periods where snowfall occurs before temperatures rise above freezing.

---

## SWF-021-004 — `sunny` can override contradictory high cloud coverage

**Severity:** Medium/High  
**Category:** Contradictory-input policy  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/models/model_a.py`

**Lines 620–680**

Relevant logic:

```python
from_code = condition_from_weather_code(weather_code)
if from_code is not None:
    ...
    return from_code
```

### Defect

A valid WMO weather code has unconditional precedence over cloud coverage.

Therefore:

```text
weather_code = 0
cloud_coverage = 90
```

returns `sunny`.

### Impact

The weather entity can expose an internally contradictory result such as:

- `sunny`
- cloud coverage = 90%

This is especially visible in Home Assistant weather cards and automations.

### Required correction

Define an explicit contradiction policy. Possible policy choices include:

- trust provider WMO code absolutely;
- trust measured cloud coverage when it conflicts with a clear-sky code;
- classify the result as partly/cloudy when evidence conflicts.

The policy must be deliberate and regression-tested.

---

## SWF-021-005 — Storm reconciliation coordinator is not listener-registered

**Severity:** High  
**Category:** Lifecycle / scheduling  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/__init__.py`

**Lines 454–465**

The listener tuple contains:

```python
station_coordinator,
open_meteo_coordinator,
srf_coordinator,
meteoblue_coordinator,
combiprecip_coordinator,
meteonomiqs_coordinator,
model_b_coordinator,
learning_coordinator,
retention_coordinator,
```

but omits:

```python
storm_reconciliation_coordinator
```

### Defect

The storm reconciliation coordinator is constructed and included in cleanup structures, but is not given the listener registration that keeps a coordinator without a CoordinatorEntity scheduled.

### Impact

Storm reconciliation may perform its initial refresh and then fail to receive the intended ongoing scheduling behavior.

### Required correction

Register `storm_reconciliation_coordinator` in the same listener lifecycle.

### Regression test

Assert that every scheduled coordinator has a registered listener, including storm reconciliation, and verify that it refreshes after the initial call.

---

## SWF-021-006 — CombiPrecip persists a removed field name

**Severity:** Critical  
**Category:** Runtime failure / cross-module contract  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/coordinator.py`

**Lines 887–900**

The storage call uses:

```python
local.precip_rate_mmh
```

### Defect

The current radar value contract uses `precip_accum_mm_1h`, while the coordinator still accesses the removed `precip_rate_mmh` attribute.

### Impact

A successful CombiPrecip extraction can fail when the local radar value is persisted.

Consequences include:

- radar observation storage failure;
- failed CombiPrecip coordinator update;
- downstream Model B radar path degradation;
- incorrect source health state;
- loss of a successful radar scan from durable storage.

### Required correction

Use the current `RadarPixelValue` field:

```python
local.precip_accum_mm_1h
```

### Regression test

Run the complete path:

```text
CombiPrecip response
→ parser
→ coordinator
→ insert_radar_observation()
→ database row
→ Model B
```

and assert the accumulated 1-hour precipitation value.

---

## SWF-021-007 — Degraded sensor omits source-specific health semantics

**Severity:** High/Medium  
**Category:** Health monitoring / caller contract  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/binary_sensor.py`

**Lines 58–66**

Current call:

```python
not is_source_healthy(health)
```

inside:

```python
for source in ALL_TELEMETRY_SOURCES
```

### Defect

The health function supports source-specific semantics, but the binary sensor does not pass the source name.

### Impact

Per-source grace periods are lost.

A source with a longer expected polling interval can therefore be evaluated using the wrong health window.

### Required correction

Pass the source:

```python
not is_source_healthy(health, source)
```

### Regression test

Use sources with intentionally different polling/grace intervals and verify the degraded binary sensor reflects the appropriate one.

---

## SWF-021-008 — Database current-schema detection checks only three sentinel columns

**Severity:** Medium/High  
**Category:** Persistence / migration integrity  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/storage/db.py`

**Lines 302–307**

```python
looks_current = (
    "reconciliation_status" in actual.get("forecast_snapshots", set())
    and "reconciled" in actual.get("storm_predictions", set())
    and "precip_accum_mm_1h" in actual.get("radar_observations", set())
)
```

### Defect

A database is treated as current when three representative columns exist.

Other required current columns can still be missing.

### Impact

A partially migrated or damaged database can bypass migration and later fail when code accesses a missing column.

### Required correction

Define a complete required-schema map for all migration-sensitive tables and verify every required column.

### Regression test

Create a database containing all three sentinel columns while removing one additional required current column. Migration must detect the incomplete schema.

---

## SWF-021-009 — Open-Meteo optional UV fallback is not actually wired

**Severity:** Medium  
**Category:** Feature/recovery path  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/clients/open_meteo.py`

**Lines 104–117**

`build_forecast_url()` supports:

```python
include_optional: bool = True
```

and adds:

```python
OPTIONAL_HOURLY_VARIABLES = ("uv_index",)
```

However:

`custom_components/swissweather_fusion/clients/open_meteo.py`

**Lines 352–379**

`async_fetch_forecast()` has no `include_optional` parameter and always builds the default request.

`custom_components/swissweather_fusion/coordinator.py`

**Lines 121–124 and 188–190**

stores `_include_optional_variables` but does not pass it into the client.

### Defect

The intended retry-without-UV mechanism exists conceptually but is not connected to the request path.

### Impact

A provider rejection caused by the optional UV variable can still fail the source rather than degrading gracefully to the core forecast set.

### Required correction

Propagate `include_optional` through:

```text
coordinator
→ client
→ URL builder
```

and retry without optional variables when the failure is appropriate.

### Regression test

Mock a provider rejection with UV enabled and verify:

1. first request contains UV;
2. retry omits UV;
3. core forecast succeeds;
4. UV becomes unavailable rather than taking the source offline.

---

## SWF-021-010 — UV is registered/published but absent from fusion measurements

**Severity:** Medium  
**Category:** Data pipeline reachability  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/coordinator.py`

**Lines 1508–1517**

`FUSED_MEASUREMENTS` contains:

```python
"precip",
"rain",
"showers",
"snowfall",
"snow_depth",
"precip_probability",
"wind_speed",
"wind_gust_speed",
"wind_bearing",
"dew_point",
"apparent_temperature",
"cloud_coverage",
"visibility",
```

but not:

```python
"uv_index"
```

### Related locations

`forecast_parameters.py` **lines 223–224** registers `uv_index`.

`weather.py` **lines 146–148** exposes `uv_index`.

### Defect

UV exists in the parameter registry and weather entity surface but is not included in the fused measurement set used to retrieve/blend forecast values.

### Impact

The apparent end-to-end UV feature is incomplete:

```text
provider → storage → fusion → weather entity
```

does not reliably carry UV through the fusion layer.

### Required correction

Add UV to the appropriate measurement pipeline and ensure provider-specific availability is handled correctly.

### Regression test

Verify UV from at least one provider survives:

```text
provider
→ parser
→ DB
→ Model A
→ weather entity
```

---

## SWF-021-011 — Sunshine duration is registered but has no confirmed executable end-to-end path

**Severity:** Medium  
**Category:** Feature contract / dead path  
**Status:** Confirmed architecture gap

### Location

`custom_components/swissweather_fusion/forecast_parameters.py`

**Lines 225–227**

```python
"sunshine_duration": _p(
    "sunshine_duration",
    ParameterClass.FUSED,
    "min",
    fuse_mean,
    0,
    60,
)
```

### Defect

The parameter is declared as a fused forecast parameter, but no confirmed provider → storage → fusion → entity/output path exists for it.

### Impact

The registry advertises a feature that is not actually reachable end-to-end.

### Required correction

Either:

1. implement the full pipeline; or
2. remove/de-scope the parameter until an executable provider contract exists.

### Regression test

Add a fixture-backed end-to-end test proving the value reaches its intended Home Assistant surface.

---

## SWF-021-012 — Constructor-stage failures can occur before centralized cleanup

**Severity:** Medium  
**Category:** Lifecycle / resource safety  
**Status:** Confirmed risk

### Location

`custom_components/swissweather_fusion/__init__.py`

**Coordinator construction:** lines 185–258

The first-refresh cleanup begins only after construction, at lines 308 onward.

### Defect

Coordinator construction occurs before the centralized setup failure cleanup region.

If an exception is raised during construction of a later coordinator, the earlier constructed objects and the opened database may not pass through the later cleanup path.

### Impact

Failure injection during constructor-stage setup can leave partially initialized resources.

This is especially relevant to:

- database lifecycle;
- coordinator objects;
- future constructors acquiring resources;
- reload/retry behavior.

### Required correction

Put coordinator construction under an explicit cleanup guard or build a single owned-resource collection whose cleanup is safe from every setup stage.

### Regression test

Inject constructor failures at each coordinator creation point and verify:

- DB closed;
- already-created coordinators shut down;
- no listener/task leakage;
- next setup attempt succeeds.

---

## SWF-021-013 — Provider physical validation does not cover the expanded parameter set

**Severity:** Medium  
**Category:** Validation / data integrity  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/provider_validation.py`

**Lines 43–49**

Current bounds cover only:

```python
temperature
humidity
pressure
precipitation
wind_speed
```

### Defect

The expanded parameter registry includes additional quantities such as:

- rain;
- showers;
- snowfall;
- snow depth;
- precipitation probability;
- wind gust speed;
- wind bearing;
- dew point;
- apparent temperature;
- cloud coverage;
- visibility;
- UV index;
- sunshine duration.

These do not have corresponding provider-level physical bounds.

### Impact

The shared provider storage guard does not protect the complete expanded schema. Invalid expanded values can reach durable storage even if later fusion validation rejects them.

### Required correction

Define validation from the canonical parameter registry or otherwise guarantee complete, synchronized coverage.

Do not invent ranges without explicit domain decisions.

### Regression test

Inject out-of-range, NaN and infinity values for every expanded parameter.

---

## SWF-021-014 — Open-Meteo duplicate runs produce misleading coordinator data

**Severity:** Medium  
**Category:** State contract / caching  
**Status:** Confirmed

### Location

`custom_components/swissweather_fusion/coordinator.py`

**Lines 174–234**

At lines 228–234:

```python
previous_fingerprint = await self._get_persisted_fingerprint(source)
if (
    previous_fingerprint is not None
    and parsed.run_fingerprint == previous_fingerprint
):
    continue
```

### Defect

Duplicate upstream runs are correctly skipped for persistence, but the source is also omitted from the current `results` dictionary.

If all sources are unchanged, the coordinator can return:

```python
{}
```

despite having healthy persisted forecasts.

### Impact

The DB retains the data, and Model A currently reads persisted data, so this is not primarily a data-loss defect.

It is nevertheless an inconsistent public coordinator-state contract.

Future entities, diagnostics, or callers can interpret the missing source as unavailable.

### Required correction

Separate:

```text
nothing new to persist
```

from:

```text
no current data exists
```

On duplicate runs, retain the previous in-memory result or expose the persisted cached result.

### Regression test

Verify:

- all sources duplicate → all remain represented;
- one changes → unchanged sources remain represented;
- restart + duplicate run → cached sources remain available.

---

## SWF-021-015 — Meteoblue expanded parameter coverage is incomplete

**Severity:** Medium  
**Category:** Provider capability / feature contract  
**Status:** Confirmed coverage gap; provider-specific availability requires schema verification

### Location

`custom_components/swissweather_fusion/clients/meteoblue.py`

**Lines 205–214**

The executable `_FIELD_MAP` contains only:

```python
temperature
relativehumidity
sealevelpressure
precipitation
windspeed
```

The comments identify additional response fields, including:

```text
felttemperature
uvindex
predictability
rainspot
```

### Defect

The common forecast parameter registry is broader than the Meteoblue mapping.

### Impact

Meteoblue cannot participate in the expanded multi-provider fusion for unmapped parameters.

This is not necessarily a provider defect: undocumented or unconfirmed response formats should not be guessed.

### Required correction

Verify the actual Meteoblue response schema and map only confirmed fields. Add parser fixtures for each newly supported field.

### Regression test

For every supported expanded Meteoblue field:

```text
fixture
→ parser
→ canonical variable
→ DB
→ fusion
```

---

## SWF-022-001 — Canonical precipitation validation key does not match the parameter vocabulary

**Severity:** Medium/High  
**Category:** Data integrity / validation contract  
**Status:** Confirmed new defect

### Location

`custom_components/swissweather_fusion/provider_validation.py`

**Lines 41–49**

The bounds table contains:

```python
"precipitation": (0.0, 500.0)
```

but the canonical internal variable used by the forecast pipeline is:

```text
precip
```

### Related location

`custom_components/swissweather_fusion/coordinator.py`

**Lines 277–290**

Provider points are passed directly to:

```python
provider_validation.validate_forecast_rows(rows)
```

The canonical point variable is `point.variable`.

`custom_components/swissweather_fusion/coordinator.py`

**Lines 1509–1517**

The fusion vocabulary explicitly uses:

```python
"precip"
```

### Defect

`validate_forecast_value()` performs:

```python
bounds = PHYSICAL_BOUNDS.get(variable)
```

An incoming canonical variable of `"precip"` therefore receives no physical bound and is returned as long as it is finite.

The `"precipitation"` entry in `PHYSICAL_BOUNDS` does not protect canonical `"precip"` rows.

### Impact

The provider-independent storage validation layer can accept physically implausible total precipitation values such as:

```text
9999 mm
```

even though the code appears to define a precipitation upper bound.

This is a genuine validation-boundary failure, not merely a missing expanded parameter.

### Required correction

Use one canonical vocabulary everywhere.

Preferred solution:

- derive validation names/bounds from `forecast_parameters.PARAMETERS`; or
- change the validation key to `"precip"` and add explicit mappings for provider-facing names only at the provider boundary.

Avoid maintaining independent parameter-name dictionaries.

### Regression test

Assert:

```text
validate_forecast_value("precip", 500)   → accepted
validate_forecast_value("precip", 500.1) → rejected
validate_forecast_value("precip", 9999)  → rejected
validate_forecast_value("precip", NaN)   → rejected
validate_forecast_value("precip", inf)   → rejected
```

Also verify Open-Meteo, SRF and Meteoblue rows use the canonical key expected by validation.

---

# 3. Cross-Defect Risk Classification

## 3.1 Runtime-breaking defects

### SWF-021-006 — CombiPrecip stale field

This is the clearest immediate runtime defect. A successful radar parse can fail during persistence because the coordinator references a removed attribute.

**Priority: P0**

---

## 3.2 Incorrect user-visible weather semantics

These defects can directly produce wrong weather conditions:

- SWF-021-001 — current condition resolver;
- SWF-021-002 — aggregate condition resolver;
- SWF-021-003 — snow/rain information loss;
- SWF-021-004 — contradictory sunny/cloud policy.

**Priority: P1**

---

## 3.3 Lifecycle and scheduling defects

- SWF-021-005 — storm reconciliation listener;
- SWF-021-012 — constructor-stage cleanup.

These can cause work to stop after startup or leave partial resources after setup failure.

**Priority: P1/P2**

---

## 3.4 Persistence and validation defects

- SWF-021-008 — incomplete schema detection;
- SWF-021-013 — incomplete physical validation;
- SWF-022-001 — canonical precipitation validation mismatch.

The new SWF-022-001 finding is particularly important because it demonstrates that a validation table can appear to cover a quantity while actually missing the project's canonical variable name.

**Priority: P1/P2**

---

## 3.5 Feature reachability defects

- SWF-021-009 — UV fallback;
- SWF-021-010 — UV fusion;
- SWF-021-011 — sunshine duration;
- SWF-021-015 — Meteoblue expanded coverage.

These are less likely to break the core integration but mean advertised/registered capabilities are not consistently available end-to-end.

**Priority: P2**

---

# 4. Recommended Fix Order

## P0 — Must fix before ICS retest

1. **SWF-021-006** — CombiPrecip stale `precip_rate_mmh`.
2. **SWF-022-001** — canonical `precip` validation mismatch.

## P1 — Must fix before ICS acceptance

3. **SWF-021-001** — current condition resolver.
4. **SWF-021-002 / SWF-021-003** — aggregate condition evidence loss and snow/rain misclassification.
5. **SWF-021-004** — contradictory weather-code/cloud policy.
6. **SWF-021-005** — storm reconciliation scheduling.
7. **SWF-021-007** — source-specific health semantics.
8. **SWF-021-008** — complete schema detection.

## P2 — Required for robustness / feature closure

9. **SWF-021-009** — UV optional fallback.
10. **SWF-021-010** — UV fusion reachability.
11. **SWF-021-011** — sunshine duration.
12. **SWF-021-012** — constructor cleanup.
13. **SWF-021-013** — expanded physical validation.
14. **SWF-021-014** — duplicate-run coordinator state.
15. **SWF-021-015** — Meteoblue expanded parameter coverage.

---

# 5. Required ICS Verification Matrix

| Test domain | Required verification |
|---|---|
| Normal provider fetch | All providers parse and persist valid data |
| Empty payload | No crash; source health reflects degraded state |
| Partial payload | Available fields persist without corrupting the run |
| Wrong types | Strings/objects/lists in numeric fields are rejected safely |
| NaN / infinity | Never reach durable storage |
| Physical bounds | Every canonical forecast parameter is validated |
| Precipitation | `precip` specifically tested at and beyond bounds |
| Array mismatch | Diagnostic warning plus deterministic truncation behavior |
| Timeout | Coordinator recovers on next scheduled cycle |
| Authentication | Reauth behavior and secret redaction verified |
| Duplicate run | No duplicate persistence while cached data remains exposed |
| Changed run | New data is persisted and exposed |
| Restart | Fingerprint and cached-source semantics remain correct |
| CombiPrecip | Successful extraction reaches DB and Model B |
| Condition | WMO code, snowfall, cloud coverage and fallback precedence tested |
| Contradiction | Clear-code + high-cloud case has an explicit expected result |
| UV | Provider → parser → DB → fusion → entity |
| Sunshine | Provider → parser → DB → fusion → output, or explicitly removed |
| Storm reconciliation | Initial and scheduled refresh both execute |
| Constructor failure | Every partial-construction stage cleans up correctly |
| Schema migration | Any missing required current column triggers migration |
| Unload | All coordinators stop and no tasks/listeners leak |

---

# 6. ICS Exit Criteria

The integration should not receive an ICS PASS until all of the following are true:

- **0 Critical defects open.**
- **0 High defects open.**
- Every canonical forecast variable has one authoritative validation vocabulary.
- Provider → parser → coordinator → DB → fusion → Home Assistant paths are demonstrated for every advertised feature.
- Current, daily and twice-daily conditions preserve sufficient evidence for precipitation type and cloud state.
- Contradictory weather evidence has an explicit tested policy.
- All scheduled coordinators have verified lifecycle/listener registration.
- Partial setup failures leave no open DB connection, scheduled coordinator, listener or task.
- Schema validation verifies the complete required current schema.
- Duplicate upstream runs do not make healthy cached sources appear absent.
- Fault injection covers malformed, non-finite, physically impossible, timeout, authentication, duplicate, changed-run, restart and unload-during-fetch cases.
- Automated tests pass in an environment containing the real runtime dependencies used by the integration.

---

# 7. Final ICS Assessment

**ICS QUALITY GATE: NOT PASSED**

The v0.2.1 package contains **18 identified defects/gaps** spanning:

- runtime contracts;
- weather-condition semantics;
- data aggregation;
- lifecycle/scheduling;
- database migration;
- physical validation;
- coordinator state;
- provider capability mapping;
- feature reachability.

The highest-risk issue is the CombiPrecip stale field access. The most important newly identified data-integrity issue is the canonical precipitation validation mismatch: the provider validation layer defines a bound for `"precipitation"` while the actual canonical pipeline variable is `"precip"`, leaving that value unbounded at the storage-validation boundary.

A successful ICS retest should be performed only after the P0/P1 defects are corrected and the fault-injection/end-to-end matrix above has been executed.
