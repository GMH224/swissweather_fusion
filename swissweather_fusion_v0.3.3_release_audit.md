# SwissWeather Fusion v0.3.3 — Release Audit (ICS)

**Release type:** feature (solar radiation for a downstream solar layer) plus two
display/diagnostics defects (backlog items 19, 20).
**Shipped during:** the Class A accuracy trial (window opened with v0.3.1/v0.3.2,
closes end of November 2026). Every change below was checked against one rule:
*it must not alter what the trial measures.*
**Verification:** 826 tests passing (795 carried + 31 new), package pyflakes
clean, 16 targeted mutations all caught (one only after a test was strengthened,
§5). No schema change, no migration, no learning reset.

---

## 1. What was added

| Item | Detail |
|---|---|
| Solar radiation | GHI, DNI, DHI from ICON-CH1, ICON-CH2, ICON-D2 via Open-Meteo, as hourly averages and as instants (six variables). Fused as a mean over models supplying a complete triple from one model run. Six sensors; the GHI average sensor carries the hourly series (`hourly_forecast`, excluded from the recorder). |
| Snow depth | Fetched and fused (median) since v0.2.0, never exposed. Now a sensor. |
| SRF global irradiance | Stored since v0.2.0 as `srf_irradiance`, never read. Now a separate, unfused sensor with storage bounds (0–1500 W/m²). |

**Deliberately not added** (owner decisions, recorded so they are not mistaken
for gaps):

- **No GTI, no panel geometry.** Tilted irradiance depends on each array's tilt
  and azimuth; that belongs to the solar layer. Adding an array must never touch
  the weather model.
- **No 15-minute data.** MeteoSwiss publishes ICON-CH1/CH2 at 1 h. Only ICON-D2
  is native sub-hourly; one native source among interpolated ones would be false
  precision. No public MeteoSwiss announcement of sub-hourly ICON-CH output was
  found (the announced 2027–28 item is the nowcasting successor to INCA).
- **No bias correction of radiation.** Needs inverter output as ground truth —
  the planned solar component (W6), not this integration.

### Semantics published to consumers

- Series keyed by hour start: `period_start` / `period_end` (UTC).
- `ghi`/`dni`/`dhi` = average over `[period_start, period_end)`. Open-Meteo labels
  averages at the END of the hour; the series pairs the hour starting at T with
  the average labelled T + 1 h. Consumers must not shift again.
- `*_instant` = value at `period_start`.
- `sources` / `instant_sources` = models contributing a complete triple
  (3 near-term; ICON-CH1 ends ~33 h, ICON-D2 ~48 h, ICON-CH2 ~120 h).

---

## 2. Design decisions with physical justification

**Triple fusion (not per-parameter).** Within a model GHI = DHI + DNI·cos(zenith);
the zenith is common to every model at a place and time. A mean over one set of
models preserves the identity. Two rules enforce "one set":

1. *Complete triples only* — a model missing any component contributes to none.
2. *One model run per triple* — all three must share `issued_at`; freshest-row
   selection is per variable and would otherwise patch a gap from an older run.

A median breaks the identity (component medians generally come from different
models — shown numerically in the tests), so radiation never reaches
`_fuse_class_b`; routing is enforced behaviourally, not by text search.

**SRF kept separate.** SRF gives one value with no direct/diffuse split, so
mixing it into GHI would break the identity. It is retained because it is the
only radiation source outside the ICON family. SRF documents it only as "Global
irradiance in W/m²" ([SRF Meteo API summary](https://developer.srgssr.ch/sites/default/files/inline-files/2023-05-10%20srf_meteo_api_commercial_eng_dok.pdf));
averaging basis and label convention are undocumented, so values pass through at
SRF's own timestamps, uninterpreted, and the entity says so.

---

## 3. Trial-safety analysis

| Risk | Mitigation | Test |
|---|---|---|
| New variables change the Open-Meteo run fingerprint → current run re-stored at upgrade → same forecast errors folded into `bucket_stats` twice | Radiation parsed via `_PARSED_VARIABLE_NAME_MAP`, excluded from the fingerprint map; fingerprint pinned to the v0.3.2 algorithm | `test_the_run_fingerprint_is_unchanged_by_radiation`, `..._matches_the_v0_3_2_algorithm` |
| A model refusing a new variable takes temperature/humidity/pressure offline | Radiation in the optional set; 400 → retry that model without it | `test_a_rejection_drops_the_optional_set_for_that_model_only` |
| Radiation reaches learning | Learning reconciles temperature/humidity/pressure only | `test_radiation_never_reaches_the_learning_loop` |
| Schema / migration | None: forecast rows are stored generically per variable | full suite |

