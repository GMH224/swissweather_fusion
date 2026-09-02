"""Constants for the SwissWeather Fusion integration.

See DEVELOPER.md in the repository root for the full architecture rationale
(why two models, why EMA instead of gradient boosting, why each source was
chosen or rejected). This module only holds the concrete values; the "why"
lives in the docs so it isn't lost in code comments nobody reads.
"""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "swissweather_fusion"

# ---------------------------------------------------------------------------
# Config entry keys
# ---------------------------------------------------------------------------
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_ELEVATION_LOOKED_UP = "elevation_looked_up"  # from Open-Meteo Elevation API
CONF_ELEVATION_OVERRIDE = "elevation_override"  # user-entered, e.g. from DGPS
CONF_ELEVATION_EFFECTIVE = "elevation_effective"  # override if set, else looked-up

CONF_STATION_TEMP_ENTITY = "station_temp_entity"
CONF_STATION_HUMIDITY_ENTITY = "station_humidity_entity"
CONF_STATION_PRESSURE_ENTITY = "station_pressure_entity"
# Rain / wind entities intentionally not present yet — added when the local
# station gains those sensors (see plan doc §2).

CONF_SRF_CONSUMER_KEY = "srf_consumer_key"
CONF_SRF_CONSUMER_SECRET = "srf_consumer_secret"
CONF_METEOBLUE_API_KEY = "meteoblue_api_key"
CONF_METEONOMIQS_API_KEY = "meteonomiqs_api_key"
# Optional — Open-Meteo's free tier needs no key at all. This is only for
# their paid/commercial tier, which raises rate limits and uses dedicated
# infrastructure — it does NOT make CH1/CH2/D2 refresh more often, since
# that's fixed by MeteoSwiss/DWD's own model run schedule regardless of
# tier. See clients/open_meteo.py for the customer- hostname requirement
# that comes with using a key.
CONF_OPEN_METEO_API_KEY = "open_meteo_api_key"

CONF_PURGE_DAYS = "purge_days"  # 0 = forever (explicit opt-in only)

# v0.1.24 (P1-22 / IND-12): whether the configured station pressure entity
# already reports MEAN SEA LEVEL pressure, or the raw pressure measured at
# the station's own altitude.
#
# This is not a nicety — it is unresolvable automatically. Netatmo, the
# reference station for this project, publishes BOTH values: "Pressure"
# (which Netatmo normalizes to mean sea level using the GPS altitude
# captured during setup) and "AbsolutePressure" (the raw measurement).
# Home Assistant's Netatmo integration exposes both, and both carry
# device_class: atmospheric_pressure — so the entity selector in
# config_flow.py physically cannot distinguish them. The user has to say.
#
# Default is False (station-level / absolute), because that is the
# physically honest reading and the one this project's reference
# installation actually uses. When False, StationCoordinator reduces the
# reading to sea level via unit_conversion.reduce_station_pressure_to_sea_level
# before storing, so that station pressure is comparable with every
# provider's forecast pressure (all of which are MSL — see
# clients/open_meteo.py's pressure_msl and clients/meteoblue.py's
# sealevelpressure).
CONF_STATION_PRESSURE_IS_SEA_LEVEL = "station_pressure_is_sea_level"
DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL = False

