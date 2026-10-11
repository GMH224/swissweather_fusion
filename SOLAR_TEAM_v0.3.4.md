# SwissWeather Fusion v0.3.4 — notes for the solar team

Three changes concern you: two new inputs (cloud cover by layer, live station
measurements) and a corrected one (SRF irradiance). All times are UTC.

## 1. New: cloud cover by layer, in the hourly forecast

Each entry of `weather.get_forecasts` (type `hourly`) now carries, next to
`cloud_coverage`:

| Field | Layer |
|---|---|
| `cloud_coverage_low` | surface to ~2 km — fog and stratus |
| `cloud_coverage_mid` | ~2–6 km |
| `cloud_coverage_high` | above ~6 km (cirrus) |

- Percent, valid **at** the entry's `datetime`; mean of ICON-CH1, CH2 and D2.
- The layers do **not** add up to `cloud_coverage` (layers overlap as seen from
  the ground).
- **Missing field = no forecast** (beyond ICON's ~5-day horizon), never 0.
- Model statements, not measurements. Low cloud and fog are where all weather
  models are weakest (11 Oct 2026: every model expected the deck to break up; it
  did not). Use a high `cloud_coverage_low` as a warning sign — e.g. to lower
  confidence or temper the radiation input — not as a correction factor until it
  has been backtested.

## 2. Corrected: SRF global irradiance is now one hour earlier

`sensor.swissweather_fusion_srf_global_irradiance`

SRF labels each value at the **end** of the interval it describes: SRF's 14:00
value is the average for 13:00–14:00. v0.3.3 passed that label through, so the
sensor showed every hour's value one hour late. Measured against the MeteoSwiss
Tänikon pyranometer (6–11 Oct): hourly error 98 W/m² as published before, 66 W/m²
read correctly. The fused ICON radiation (51 W/m²) remains the better input; SRF
is the independent second opinion.

**What changes for you**

- The sensor **state** is now the average for the current hour.
- The `hourly_forecast` attribute uses the same keys as the solar radiation
  series:

  ```
  {"period_start": "2026-10-12T09:00:00+00:00",
   "period_end":   "2026-10-12T10:00:00+00:00",
   "value": 412.0}
  ```

  `value` is the average over `[period_start, period_end)`. **The `time` key no
  longer exists** — update any reader that used it.
- 1-hour periods in SRF's hourly window; 3-hour periods beyond it.
- If you shifted SRF by one hour yourself as a workaround, **remove that shift**.
  The series is already aligned; shifting again would make it an hour early.

## 3. New: live measurements from MeteoSwiss Tänikon

The nearest MeteoSwiss station that measures solar radiation (Tänikon, TAE,
~8 km) is read every 10 minutes. MeteoSwiss publishes 10–20 minutes after the
measurement.

| Sensor (`sensor.swissweather_fusion_reference_station_…`) | Use for solar |
|---|---|
| `global_radiation` (W/m², 10-min mean) | what actually arrives now — compare with the forecast for the current hour |
| `sunshine_10_min` (min) | sun vs cloud right now |
| `longwave_radiation_from_the_sky` (W/m²) | cloud deck indicator: high (≈ 330–350 in October) with little sun = closed cloud |
| `temperature` (°C), `wind_speed` (m/s) | inputs for a panel-temperature model |
| `humidity`, `pressure_sea_level`, `wind_gust`, `wind_direction`, `precipitation_10_min` | context |

`sensor.swissweather_fusion_reference_station` holds **everything** the station
reports in its `values` attribute (MeteoSwiss short names), with descriptions and
units in `parameters`. At Tänikon this includes **measured diffuse radiation**
(`ods000z0`) and reflected shortwave (`osr000z0`, rises sharply with snow on the
ground).

- 10-minute values cover the 10 minutes **ending** at the sensor's time.
- Sensors show *unknown* when the newest record is older than 60 minutes.
- Over 6–11 October your PV output followed Tänikon's measured radiation more
  closely (hourly correlation 0.95) than any forecast (0.79–0.89). The station is
  a good live reference for the roof.

## 4. Unchanged

- The solar radiation sensors (GHI, DNI, DHI, hour average and instant) and their
  `hourly_forecast` series: same keys, same semantics as v0.3.3.
- Precipitation, temperature and the weather card.