**Consequence accepted:** because the fingerprint does not change, the run that
is current at upgrade is (correctly) recognised as already stored and skipped.
Radiation therefore appears with the next new upstream run: ≤3 h (CH1, D2),
≤6 h (CH2).

**Cost:** Open-Meteo request grows from 24 to 30 variables (23 core + UV + 6
radiation); Open-Meteo weights requests by variable count, fetched only on new
runs. Storage grows ≈25 % in Open-Meteo rows per run (6 new variables on 24); existing retention applies.

---

## 4. Defects found and fixed

### SWF-033-001 (High, pre-existing since v0.2.2) — optional-variable fallback fired on transient errors, for all models
Any "data"-class error on a request carrying optional variables disabled them
**until Home Assistant restarted** — and "data" includes HTTP 503 and timeouts,
which the owner's diagnostics show on all three models. Each 503 silently removed
UV index; with radiation in the set it would have removed radiation too. The flag
was also a single bool, although its own comment said "that source".
**Fix:** only `OpenMeteoRequestRejected` (HTTP 400) triggers it, per model.
Mutations M2/M3 caught.

### SWF-033-002 (Medium) — an HTTP 400 without a readable reason escaped as an unrelated error
The client raised a typed error only when the 400 body parsed as JSON with a
`reason`; otherwise a plain HTTP error, or a JSON decode error for a non-JSON
body. **Fix:** every 400 raises `OpenMeteoRequestRejected` (a `ValueError`, so
health classification is unchanged). Mutation M11 caught.

### Backlog item 20 (Medium, since v0.2.6) — Wetter-Alarm health sensors permanently blind
`sensor._get_health` had no Wetter-Alarm branch, so its four telemetry sensors
read `unknown` / `0` regardless of state. Field-confirmed 2026-09-29: coordinator
healthy (`poi_id` 145140 at 0.5 km, no errors in 21 h of log), sensors blind.

### Backlog item 19 (Low, since v0.2.6) — diagnostics export omitted Wetter-Alarm
Hard-coded source tuple in `diagnostics.py`.

**Fix for 19 + 20:** both now derive from `const.SOURCE_HEALTH_OWNER`; a test
fails if any telemetry source lacks an owner, and another checks every owner is a
real runtime key. Mutations M8/M9 caught.

### SWF-033-003 (Medium, process) — the diagnostics smoke test cited since v0.1.22 did not exist
`diagnostics.py` referenced `tests/test_diagnostics.py::test_async_get_config_entry_diagnostics_smoke`
as the closure of a real production crash. No such test existed; nothing called
the export end to end. That export is the November deliverable. **Fix:** an
end-to-end test now calls it, JSON-encodes the result as Home Assistant does, and
checks redaction and source coverage. The comment is corrected.

---

## 5. Defects in this pass's own work (caught before release)

- **Median example proved nothing.** The first draft's example triples took all
  three component medians from one model, so the identity held by accident.
  Replaced with medians from different models.
- **Docstring false positive (repeat of v0.3.1).** A source-text check for
  `_fuse_class_b` matched the word in `_fuse_radiation`'s own docstring.
  Replaced with a behavioural check that fails if the per-parameter path is
  reached.
- **Mutation M15 escaped first time.** The SRF-isolation test placed SRF values
  only where average labels did not coincide and checked only the averaged
  triple; SRF leaking into the instant triple went undetected. Test now covers
  every hour and both bases; M15 caught.

### Existing tests changed (with reason)

Two reachability guards (`test_v0_2_2_ics_audit`, `test_v0_2_5_convective`)
asserted every registered Class B parameter is in `MEASUREMENTS`. Radiation is
fused on its own path by design. The guards now accept that path **only if** it
covers exactly the radiation set, so their intent — nothing registered but
unwired — is preserved. The Home Assistant test stub gained
`SensorDeviceClass.IRRADIANCE`, mirroring the real constant.

---

## 6. Corrections to earlier statements in this engagement

- **Backlog item 17 withdrawn.** I described concurrent Wetter-Alarm warnings as a
  live risk ("the headline may not be the most severe"), quoting a code comment.
  That comment describes a defect *fixed* in v0.2.6: `parse_all_alarms` sorts by
  descending priority, the headline is the most severe, all concurrent warnings
  are in the attribute.
- **SRF irradiance "never stored" (said 2026-09-27) was wrong** — it was stored as
  `srf_irradiance` since v0.2.0, just never read.

---

## 7. Mutation record