# v0.1.24 fix (P1-29): the options flow's "blank means keep existing"
# backfill is correct and necessary for the four REQUIRED credentials,
# because a masked password field cannot be pre-filled. Applied uniformly
# it also made the one genuinely optional credential impossible to
# remove, so a user could never return to Open-Meteo's free tier. Same
# tri-state checkbox pattern already established for
# CONF_CLEAR_ELEVATION_OVERRIDE.
CONF_CLEAR_OPEN_METEO_API_KEY = "clear_open_meteo_api_key"
# v0.1.23 fix (L-10): how often RetentionCoordinator actually runs
# purge_older_than() — see coordinator.py. purge_days controls the
# retention *window*; this controls the *check frequency*, and the two are
# deliberately independent — retention is a slow-moving housekeeping
# concern, so once/day is plenty regardless of what window the user
# configures, and there's no reason it should share a schedule with any
# of the polling coordinators above.
RETENTION_CHECK_INTERVAL = timedelta(hours=24)
# v0.1.24 fix (IND-06): this was 0 — "keep forever" — and the setup flow
# never asked, so every installation silently defaulted to unbounded
# SQLite growth on the SD-card / HA-Green class hardware this integration
# targets. Each changed Open-Meteo run inserts on the order of 5
# variables x up to 168 forecast hours per model, across three models,
# plus SRF and meteoblue.
#
# 90 days is chosen deliberately: comfortably longer than both
# INITIAL_LOOKBACK_DAYS (14, the learning warm-up window) and the
# 168-hour forecast horizon, so nothing the models actually consume is
# ever purged out from under them, while still bounding the file. 0 is
# still accepted and still means forever — it is now an explicit opt-in
# rather than the silent default.
DEFAULT_PURGE_DAYS = 90

# Toggleable diagnostic event recorder (v0.1.9) — off by default, since it
# only accumulates data when someone has explicitly asked to watch
# closely, not as a standing background cost. See diagnostics_recorder.py
# and diagnostics.py.
CONF_DIAGNOSTIC_LOGGING_ENABLED = "diagnostic_logging_enabled"
DEFAULT_DIAGNOSTIC_LOGGING_ENABLED = False
DIAGNOSTIC_EVENT_BUFFER_SIZE = 1000

# ---------------------------------------------------------------------------
# Sources (Model A blend experts)
# ---------------------------------------------------------------------------
SOURCE_CH1 = "ch1"
SOURCE_CH2 = "ch2"
SOURCE_ICON_D2 = "icon_d2"
SOURCE_SRF = "srf"
SOURCE_METEOBLUE = "meteoblue"

ALL_FORECAST_SOURCES = (
    SOURCE_CH1,
    SOURCE_CH2,
    SOURCE_ICON_D2,
    SOURCE_SRF,
    SOURCE_METEOBLUE,
)

# CombiPrecip is deliberately NOT in ALL_FORECAST_SOURCES: it's ground-truth
# radar observation, not a forecast to bias-correct, and never enters Model
# A's bucket_stats. It's a Model B feature only. See plan doc §10.
SOURCE_COMBIPRECIP = "combiprecip"

# Meteonomiqs is, like CombiPrecip, a Model B input rather than a Model A
# blend expert — it's used for its nowcast/CAPE signal, not routinely
# bias-corrected like the forecast sources in ALL_FORECAST_SOURCES.
SOURCE_METEONOMIQS = "meteonomiqs"

# ---------------------------------------------------------------------------
# Poll intervals
# ---------------------------------------------------------------------------
# CH1 / CH2 / ICON-D2 don't use a fixed interval — the coordinator checks
# each model's own last_run_availability_time via Open-Meteo's metadata API
# before fetching, rather than guessing a buffer. See plan doc §2.
OPEN_METEO_CHECK_INTERVAL = timedelta(minutes=15)

SRF_POLL_INTERVAL = timedelta(minutes=45)

# meteoblue: seasonal schedule, both 3 calls/day (credit-neutral). Hours are
# in local time; the coordinator converts to UTC internally.
METEOBLUE_SUMMER_MONTHS = (3, 4, 5, 6, 7, 8, 9, 10)  # Mar-Oct
METEOBLUE_SUMMER_HOURS_LOCAL = (12, 16, 20)
METEOBLUE_WINTER_HOURS_LOCAL = (6, 12, 18)
METEOBLUE_MAX_BONUS_CALLS_PER_EVENT = 1

