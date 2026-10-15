# SwissWeather Fusion v0.3.4 — Release Audit (ICS)

**Release type:** two features for downstream consumers — cloud cover by layer, and live measurements from the nearest
MeteoSwiss station (§6) — plus one correction (SRF irradiance timing, §5).
**Shipped during:** the Class A accuracy trial (closes end of November 2026;
diagnostics download planned 13–15 November). Every change was checked against
one rule: *it must not alter what the trial measures.* Changes that touch Class A
learning are reserved for v0.4.0.
**Verification:** 893 tests passing (826 carried — one deliberately updated (§5)
and two guard fixtures extended for the new coordinator (§6) — and 67 new),
package pyflakes clean, 32 targeted mutations all caught (one only after a test
was strengthened, §6). No schema change, no migration, no learning reset.

---

## 1. What was added

| Item | Detail |
|---|---|
| Cloud cover by layer | `cloud_cover_low`, `cloud_cover_mid`, `cloud_cover_high` from ICON-CH1, ICON-CH2, ICON-D2 via Open-Meteo (native model output per Open-Meteo's MeteoSwiss model documentation). Stored as `cloud_coverage_low/_mid/_high`, fused as the mean of the models that supply them, bounds 0–100 %. |
| Output | Three extra keys on every entry of the weather entity's **hourly** forecast, returned by `weather.get_forecasts` (owner requirement: "it must show in `weather.get_forecasts`, type hourly"). Also present in the blend's `current` values. |
| SRF irradiance timing | Series re-keyed by `period_start` / `period_end` (SRF labels the end of its interval, measured). §5. |
| Reference station | Measured 10-minute data from the nearest MeteoSwiss SwissMetNet station with global radiation (Tänikon for the reference installation), or a configured one. Ten key sensors with history, one dataset sensor with every parameter, health telemetry, one new option. §6. |
| Documentation | README sections "Cloud cover by layer" and "Reference station", SRF timing note; DEVELOPER.md v0.3.4 entry. |

**Deliberately not added:**

- **No layers from meteoblue or SRF.** meteoblue's basic package has total cover
  only; SRF has no layer split. A test asserts nothing is mapped onto the layer
  names from meteoblue.
- **No daily / twice-daily layer values.** A consumer consumes hourly data;
  the daily aggregation rules (max of total cover) were not designed for layers
  and are left unchanged.
- **No sensors.** The owner asked for the forecast service; three more sensors
  would add recorder load without a consumer.
- **No closure between layers and total.** Layers overlap as seen from the
  ground (60 % low and 60 % high can be 70 % total); enforcing a sum would be
  physically wrong.
- **No learning or verification.** There is no local measurement of cloud
  layers. Treated as Class B like total cover.

### Semantics published to consumers

- Percent, valid **at** the hour of the entry, same as `cloud_coverage`.
- Key **absent** when no model has a value for that hour (beyond ICON's ~5-day
  horizon), never `null` and never `0` — a 0 % low layer nobody forecast would
  read as "no fog".
- Extra keys are not members of Home Assistant's `Forecast` TypedDict, so weather
  cards ignore them. `weather.get_forecasts` returns the entity's entries with
  extra keys intact; the entity passes the list through unchanged (tested).

---

## 2. Trial protection — the three ways this could have disturbed Class A

| Risk | Mechanism | Guard |
|---|---|---|
| **Run identity changes at upgrade** | Open-Meteo runs are deduplicated by a fingerprint over `_VARIABLE_NAME_MAP`. Extending that map would make each model's current run look new, store it again with a fresh `issued_at`, and fold its errors into `bucket_stats` twice. | Layers parsed via `_CLOUD_LAYER_VARIABLE_NAME_MAP` into `_PARSED_VARIABLE_NAME_MAP`, kept out of `_VARIABLE_NAME_MAP`. Tests: fingerprint identical with/without layers, and equal to the v0.3.3 algorithm (mutation M1). |
| **Class A goes offline** if a model refuses a new variable | All new variables are in the optional set; a 400 drops the optional set for that model only and retries with the core set. | Tests: core-only URL still carries temperature/humidity/pressure and no optional variable (M8). |
| **Layers enter learning** | Anything added to `LEARNED_MEASUREMENTS` or the reconciliation set changes the trial. | Tests: `LEARNED_MEASUREMENTS` and every `RECONCILIATION_MEASUREMENTS` remain exactly temperature/humidity/pressure (M5). |

Consequence for the owner, same as v0.3.3: the layers appear with the **next new
upstream run** after the upgrade (≤ 3 h ICON-CH1/D2, ≤ 6 h ICON-CH2), not
immediately.

The weather condition shown on cards is unchanged: it derives from the WMO code
and total cover only. A test confirms identical conditions with and without layer
data.

---

## 3. Residual risks (accepted, documented)

1. **The optional set is one unit per model.** If a model ever refused only a
   layer variable, that model would also lose radiation and UV until restart.
   Open-Meteo documents the layers for all three models. A refusal is visible as
   a logged HTTP 400 and an `optional_variables_dropped` diagnostics event.
   Splitting the optional set per feature was not done in a mid-trial release:
   it changes the retry logic that v0.3.3 hardened.
2. **Storage growth.** Three more stored variables per Open-Meteo model run
   (30 → 33 per hour), about +10 % Open-Meteo forecast rows, bounded by the
   configured purge window like everything else.
3. **Unverified quality.** Low cloud and fog are the weakest part of every
   weather model (11 Oct 2026: all ICON variants and SRF expected the deck to
   break up; it did not). The values are model statements, documented as such.
   A backtest of their value for radiation forecasts is planned (archive data for
   cloud layers is available at Open-Meteo).

---

## 4. Verification

**Tests (19 for the cloud layers, plus 8 for SRF in §5; `tests/test_v0_3_4_cloud_layers.py`):** request and optional
status for all three models; radiation/UV still requested; parsing into internal
names at the right labels; fingerprint unchanged and equal to the v0.3.3
algorithm; layer names outside run identity; no meteoblue mapping; registry
class, unit, mean fusion and bounds per layer; exclusion from learning and
reconciliation; end-to-end through storage (fused means per hour, model horizons,
JSON-serialisable); omission when no data; condition unchanged; current values;
weather entity pass-through; core-only fallback.

**Mutation testing (10 mutations, all killed):**

| # | Mutation | Caught |
|---|---|---|
| M1 | layer variable added to run identity | ✓ |
| M2 | layers removed from the fused set | ✓ |
| M3 | low layer fused with max | ✓ |
| M4 | mid layer missing from the forecast entry | ✓ |
| M5 | low layer added to Class A learning | ✓ |
| M6 | low/mid mapping swapped | ✓ |
| M7 | mid bounds widened to 0–1000 | ✓ |
| M8 | layer made a core (non-optional) variable | ✓ |
| M9 | high key reads the low value | ✓ |
| M10 | key written as null instead of omitted | ✓ |

**Not verifiable here:** a live Open-Meteo response and a live Home Assistant
`weather.get_forecasts` call (no network to either from the build environment).
Post-install check for the owner: run the service from Developer Tools after the
next model run and confirm `cloud_coverage_low/_mid/_high` on the first ~120
hourly entries.

---

## 5. Correction: SRF irradiance timing

v0.3.3 published SRF's global irradiance at SRF's own labels because SRF does not
document its convention. Owner data from 6–11 October 2026, compared with the
MeteoSwiss Tänikon pyranometer (61 daylight hours):

| Series | Hourly MAE | Correlation |
|---|---|---|
| Fusion GHI (hour average) | 51 W/m² | 0.90 |
| SRF, label taken as end of interval | 66 W/m² | 0.84 |
| SRF as published by v0.3.3 | 98 W/m² | 0.70 |

Supporting evidence: SRF's daily peak fell on the value labelled 14:00 every day
while solar noon was 13:12 CEST; the best lag correlation with the fusion
instant series was at -1 h. Conclusion: SRF labels the **end** of the interval,
the same convention Open-Meteo uses for its radiation averages.

**Change.** `_compute_srf_irradiance` now emits entries
`{period_start, period_end, value}` with `period_end` = SRF's label and
`period_start` = label − span (span = gap to SRF's previous label: 1 h in SRF's
hourly window, 3 h beyond; the first label uses the gap to the next label; capped
at 3 h so a data gap is never averaged across). Intervals that ended at or before
now are history and are dropped. The sensor state is the value whose period
contains now. The keys are those of the solar series; the old `time` key is gone
(deliberate interface change, announced in the README).

**Why now and not in v0.4.0.** Owner decision: every radiation series must follow
one convention, and the release had not been deployed. The change is outside
Class A: nothing learns from SRF irradiance and no other SRF variable changes.
SRF precipitation is also end-labelled, which is already the Open-Meteo
convention the blend uses, so the blend remains consistent.

**Test changed on purpose.** `test_srf_irradiance_is_passed_through_and_never_fused`
(v0.3.3) asserted the pass-through; it now asserts the end-of-interval reading.
Its second purpose — SRF never leaks into the ICON triple — is unchanged.

**Tests added (8):** value labelled T describes the hour before T; same keys as
the solar series; 3-hourly values cover 3 h; no stretching across a data gap; no
data → no current value; only a past label → no current value; first label spans
to the next; sensor semantics state the convention.

| # | Mutation | Caught |
|---|---|---|
| S1 | label treated as period start (v0.3.3 behaviour) | ✓ |
| S2 | span always 1 h | ✓ |
| S3 | no cap at gaps | ✓ |
| S4 | interval ending now kept | ✓ |
| S5 | first span fixed at 1 h | ✓ |
| S6 | current taken from the wrong entry | ✓ |

## 6. Reference station (MeteoSwiss SwissMetNet)

**What.** A new observation source, `reference_station`. Every 10 minutes the
newest record of one SwissMetNet station is fetched from MeteoSwiss open data
(`ch.meteoschweiz.ogd-smn` on data.geo.admin.ch, the host already trusted for
CombiPrecip): file `<abbr>/ogd-smn_<abbr>_t_now.csv`, ~30 kB. File layout verified
against live files by the owner's collector (10 Oct 2026); the test fixture uses
three real Tänikon records with all 32 parameter columns, empty fields included.

**Station choice.** Configured three-letter code (new option, validated in the
handler so the form schema stays serialisable), else the nearest station within
50 km whose file carries global radiation (`gre000z0`); up to five candidates are
probed in distance order. Chosen once per load; a failed resolution is retried on
the next poll. For the owner: Tänikon (TAE), ~8 km.

**Published.**

- `sensor.…_reference_station`: state = record time; attributes = all parameter
  values by MeteoSwiss short name, English descriptions and units (from
  `ogd-smn_meta_parameters.csv`, refreshed daily, optional), station identity and
  distance, age, stale flag. Dataset attributes are excluded from the recorder.
- Ten key sensors with units, device classes and long-term statistics:
  temperature, humidity, QFF pressure, global radiation, sunshine (10 min),
  incoming longwave, wind speed, gust, direction, precipitation (10 min).
  Unknown when the newest record is older than 60 minutes.
- Four health telemetry sensors, via `SOURCE_HEALTH_OWNER`; the source counts
  toward Status/Degraded like every other source.

**Semantics.** UTC; 10-minute means and sums cover the 10 minutes ending at the
record time; empty fields are null and never filled from an older record, so all
values in one dataset share one timestamp.

**Trial protection.** Observation only: the coordinator has no database access;
blend, learning and station coordinators never reference it (tested by source
inspection); it is not a forecast source; Class A sets unchanged.

**Robustness.** HTTPS to data.geo.admin.ch only; 5 MB cap, read in chunks; 120 s
budget per poll; metadata failures never fail a poll (configured station works
without the station list; values publish without descriptions); a metadata row
whose field count differs from the header is skipped rather than mislabelled.

**Lifecycle.** The coordinator appears in every lifecycle list: construction
guard, auth-failure cleanup, first refresh, `_noop` listener, platform-failure
cleanup, runtime dict, `_all_coordinators`. Two existing guard tests flagged it
until covered: the construction-coverage test and the health-visibility fixture
(both extended, not weakened).

**Tests (39 + 1 construction).** Parsing of real records (newest by timestamp,
all columns, nulls, Windows-1252); metadata parsing and ranking; parameter
descriptions incl. a malformed row; client host/scheme refusal, chunk reassembly,
size cap; auto-selection skipping a nearer rain-only station; probe reuse; data
shape and JSON; cached selection (one fetch per later poll); configured station
without metadata; no station within range (fails, never probes beyond 50 km,
retries and recovers); health on failure; optional descriptions; value sensors
(current, stale, missing); dataset sensor (32 values, age, unrecorded); health
telemetry; trial guard; lifecycle; listener coverage; option validation and
translations.

| # | Mutation | Caught |
|---|---|---|
| R1 | last line used instead of newest record | ✓ |
| R2 | empty fields dropped | ✓ |
| R3 | radiation requirement removed | ✓ |
| R4 | 50 km limit removed | ✓ |
| R5 | probe download not reused | ✓ |
| R6 | staleness check removed | ✓ |
| R7 | configured station ignored | ✓ |
| R8 | success not recorded in health | ✓ |
| R9 | dataset attributes recorded | ✓ |
| R10 | coordinator missing from the listener loop | ✓ (after strengthening) |
| R11 | description failure fails the poll | ✓ |
| R12 | host check removed | ✓ |
| R13 | size cap removed | ✓ |
| R14 | missing from the unload list | ✓ |
| R15 | option not validated | ✓ |
| R16 | descriptions for parameters the station does not report | ✓ |

**R10 finding.** The first version of the lifecycle test only counted mentions of
the coordinator in `async_setup_entry`, so removing it from the `_noop` listener
loop went unnoticed. That loop is what keeps a coordinator without entity
listeners scheduled (the v0.1.16 freeze). The new test parses the loop and
asserts it covers every runtime coordinator except the blend — a general guard,
not just for this coordinator.

**Not verifiable here:** live MeteoSwiss responses (no network from the build
environment; the format comes from the owner's collector) and Home Assistant's
entity-ID slugs. Post-install check: the reference station sensor shows a time
within ~20 minutes and `station.abbr` = TAE.

## 7. Upgrade

No migration, no learning reset, no configuration change required (the reference
station selects itself; the new option is optional). Replace the
`custom_components/swissweather_fusion` folder and restart Home Assistant.
