<p align="center">
  <img src="icons/icon.png" width="128" height="128" alt="SwissWeather Fusion icon">
</p>

# SwissWeather Fusion

A Home Assistant integration that fuses multiple weather forecast sources —
MeteoSwiss's ICON-CH1-EPS and ICON-CH2-EPS, DWD's ICON-D2, SRF (SRG SSR),
and meteoblue — into a single locally-corrected forecast, continuously
learning the bias between each source and your own weather station. It
also runs a short-horizon storm-onset indicator for summer convective
weather (temperature/rain arriving together with a pressure signature),
using MeteoSwiss's CombiPrecip radar feed and an optional independent
check from Meteonomiqs.

**Status: v0.1.1.** The core architecture is built and the business logic
(bias correction, storm scoring, radar sampling) is unit-tested. The first
real deployment against a live Home Assistant instance found four bugs
(wrong Open-Meteo model names, an SRF response-shape crash, a setup
sequencing issue that let one failing source block the whole integration,
and an overly-narrow pressure sensor filter) — all fixed, see
[DEVELOPER.md](DEVELOPER.md) for the full account. Continued real-world
testing is still the priority; this is one deployment cycle in, not a
mature, battle-tested release.

## What this does

- **Model A** — blends CH1, CH2, ICON-D2, SRF, and meteoblue's forecasts,
  each corrected for its own systematic bias against your specific
  location, learned continuously and automatically (no manual tuning).
- **Model B** — watches your station's pressure/humidity/temperature trend
  plus a live radar precipitation reading (at your location and three
  points upwind, roughly 20/35/60 minutes out) for the classic summer
  storm signature, and exposes a live "storm probability" percentage —
  useful for automations like closing blinds ahead of a downpour.

This is **not** a replacement for professional weather models — it can't
run atmospheric physics. It corrects and blends forecasts that already
exist, the same way national weather services localize their own
forecasts (a technique called Model Output Statistics).

## Requirements

- A Home Assistant instance with local temperature, humidity, and pressure
  sensors already set up (rain/wind support is planned for later).
- Four free API credentials (no cost for the tiers this integration uses):
  - **SRF (SRG SSR)**: register at [developer.srgssr.ch](https://developer.srgssr.ch),
    create an App on the "SRG SSR PUBLIC API V2" product, get a consumer
    key/secret.
  - **meteoblue**: register for the free Weather API at
    [meteoblue.com](https://www.meteoblue.com).
  - **Meteonomiqs (wetter.com)**: contact `info@meteonomiqs.com` for an API
    key.
  - MeteoSwiss's own CH1/CH2 data and DWD's ICON-D2 are fetched via
    [Open-Meteo](https://open-meteo.com), which needs no account or key.

## Installation

1. Add this repository as a custom repository in HACS, or copy
   `custom_components/swissweather_fusion` into your `config/custom_components/` folder.
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for
   "SwissWeather Fusion".
4. Follow the setup steps: location & elevation, local station sensors,
   then the four API credentials.

### About the icon

`icons/icon.png` (256×256) and `icons/icon@2x.png` (512×512) are included,
sized to match the [home-assistant/brands](https://github.com/home-assistant/brands)
convention. Worth being upfront about a real limitation here: for a
private/custom HACS repository (added manually, not the default HACS
store), there isn't a way to make this icon show natively next to the
integration in HA's own Settings → Devices & Services list or HACS's
store UI — that specific placement is sourced from the `brands` repository,
which requires submitting a PR there and is really only appropriate once
this is a public, stable, more broadly-used integration. Until then, the
icon displays wherever this repo's README is rendered (HACS's own repo
info page, GitHub itself), which is what's set up above.

## Configuration notes

- **Location**: use precise coordinates, not a postal code — this
  integration exists to correct for microclimate variation, and a postal
  code covers an area wider than that variation.
- **Elevation**: auto-looked-up from your coordinates by default; enter a
  manual value if you have a more precise one (e.g. from a differential
  GPS survey) — this feeds an optional physics-based correction that gives
  Model A a head start before it's learned anything from data yet.
- Everything is editable later via the integration's **Configure** button,
  not just at initial setup.

## Sensors

Beyond the main `weather.*` entity, this integration exposes:

- `sensor.*_status`, `*_forecast_accuracy`, `*_active_sources`
- `sensor.*_last_learning_a` / `*_last_learning_b` — when each model last
  updated its correction
- `sensor.*_expert_weight_<source>` — one per blend source, for seeing how
  much each is currently trusted
- `sensor.*_storm_onset_probability` — the live storm indicator
- Per-source health: `*_last_success`, `*_last_poll_duration`,
  `*_last_data_error`, `*_consecutive_failures`, and `*_last_auth_error`
  for SRF specifically
- `binary_sensor.*_degraded` — one glance-able "is anything unhealthy" flag

## Known v0.1 limitations

- `forecast_accuracy` and `last_learning_a/b` are honest stubs (return
  `None`) pending the first real run's data — see DEVELOPER.md.
- The CombiPrecip radar client's HDF5 parsing is built against the
  documented ODIM_H5 standard, not a downloaded real file (no network
  access to data.geo.admin.ch in the environment that built this) — worth
  verifying against a real file early.
- Model B's v0 rule is a hand-crafted heuristic, not a trained model — see
  DEVELOPER.md for the upgrade path once a storm season of data exists.

## Diagnostics

Per-source health is real, not a placeholder: each of the 5 vendor
integrations (Open-Meteo, tracked per-model since CH1/CH2/D2 can fail
independently; SRF; meteoblue; CombiPrecip; Meteonomiqs) exposes its own
last-success time, last poll duration, consecutive-failure count, and —
critically — **data errors and auth errors as separate sensors**. An
expired or revoked credential (SRF is the one source with a true
credential that can expire) shows up specifically as
`sensor.*_srf_last_auth_error`, distinct from a generic timeout, since the
fix is different (re-enter credentials vs. just wait for the normal
retry). `binary_sensor.*_degraded` gives one glance-able "is anything
wrong" flag; the per-source sensors tell you which one and why.

## More detail

See [DEVELOPER.md](DEVELOPER.md) for the full architecture rationale —
why two models instead of one, why EMA instead of gradient boosting, why
each source was included or rejected, and what's actually been verified
versus what's a documented best guess.