# v0.1.24 fix (P1-06): meteoblue had per-day and per-event caps but NOTHING
# bounding the annual total, so the local control plane could authorize
# more than the provider budget allows:
#
#   3 scheduled/day x 8,000 credits x 365 = 8.76M  (inside the 10M cap)
#   4 calls/day     x 8,000 credits x 365 = 11.68M (exceeds it)
#
# Both figures come from clients/meteoblue.py's own module docstring —
# 8,000 credits per call flat, against a 10M/year account cap, confirmed
# against a live test call during original planning. Not a guessed
# placeholder.
#
# Note the allocation this implies: 3 scheduled calls/day is 1,095
# calls/year, leaving 155 bonus calls/year. That is a deliberate
# trade-off (scheduled coverage is worth more than bonus responsiveness),
# not an accident of the arithmetic.
METEOBLUE_ANNUAL_CALL_BUDGET = 10_000_000 // 8_000  # = 1250 calls/year

# v0.1.24 fix (P1-09): _last_scheduled_call_hour was only ever updated on
# SUCCESS, so a failing scheduled call re-entered the call path on every
# 5-minute poll for the rest of that hour — up to ~12 attempts, each
# spending a real API credit against the annual ceiling above. 3x the
# poll interval bounds retry frequency within a still-unserviced slot
# without giving up on retries entirely.
METEOBLUE_SCHEDULED_RETRY_COOLDOWN = timedelta(minutes=15)
# v0.1.17 fix: Meteonomiqs's bonus-call path had no per-day cap at all —
# only the overall 1000-calls/year budget check — confirmed in production
# to allow it firing every 5 minutes if the cross-model trigger kept
# re-evaluating true, burning the annual budget in days. Same daily
# philosophy as meteoblue's cap above: one bonus call per storm scenario,
# per day, regardless of how many times the trigger condition re-fires.
METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT = 1

# v0.1.24 (P1-19 — audit finding REFUTED, deliberately unchanged): the
# external audit claimed MeteoSwiss publishes CombiPrecip on a ~10-minute
# cycle and that polling at 5 minutes wasted requests. MeteoSwiss's own
# open-data documentation lists the update frequency for the CombiPrecip
# 60-minute-total product (CPC) as 5 MINUTES, the same as PRECIP (RZC)
# and PRECIP-SV (TZC). The "60 minute" in the product name is the
# ACCUMULATION WINDOW, not the publication interval — the two appear to
# have been conflated in the audit.
#
# This value is therefore correct as it stands and must not be "fixed" to
# 10 minutes; doing so would halve the radar update rate for no benefit
# and would also corrupt RADAR_FRESHNESS_LIMIT below, which is derived
# from it. tests/test_combiprecip.py pins this with a citation so the
# same change cannot be reintroduced silently.
COMBIPRECIP_POLL_INTERVAL = timedelta(minutes=5)

# v0.1.24 fix (P1-13): Home Assistant's DataUpdateCoordinator keeps
# serving its last successful .data indefinitely across repeated failed
# refreshes, so a stalled CombiPrecip feed could influence the storm
# score forever. 2x the (correct, 5-minute) product cadence, which
# tolerates exactly one missed publication before a reading is considered
# stale.
RADAR_FRESHNESS_LIMIT = timedelta(minutes=10)

# v0.1.24 fix (P1-16): MeteoSwiss encodes a radar quality code directly in
# the CombiPrecip FILENAME — CPCyyjjjHHMMQ_00060.*.h5, where Q is a single
# digit 0-9 and 9 is best. That is strictly more reliable than the
# optional ODIM_H5 quality1/data1 sub-group, which is genuinely optional
# even within the format spec and may never be populated in practice.
# Points whose quality code is CONFIRMED below this threshold are
# excluded from scoring; points whose quality is UNKNOWN (None) are NOT
# excluded, since treating unknown as bad would risk silently disabling
# the radar signal entirely.
RADAR_QUALITY_MINIMUM_CODE = 5