| # | Mutation | Caught by |
|---|---|---|
| M1 | fingerprint includes radiation | `test_the_run_fingerprint_is_unchanged_by_radiation` |
| M2 | fallback on any data error | `test_a_transient_error_does_not_drop_the_optional_variables` |
| M3 | fallback global | `test_a_rejection_drops_the_optional_set_for_that_model_only` |
| M4 | incomplete triples allowed | `test_an_incomplete_triple_is_excluded_from_all_three` |
| M5 | mixed-run triples allowed | `test_a_triple_patched_from_two_model_runs_is_excluded` |
| M6 | average labelled at hour start | `test_hour_entries_pair_the_right_labels` |
| M7 | instant labelled at hour end | `test_hour_entries_pair_the_right_labels` |
| M8 | Wetter-Alarm missing from owner map | `test_every_telemetry_source_has_a_health_owner` |
| M9 | diagnostics hard-coded tuple restored | `test_diagnostics_export_end_to_end_includes_every_source` |
| M10 | radiation fused with median | `test_a_median_would_break_the_identity_...` |
| M11 | 400 rejected only with a reason | `test_every_http_400_is_a_rejection` |
| M12 | SRF irradiance bounds removed | `test_srf_irradiance_now_has_storage_bounds` |
| M13 | series written to recorder | `test_solar_sensors_report_the_current_hour_...` |
| M14 | radiation routed to per-parameter path | `test_a_median_would_break_the_identity_...` |
| M15 | SRF fused into GHI | `test_srf_irradiance_is_passed_through_and_never_fused` (after strengthening) |
| M16 | snow depth entity removed | `test_the_new_entities_are_created` |

---

## 8. Verification gaps (not closable without a live system)

- **Live Open-Meteo acceptance** of the six radiation variables per model is
  documented (Open-Meteo MeteoSwiss and DWD API pages) but not exercised live.
  Mitigated: a refusal is a per-model 400 → that model retries without them.
- **Home Assistant runtime**: entity creation, the irradiance device class and
  recorder exclusion are exercised against the test stub, not a running instance.
  `_unrecorded_attributes` requires HA ≥ 2023.10; the integration declares 2024.1.
- **Carried forward unchanged:** SWF-ICS-025/029/062 (executor cancellation,
  `gather()` semantics under first-refresh failure).
- Older test files carry pre-existing pyflakes warnings (unused imports); the
  package and the new test file are clean.

---

## 9. Open backlog after v0.3.3 (for the end-of-November review)

**Decide with November data**
1. Humidity: blend loses to SRF alone at 1–12 h leads (150–200 samples/cell at
   2026-09-23). Consider "use SRF for humidity".
2. Meteonomiqs day 5–8 standalone accuracy — one-off DB analysis (raw rows stored
   under `meteonomiqs_` prefix).
12. Learning refinement: hierarchical shrinkage / sample-count-aware weights /
    n-aware alpha, or a GAM-style smooth model (cyclical hour/season, continuous
    lead). Additive, no reset. Prototype offline on the November DB first.

**Defects held (cosmetic or dormant, per the no-cosmetic-release rule)**
3. Snowfall height / cloud base zero-inflation in Class B mean fusion.
13. meteoblue `predictability` sensor dead: client reads `data_1h`, field is in
    `data_day` (daily, 7 values). Confirmed against a live response 2026-09-23.
14. Health classifier: 401/403 from keyless sources classified "auth"; success
    never clears stale error fields; auth errors invisible for non-SRF sources.
15. Dormant: a transient 403 on SRF or meteoblue raises `ConfigEntryAuthFailed`
    (false reauth). Not observed. If a reauth prompt appears, check the key
    before re-entering it.
18. Wetter-Alarm `_async_resolve_poi` exception path bypasses health (minor; not
    the cause of item 20).

**Improvements**
4. Station cross-check for humidity/temperature (pressure only today).
5. `station_pressure_is_sea_level` / `purge_days` differ between config data and
   options in diagnostics — reconcile.
6. Per-source contribution visibility for Class B.
7. Source health: distinguish "not yet scheduled" from "broken".
8. Translation-key coverage and entity-attribute-contract tests.
9. Live-HA verification gaps (above).
10. CombiPrecip 403s — observed intermittent; watch only.

**W6 — solar production model (separate component, not this integration)**
16. Design settled with the owner: learn measured production directly from
    forecast air temperature + radiation (this release's sensors); learning
    window MAM/JJA/SON; angle of incidence computed analytically from known roof
    geometry; inverter AC rating as a hard cap; continuous online learning
    absorbs soiling drift; sudden step changes are a fault signal.
    Hardware: East = Huawei SUN2000-5KTL-M1 (1 MPPT, 5 kW AC cap). West + South =
    SUN2000-10KTL-M1 (2 MPPT, one shared 10 kW AC cap → model as one combined
    target). Per-MPPT 11 A is not the binding limit (DC oversizing ~1.5×).

**Closed in v0.3.3:** 19, 20. **Withdrawn:** 17.
