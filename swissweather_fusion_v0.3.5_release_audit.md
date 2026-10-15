# SwissWeather Fusion v0.3.5 — Release Audit (ICS)

**Release type:** feature. One additive attribute, `recent_values`, on the
reference station sensor, requested by a downstream consumer ("keep every 10-minute station
record", 13 Oct 2026).
**Shipped during:** the Class A accuracy trial (closes end of November 2026).
Checked against the trial rule — *it must not alter what the trial measures* — and
against the version rule: nothing here touches Class A learning, so it is a
v0.3.x release.
**Verification:** 913 tests passing (893 carried unchanged, 20 new), package
pyflakes clean, 13 targeted mutations all caught (§4). No schema change, no
migration, no learning reset, no configuration change.

---

## 1. Problem

A consumer counts an hour as measured only when at least 5 of its 6
ten-minute records are known. The reference station sensor (v0.3.4) published
only the newest record. On the owner's system, between 04:40 and 07:40 UTC on
13 Oct, it showed 10 of 19 records, so no hour ever reached 5 of 6 and the solar
learner could not start.

**Cause.** MeteoSwiss publishes 10-minute data with a varying delay (its
documentation gives a 20-minute update interval for 10-minute files). The
integration polls every 10 minutes. When the delay changes between polls, one
poll sees the same newest record as the previous one and the next poll has
already moved on: a record is never the newest one at the moment of a poll.

**Why a buffer of polled values would not help** (the request makes the same
point): a skipped record was never polled.

## 2. What changed

| Item | Detail |
|---|---|
| Source | The file already downloaded every poll, `ogd-smn_<abbr>_t_now.csv`. MeteoSwiss documents it as "the most recent realtime data from yesterday 12UTC to now", updated every 10 minutes — so it always holds at least 12 hours of records, including those a poll skipped. **No extra download.** |
| Parser | `smn.parse_records(text, window)`: every record with `newest − window < time ≤ newest`, oldest first. Shares row parsing with `parse_latest` (`_parse_rows`, `_observation`). |
| Coordinator | `data["recent"] = [{"time": ISO UTC, "values": {...}}, ...]`. |
| Sensor | `recent_values` attribute on `sensor.swissweather_fusion_reference_station`; added to `_unrecorded_attributes`; the `semantics` text describes it. |
| Constant | `REFERENCE_STATION_RECENT_WINDOW = 6 h` (36 records). |
| Version | manifest 0.3.5. |

## 3. Request rules — compliance

| # | Rule (request §2) | How it is met | Test |
|---|---|---|---|
| 1 | `time` = end of interval, UTC, ISO with offset, on the 10-minute grid | MeteoSwiss `reference_timestamp` (end of interval for z-parameters) → `isoformat()` of a UTC datetime (`+00:00`). Off-grid rows are skipped. | iso, grid |
| 2 | Same parameters and short names as `values` | Same column parser; last entry equals `values` exactly | names, coordinator |
| 3 | Every published record, also those polling skipped | Read from the file, not from what was polled; simulated varying delay: latest-only sees fewer records, `recent_values` has 6 of 6 for every hour | delay simulation |
| 4 | Nothing invented; missing value null; unpublished record absent | Empty field → null; absent row stays absent; window not stretched to compensate | gap, nulls |
| 5 | Ascending, no duplicate times | Sorted; first row wins on a duplicate timestamp (same rule as `values`) | order, duplicate |
| 6 | 3 h ≤ window ≤ 6 h | 6 h, 36 records | window |
| 7 | Not recorded | In `_unrecorded_attributes` | sensor |
| 8 | Complete at the first update after a restart | Rebuilt from the file on every poll; no memory involved | restart, no-buffer |
| 9 | Everything else unchanged | Attribute set = v0.3.4 set + `recent_values`; state, `values`, sensors and the 60-minute rule untouched; all 893 v0.3.4 tests pass unchanged | existing attributes |

**Two choices beyond the request, stated for the consumer:**

- **The window is anchored on the newest record, not the clock.** The list
  therefore always ends with the record in `values`, and a late feed does not
  shorten the window. If MeteoSwiss stops publishing, the list ages with the
  state; the consumer sees the age in the `time` fields.
- **No buffer between polls.** A record present in one poll's file and absent
  from the next (MeteoSwiss withdrawing a value) disappears from the list. The
  source decides, as the request intends.

## 4. Tests and mutations

20 new tests (`tests/test_v0_3_5_recent_values.py`), built on the real Tänikon
column layout and record values from the v0.3.4 fixtures, extended to full
"now" files from yesterday 12 UTC.

| # | Mutation | Caught |
|---|---|---|
| V1 | window filter removed | ✓ |
| V2 | window off by one (37 records) | ✓ |
| V3 | records not sorted | ✓ |
| V4 | last duplicate wins instead of first | ✓ |
| V5 | grid filter removed | ✓ |
| V6 | window anchored on the clock | ✓ |
| V7 | gaps filled from the previous record | ✓ |
| V8 | `recent_values` recorded | ✓ |
| V9 | buffer kept across polls | ✓ |
| V10 | sensor reads the wrong key | ✓ |
| V11 | time not ISO 8601 | ✓ |
| V12 | window 2 h | ✓ |
| V13 | only the latest record published | ✓ |

## 5. Trial protection and risk

- **Class A untouched.** The change lives in the reference-station client,
  coordinator and sensor; the v0.3.4 trial-guard test (no learning, blend or
  reconciliation reference to the station) passes unchanged.
- **Load.** No extra HTTP request. Parsing ~110 extra rows per poll is
  negligible.
- **Size.** 36 records × 32 parameters ≈ 23 kB of JSON. Not recorded, so no
  database growth and no recorder size warning (the recorder filters
  unrecorded attributes before its 16 kB check). The attribute is sent to open
  dashboards with each 10-minute update; acceptable.
- **Residual:** if MeteoSwiss ever shortened the "now" file below 6 hours, the
  list would hold what the file holds — fewer records, never invented ones.

**Not verifiable here:** live MeteoSwiss files (no network from the build
environment; format verified by the owner's collector since 10 Oct 2026, span
from MeteoSwiss's documentation).

## 6. Post-install check

Within 10 minutes of the restart, in **Developer tools → States**,
`sensor.swissweather_fusion_reference_station` has `recent_values` with 35–36
entries; the last `time` equals the sensor state; listing the `time` fields
shows 10-minute steps without 20-minute jumps (request §3).

## 7. Upgrade

No migration, no learning reset, no configuration change. Replace the
`custom_components/swissweather_fusion` folder and restart Home Assistant.

## 8. Repackage of 15 Oct 2026 — privacy scrub (same version)

The package was checked for confidential content before publication. No
credentials were found (no API keys, consumer secrets or tokens; test files use
placeholders such as `TESTKEY`). Removed:

- **Precise coordinates of the owner's home and SRF's grid point next to it**
  (4 decimals, ~10 m) in tests, two documents and `srf_probe.py`. Replaced with
  fictional points; the redaction tests keep the same structure (configured
  point plus a nearby grid point about 200 m away).
- **Details of the owner's installation beyond the weather station** in the
  v0.3.3 audit.
- **The Wetter-Alarm location id nearest the owner's home** (a public POI about
  0.5 km away, so it located the home to its neighbourhood) in the v0.3.3 audit
  and 22 places in the warnings tests; replaced with a fictional id.
- **Where the owner was at one point** (a timezone remark in DEVELOPER.md) and
  **the name of another integration the owner runs** (DEVELOPER.md v0.1.14).

**Second pass, same day (owner request):** this integration has no reason to
know what the owner's other systems are or who consumes its data. Removed: the
two consumer notes for v0.3.4 and v0.3.5 (which held owner-specific
data), the production-model design notes in the v0.3.3
audit, and every attribution naming the consumer
attribution in code comments, tests and documents. The interface descriptions
they carried (cloud layers, SRF timing, reference station, `recent_values`)
remain in the README in neutral form, including the SRF migration note.

Method: an automated scan of all 116 files (credentials, coordinates, ids,
addresses, names, image metadata), followed by a full read of all documents.
The last three items were found only by the full read.

Kept deliberately: example elevations in physics comments and tests (they do
not locate a house), the station brand named in the pressure-datum rationale, the town-level location (Frauenfeld / Tänikon, also implied by
the station choice) and two-decimal test coordinates (~1 km). Hits inside
`wetteralarm_pois.json` are public Wetter-Alarm locations, not the owner's.
Code unchanged; 913 tests pass.