# Upwind sampling points for CombiPrecip (same downloaded grid, extract 4
# pixels instead of 1 — negligible extra cost since the whole grid is
# already in memory after one HDF5 parse).
#
# v0.1.24 fix (P1-15): these distances were previously commented as
# corresponding to "~20/35/60 minute lead times at typical storm-cell
# propagation speed". That is a specific, checkable timing claim which
# was never validated against this project's own data, and it is made
# worse by P1-14: what CombiPrecip reports is a ONE-HOUR ACCUMULATION,
# so a spatial sample of it cannot carry a 20-minute arrival time in the
# first place. The minute figures have been dropped entirely. The points
# remain distance-graded evidence — closer means more imminent, which is
# defensible — but no timing claim is attached to them.
#
# Bearing is FROM the configured location TOWARD where weather is coming
# from (225° = southwest) — a fixed default, not dynamically recomputed
# from actual wind direction. See DEVELOPER.md for why v0 uses a fixed
# bearing and what upgrading to dynamic wind-direction sampling would
# require.
UPWIND_BEARING_DEGREES = 225.0  # SW
UPWIND_DISTANCES_KM = (30.0, 45.0, 70.0)
UPWIND_POINT_LABELS = ("near", "mid", "far")  # nearest -> farthest upwind

# Meteonomiqs (wetter.com PWA v4.0) — 1000 calls/year total. Two distinct
# obligations, not one: (1) MeteoSwiss/Meteonomiqs's own confirmation that
# the API key is revoked after ~30 days of inactivity, so at least one call
# per day is made unconditionally as a keep-alive, using the nowcast
# endpoint (dual-purpose: keeps the key alive AND is itself a genuinely
# useful Model B data point, not wasted traffic); (2) event-triggered bonus
# calls when Model B's own signals suggest something developing, same
# pattern as meteoblue's allowance. Both draw from the same annual pool.
# 365 keep-alive calls/year leaves ~635/year (~1.7/day average) of
# headroom for bonus calls — comfortable even in an active storm season.
# See DEVELOPER.md ("Why Meteonomiqs needs a daily heartbeat").
METEONOMIQS_ANNUAL_CALL_BUDGET = 1000
METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS = 30  # their stated revocation window
METEONOMIQS_KEEPALIVE_INTERVAL = timedelta(days=1)  # polled far more often than
                                                      # the 30-day limit requires,
                                                      # for safety margin
# v0.1.23 cleanup: METEONOMIQS_MAX_CALLS_PER_EVENT (a duplicate of
# METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT above, which is the constant
# actually used) was removed here — dead code with no references anywhere
# in production or tests.

# v0.1.23 fix (own-review finding — "Meteonomiqs hourly forecast fetched
# but never used"): the seasonal /forecast/hourly call's pressure/precip
# data was parsed into MeteonomiqsCoordinator.last_hourly_forecast and then
# never read again by anything — not persisted, not exposed on a sensor,
# not fed into Model B, despite const.py's own comment further below
# describing this call as "a straight upgrade" of the mandatory keep-alive
# specifically because it's "useful for Model B" during these months. That
# usefulness was never actually wired up. Rather than invent new Model B
# scoring behavior that wasn't specified anywhere, the fix persists this
# data into forecast_snapshots (so it's durable and available for future
# use / manual correlation) under variable names distinctly prefixed with
# the source name — the same pattern SRF's own daily-only fields already
# use (see clients/srf.py's _DAY_FIELD_MAP comment) specifically so this
# can NEVER be picked up by Model A's blend even by accident: Meteonomiqs
# is deliberately excluded from ALL_FORECAST_SOURCES above and stays that
# way. See MeteonomiqsCoordinator._async_fetch_hourly_forecast.
METEONOMIQS_HOURLY_VARIABLE_PREFIX = "meteonomiqs_"

# During the same Mar-Oct storm-season window already established for
# meteoblue's schedule (kept identical deliberately, rather than
# introducing a third date range to track), the mandatory daily keep-alive
# call uses /forecast/hourly (pressure, precipitation — plain, non-premium
# tier; CAPE was hoped for via /forecast2 but that endpoint turned out to
# be paid and unavailable, see DEVELOPER.md) instead of /nowcast, at local
# noon. This is NOT an extra call — either one satisfies the same
# keep-alive requirement, so the daily budget cost is identical; it's a
# straight upgrade of what that one mandatory call returns during the
# months it's actually useful for Model B. Outside this window, the
# keep-alive reverts to the plain nowcast call. See DEVELOPER.md ("Why
# Meteonomiqs needs a daily heartbeat").
METEONOMIQS_FORECAST_SEASON_MONTHS = METEOBLUE_SUMMER_MONTHS
METEONOMIQS_FORECAST_CALL_HOUR_LOCAL = 12

