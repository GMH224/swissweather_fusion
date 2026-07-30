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

CONF_PURGE_DAYS = "purge_days"  # 0 = forever
DEFAULT_PURGE_DAYS = 0

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

COMBIPRECIP_POLL_INTERVAL = timedelta(minutes=5)

# Upwind sampling points for CombiPrecip (same downloaded grid, extract 4
# pixels instead of 1 — negligible extra cost since the whole grid is
# already in memory after one HDF5 parse). Distances chosen to correspond
# to roughly 20/35/60 minute lead times at typical storm-cell propagation
# speed. Bearing is FROM the configured location TOWARD where weather is
# coming from (225° = southwest) — a fixed default, not dynamically
# recomputed from actual wind direction. See DEVELOPER.md for why v0 uses a
# fixed bearing and what upgrading to dynamic wind-direction sampling would
# require.
UPWIND_BEARING_DEGREES = 225.0  # SW
UPWIND_DISTANCES_KM = (30.0, 45.0, 70.0)
UPWIND_POINT_LABELS = ("near", "mid", "far")  # ~20 / ~35 / ~60 min lead time

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
METEONOMIQS_MAX_CALLS_PER_EVENT = 1

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
RADAR_PRECIP_DETECTION_MMH_THRESHOLD = 0.5  # above this counts as "detected"
UPWIND_POINT_PROBABILITY = {
    "far": 0.30,
    "mid": 0.55,
    "near": 0.75,
}
LOCAL_POINT_PROBABILITY = 0.90  # precip already detected at the configured location

STORM_PREDICTION_UPPER_CROSSING_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_FILENAME = "swissweather_fusion.db"
