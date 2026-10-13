# SwissWeather Fusion v0.3.5 — reply to "keep every 10-minute station record"

Your format is confirmed as requested, with one choice made explicit (window
6 hours) and two clarifications. It is implemented and shipped in v0.3.5; you can
code 0.1.34.1 against it.

## Attribute

`sensor.swissweather_fusion_reference_station` → `recent_values`

```json
"recent_values": [
  {"time": "2026-10-13T01:50:00+00:00",
   "values": {"gre000z0": 0.0, "ods000z0": 0.0, "sre000z0": 0.0, "oli000z0": 331.0,
              "tre200s0": 8.4, "ppz850s0": null, "...": "..."}},
  ...
  {"time": "2026-10-13T07:40:00+00:00", "values": {"...": "..."}}
]
```

| # | Your rule | Answer |
|---|---|---|
| 1 | `time` = end of interval, UTC ISO with offset, 10-minute grid | Yes. Same meaning as the sensor state. |
| 2 | Same parameters and names as `values` | Yes. The **last** entry is always identical to `values` and its `time` to the state. |
| 3 | Every published record, including those polling skipped | Yes. Read from MeteoSwiss's file, which holds every record since yesterday 12 UTC; no extra download. |
| 4 | Nothing invented | Yes. Missing value `null`; record MeteoSwiss did not publish: absent. |
| 5 | Ascending, no duplicates | Yes. |
| 6 | 3–6 hours | **6 hours: up to 36 records**, ~23 kB. |
| 7 | Not recorded | Yes. |
| 8 | Complete after a restart | Yes, at the first update (within 10 minutes). |
| 9 | Everything else unchanged | Yes. |

## Clarifications

- **The window ends at the newest record, not at "now".** When MeteoSwiss is late,
  the list still holds 6 hours, ending at the record shown in the state. Use the
  `time` fields, not the clock, to decide which hours are complete.
- **No memory between updates.** Each update's list is built from MeteoSwiss's
  file alone. In the rare case MeteoSwiss withdraws a value, it disappears from
  the next list. Your merge rule ("replace only if the same or more complete")
  keeps your copy, which is fine.

## Acceptance check (your §3)

Expected to pass: tests simulating MeteoSwiss's varying 10–20 minute delay show
the latest-only view missing records while `recent_values` holds 6 of 6 for
every complete hour. Live check after install: list the `time` fields and look
for 20-minute jumps.