STATION_POLL_INTERVAL = timedelta(minutes=5)

MODEL_B_SCORING_INTERVAL = timedelta(minutes=5)

# ---------------------------------------------------------------------------
# Model A: bucket dimensions
# ---------------------------------------------------------------------------
SEASON_DJF = "DJF"
SEASON_MAM = "MAM"
SEASON_JJA = "JJA"
SEASON_SON = "SON"

LEAD_TIME_SHORT = "short"      # < 24h
LEAD_TIME_MEDIUM = "medium"    # 24-72h
LEAD_TIME_LONG = "long"        # > 72h

LEAD_TIME_SHORT_MAX_HOURS = 24
LEAD_TIME_MEDIUM_MAX_HOURS = 72

# EMA responsiveness (alpha), by lead_time_bucket. Short buckets adapt fast
# (regime changes matter), long buckets smooth heavily (sparser, noisier
# data per bucket). See plan doc §3.
EMA_ALPHA_BY_LEAD_TIME = {
    LEAD_TIME_SHORT: 0.15,
    LEAD_TIME_MEDIUM: 0.08,
    LEAD_TIME_LONG: 0.04,
}

# Below this many samples, a bucket's correction is not trusted — serve the
# raw forecast instead. Starting point only; revisit once real fill rates
# are observed (plan doc §3).
MIN_SAMPLES_TO_TRUST_BUCKET = 5

EMA_WEIGHT_EPSILON = 0.01  # avoids divide-by-zero when ema_abs_error is 0

# Approximate environmental lapse rate, used only for the optional
# lapse-rate pre-correction when a precise elevation is configured.
LAPSE_RATE_C_PER_1000M = 6.5

# ---------------------------------------------------------------------------
# Model B: storm-onset classifier
# ---------------------------------------------------------------------------
TENDENCY_WINDOWS_MINUTES = (10, 30, 60)

# v0 threshold rule defaults — a starting point, not tuned against real data
# yet. See DEVELOPER.md for the reasoning and plan doc §4.
V0_PRESSURE_DROP_HPA_THRESHOLD = 1.0  # over the 30-min window
V0_HUMIDITY_RISE_PCT_THRESHOLD = 8.0  # over the 30-min window
V0_TRIGGER_PROBABILITY = 0.65  # synthetic "probability" v0 reports when the rule fires

# Graduated probability by which upwind CombiPrecip point shows significant
# precipitation, used for the "storm in ~30 minutes" indicator (blinds
# automation, etc). "far" (~60min out) contributes a modest probability,
# "near" (~20min out) or precip already at the local point contributes a
# high one — this is a hand-crafted v0 heuristic, not yet a trained model.
# See DEVELOPER.md for the v0 -> v1 (XGBoost/LightGBM) upgrade path once
# enough storm_predictions/storm_events data exists to train on.
# v0.1.24 fix (P1-14): this constant was named ..._MMH_THRESHOLD and
# applied to a field named precip_rate_mmh, as though CombiPrecip
# reported an instantaneous rain rate. MeteoSwiss's own open-data
# documentation is explicit that it does not:
#
#   CPC  "Combiprecip 60-minute total"  unit mm    accumulation over 1 hour
#   RZC  "PRECIP"                       unit mm/h  instantaneous rain rate
#
# This project fetches CPC (see STAC_COLLECTION and the CPC/_00060 asset
# contract in clients/combiprecip.py), so the value is millimetres
# accumulated over the preceding hour. Renamed and re-derived rather than
# merely re-documented, because the threshold's NUMERIC meaning was
# wrong, not just its label.
#
# 0.5 mm accumulated in the preceding hour is retained as the detection
# floor: it is roughly the smallest accumulation that reliably indicates
# real precipitation rather than radar noise, and it stays comparable
# with the previous behaviour for the steady-rain case where an hourly
# accumulation and an hourly-average rate coincide numerically. It is
# still a v0 heuristic, not a calibrated figure — see the caveat in
# DEVELOPER.md.
RADAR_PRECIP_ACCUM_MM_THRESHOLD = 0.5  # mm accumulated over the preceding hour
UPWIND_POINT_PROBABILITY = {
    "far": 0.30,
    "mid": 0.55,
    "near": 0.75,
}
LOCAL_POINT_PROBABILITY = 0.90  # precip already detected at the configured location

STORM_PREDICTION_UPPER_CROSSING_THRESHOLD = 0.5

# v0.1.24 fix (P1-12): every interval Meteonomiqs' nowcast returned was
# folded into max(risk_values) regardless of how far in the future it
# was, so a high-risk interval hours out could raise a score presented as
# "storm within the near term". The filter is an OVERLAP test against
# [now, now + this), not a "starts after now" test — the latter would
# wrongly exclude the currently-active interval, which by definition
# began slightly before now.
METEONOMIQS_NOWCAST_TARGET_WINDOW = timedelta(minutes=30)

# ---------------------------------------------------------------------------
# Storm-event reconciliation (v0.1.24, P2-08)
# ---------------------------------------------------------------------------
# Nothing in v0.1.23 ever called SwissWeatherDB.insert_storm_event(), so
# storm_events — the ground-truth table the entire Model B v1 plan depends
# on — could never fill from runtime operation.
#
# StormEventReconciliationCoordinator closes that loop: for any
# storm_predictions row whose follow-up window has fully elapsed, it
# fetches the real station and radar observations across that window and
# checks them against Model B's OWN existing v0 thresholds
# (V0_PRESSURE_DROP_HPA_THRESHOLD, RADAR_PRECIP_ACCUM_MM_THRESHOLD),
# reusing the live scorer's definition of a storm signature rather than
# inventing a second, divergent one.
STORM_RECONCILIATION_INTERVAL = timedelta(minutes=30)
# How long after a prediction its outcome is considered settled. Matches
# the "storm in ~30 minutes" claim the score is used for, with generous
# margin for a slow-moving cell.
STORM_FOLLOW_UP_WINDOW = timedelta(minutes=90)
# Only predictions that were actually worth checking are reconciled — a
# score that never crossed the reporting threshold has no outcome to
# confirm.
STORM_RECONCILIATION_MIN_PROBABILITY = STORM_PREDICTION_UPPER_CROSSING_THRESHOLD

# ---------------------------------------------------------------------------
# Weather condition mapping (v0.1.24, P2-10)
# ---------------------------------------------------------------------------
# Four separate call sites collapsed every non-rain condition to "sunny".
# This threshold adds a "cloudy" branch. It is an explicitly
# plausible-but-unvalidated proxy: high relative humidity with no
# significant precipitation often but not always means overcast. It is
# NOT calibrated cloud-cover data — none of the five providers this
# project consumes is queried for cloud cover today. Disclosed as a v0
# heuristic in DEVELOPER.md rather than presented as a measurement.
CONDITION_CLOUDY_HUMIDITY_THRESHOLD = 90.0

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_FILENAME = "swissweather_fusion.db"


# ---------------------------------------------------------------------------
# Station pressure plausibility (v0.2.1, SWF-P1-009)
# ---------------------------------------------------------------------------
# Bounds on the value AFTER any sea-level reduction. Deliberately wide —
# the world record range for mean sea level pressure is roughly 870 hPa
# (typhoon Tip) to 1084 hPa (Siberia), and Swiss extremes sit comfortably
# inside 960-1050. Anything outside this is not weather; it is a
# configuration error, most likely the sea-level setting being inverted.
#
# Narrower than provider_validation's 800-1100 storage bounds on purpose:
# that range must accommodate raw STATION pressure at altitude (a sensor
# at 2000 m legitimately reads ~795 hPa), whereas by this point the value
# is supposed to already be sea-level normalised.
PRESSURE_PLAUSIBLE_MIN_HPA = 870.0
PRESSURE_PLAUSIBLE_MAX_HPA = 1085.0
