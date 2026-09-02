"""SRF Weather API (V2) client, via the SRG SSR developer portal.

Two things make this client different from open_meteo.py, both confirmed
during planning (see DEVELOPER.md):

1. Consumer key/secret are NOT sent on API calls directly — they're
   exchanged for a short-lived bearer token first (OAuth2 client-credentials
   flow). The token is currently valid 7 days and needs proactive refresh,
   not a one-time fetch.
2. The forecast endpoint doesn't accept raw lat/lon — a `geolocationId`
   must be resolved once via a separate lookup and then reused. SRF's
   coverage isn't universal, so this snaps to their nearest known point,
   not necessarily the exact configured coordinates.

V1 of this API is deprecated; this client only ever targets V2 (which,
notably, is also the version that added humidity and pressure fields V1
lacked).
"""
from __future__ import annotations

import math

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)


class SrfPermanentError(RuntimeError):
    """v0.1.23 fix (L-11): raised for SRF responses that should NOT
    trigger the v2/forecastpoint -> daily-fallback attempt — a permanent
    4xx (bad request, account/API-plan restriction, revoked/invalid
    auth), not a transient network/5xx condition. Previously
    async_fetch_forecastpoint raised a plain RuntimeError for the
    parseable-SRF-detail case, which the coordinator's `except Exception`
    treated identically to a genuine transient failure — meaning a
    permanent rejection (e.g. the confirmed real "exceeded your location
    limit" free-plan restriction) triggered the exact same fallback
    request, and then repeated that same wasted primary+fallback pair on
    every subsequent poll forever, with the root cause hidden behind
    continuously-degraded-but-not-loudly-failing operation.

    Carries the HTTP status so health.classify_exception (which checks
    `getattr(err, "status", None)`) still correctly buckets 401/403 as an
    auth error distinct from other permanent 4xx data errors.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


# v0.1.6: none of this client's three HTTP calls (token exchange,
# geolocation lookup, forecast fetch) had an explicit timeout. Found after
# SRF's polling appeared to silently stop entirely for several hours in
# production — last_success frozen, but consecutive_failures also stuck
# at 0 (i.e. not failing loudly either), which is the signature of a
# request hanging forever rather than erroring. This is a reasoned
# hypothesis based on that symptom pattern, not a confirmed root cause
# the way the URL/shape fixes were — but an unbounded HTTP call is worth
# fixing regardless of whether it's the exact cause here.
REQUEST_TIMEOUT_SECONDS = 30

# v0.1.7: the last diagnostic log capture showed the real SRF response is
# dominated by verbose location metadata (station_id, alarm_region_name,
# district, geolocation_names...) before ever reaching whatever field
# holds the actual forecast data — the previous 500-character limit was
# entirely consumed by that metadata, so the actual data fields we
# actually need to see were cut off. Raised substantially so the next
# diagnostic capture (if still needed) shows enough to work with.
# v0.1.8: raised again — the 4000-character limit from v0.1.7 was
# entirely consumed by the geolocation metadata plus a 5-7 day forecast
# array, cut off mid-array, before ever reaching whatever comes after it
# in the response. That capture is what confirmed the real "day" array
# structure (see _DAY_FIELD_MAP below), for THIS endpoint specifically —
# a genuinely different endpoint (v2/forecastpoint, see
# FORECASTPOINT_URL_TEMPLATE below), confirmed in v0.1.18 via a
# standalone probe script run against the real API, does return "hours"
# and "three_hours" siblings with real hourly data. Still kept high in
# case a future response from either endpoint needs full capture again.
DIAGNOSTIC_LOG_TRUNCATION_CHARS = 20000


def _client_timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)


TOKEN_URL = "https://api.srgssr.ch/oauth/v1/accesstoken?grant_type=client_credentials"
# v0.1.3 fix: both URLs below had an incorrect /v2/ path segment, and the
# forecast URL additionally passed geolocationId as a query parameter
# instead of a path parameter. Confirmed correct structure from the
# official SRG-SSR docs ("/forecast/{geolocationId}") and a real working
# example hitting this exact API successfully
# (https://api.srgssr.ch/srf-meteo/forecast/47.3965,8.4894) — "V2" refers
# to the developer-portal product/subscription tier, not a URL version.
GEOLOCATION_URL = "https://api.srgssr.ch/srf-meteo/geolocations"


def build_forecast_url(geolocation_id: str) -> str:
    return f"https://api.srgssr.ch/srf-meteo/forecast/{geolocation_id}"


# v0.1.18: confirmed working via a standalone probe script (not part of
# this codebase) run directly against the real API — returns "hours",
# "three_hours", and "days" as top-level siblings alongside
# "geolocation", NOT wrapped in a "forecast" key the way both this
# project's own v0.1.8 finding (for the OLD endpoint above) and an
# outside reference's assumption both expected. Confirmed field names
# also differ from that reference's guesses in places (e.g. TTTFEEL_C,
# not FEELSTTT_C; UVI, not UV_INDEX) — see parse_forecastpoint_response
# below for the full, confirmed mapping. This supersedes the old
# endpoint entirely: it returns everything build_forecast_url's daily
# data did, plus genuine hourly and three-hourly granularity, in the
# same one HTTP call.
FORECASTPOINT_URL_TEMPLATE = "https://api.srgssr.ch/srf-meteo/v2/forecastpoint/{geolocation_id}"


def build_forecastpoint_url(geolocation_id: str) -> str:
    return FORECASTPOINT_URL_TEMPLATE.format(geolocation_id=geolocation_id)


# SRF reports wind in km/h; every other source in this project reports
# wind_speed in m/s (v0.1.5: &wind_speed_unit=ms for Open-Meteo). Storing
# SRF's raw km/h value under the same "wind_speed" variable name other
# sources use would silently corrupt Model A's blend — this constant
# makes the conversion explicit and impossible to miss in review.
KMH_TO_MS = 1.0 / 3.6


def _kmh_to_ms(raw: Any) -> Optional[float]:
    """Convert a km/h reading to m/s, defensively.

    **v0.1.27 fix (SWF-P2-002).** Both wind call sites did
    `entry[srf_key] * KMH_TO_MS` directly on the raw JSON value. A
    provider returning a string ("12") raises TypeError inside the
    parser, and a non-finite float sails through into storage — either
    way the arithmetic happens BEFORE provider_validation.py's shared
    physical-bounds check ever sees the value, so that safety net cannot
    help. A TypeError here is not contained gracefully: it aborts the
    whole SRF parse, discarding every other variable in the response.

    Returns None for anything that is not a finite number, which is the
    same shape the surrounding code already uses for a missing field.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value * KMH_TO_MS

TOKEN_LIFETIME = timedelta(days=7)
# Refresh a bit before actual expiry rather than reacting only to a 401.
TOKEN_REFRESH_MARGIN = timedelta(days=1)


def parse_srf_error_detail(body_text: str) -> Optional[str]:
    """SRF's own structured error shape, confirmed via a live probe
    against a real account that had hit a real restriction:
    `{"code": "400.01.007", "message": "location mismatch for developer
    app", "info": "You have exceeded your location limit"}`.

    **v0.1.21**: added after exactly that error was the actual root
    cause of expert_weight_srf staying Unknown — the free SRF API plan
    allows exactly ONE registered location per developer app, with no
    self-service reset once a location is claimed (confirmed directly
    with SRF, not guessed). This is an SRG-SSR account/API-plan
    restriction, not something any code change here can work around —
    but surfacing SRF's own code/message/info verbatim, distinctly from
    a generic HTTP error, means whoever sees the log/diagnostics next
    time immediately knows to check their developer portal account
    rather than assume it's a code or network bug and spend hours
    debugging a coordinator/parser that was never broken.
    """
    try:
        payload = json.loads(body_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    message = payload.get("message")
    info = payload.get("info")
    if code is None and message is None:
        return None
    return " — ".join(str(p) for p in (code, message, info) if p)


def build_basic_auth_header(consumer_key: str, consumer_secret: str) -> str:
    raw = f"{consumer_key}:{consumer_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True)
class CachedToken:
    access_token: str
    obtained_at: datetime

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.obtained_at + TOKEN_LIFETIME - TOKEN_REFRESH_MARGIN


def parse_token_response(payload: dict[str, Any]) -> str:
    token = payload.get("access_token")
    if not token:
        raise ValueError(f"No access_token in SRF token response: {payload!r}")
    return token


def parse_geolocation_response(payload: Any) -> Optional[str]:
    """SRF's geolocation search returns matches; take the first one.

    **v0.1.19 note**: the docstring here previously said "take the
    closest one", but the implementation has only ever taken
    `results[0]` — it does not compute or compare distance to the
    configured coordinates. Since the request itself is a coordinate-based
    search (`?latitude=...&longitude=...`), it's a reasonable expectation
    that the API already returns its best/nearest match first, but that
    ordering has never been independently confirmed against a real
    response containing multiple candidates, so it's a "most likely
    correct" assumption, not a verified one. Deliberately NOT changed to
    an actual distance calculation in this pass: none of the three
    confirmed response shapes below include a documented lat/lon or
    distance field on each entry, and every other fix in this file was
    made specifically by matching against a real, captured API response
    rather than guessing at a field shape (that discipline is why the
    v0.1.1/v0.1.4/v0.1.8 fixes below exist in the first place — guessing
    at SRF's shapes has been wrong before). If a live multi-result
    response is captured showing the actual sort order (or an explicit
    distance/lat/lon field per entry), that should replace this docstring
    fix with a real one. Tracked as a follow-up risk, not a confirmed
    defect, in the SRF weighting audit.

    **Fixed in v0.1.1**: this crashed in production with 'list' object has
    no attribute 'get' — the actual response is very likely a bare JSON
    array at the top level, not the dict-wrapped shape
    ({"geolocations": [...]}) this originally assumed from documentation
    alone, never verified against a live call before deployment (flagged
    as an outstanding item at the time). Handles both shapes now rather
    than gambling on which one is actually correct, since the raw
    response body wasn't captured to confirm definitively.

    **v0.1.4**: also defends against each list entry being a bare string
    (e.g. the coordinate itself, like "46.9480,7.4474") rather than an
    object with a geolocationId/id field — a third SRF response-shape
    surprise made this worth guarding rather than assuming a fixed shape.
    If an entry is already a string, it's presumably already usable as
    the ID directly.
    """
    if isinstance(payload, list):
        results = payload
    elif isinstance(payload, dict):
        results = payload.get("geolocations") or payload.get("results") or []
    else:
        return None
    if not results:
        return None
    first = results[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        return first.get("geolocationId") or first.get("id")
    return None


# v0.1.8: replaced entirely with confirmed field names from a real
# production response (previous guesses — temperature, relativeHumidity,
# meanSeaLevelPressure — never appeared in any actual SRF response and
# were the reason this kept returning zero usable points). The real
# response nests under forecast.day (a list), with genuinely DAILY
# fields: TX_C/TN_C (day max/min), RRR_MM (day precip total), FF_KMH (day
# avg wind), no humidity or pressure field visible at all at this
# granularity.
#
# Deliberately mapped to measurement names DISTINCT from the hourly ones
# ("temperature", "humidity", "pressure", "precip", "wind_speed") that
# CH1/CH2/D2/meteoblue use — a day's maximum temperature is not the
# temperature at any specific hour, and silently writing it into the same
# hourly bucket system Model A learns from would corrupt bias-learning
# for whatever hour it got assigned to. Named distinctly here, these
# simply don't participate in Model A's hourly blend at all, which is the
# correct, safe behavior until the actual hourly structure (if SRF's API
# offers one — see DEVELOPER.md) is confirmed.
_DAY_FIELD_MAP = {
    "TX_C": "temperature_daily_max",
    "TN_C": "temperature_daily_min",
    "RRR_MM": "precip_daily_total",
    "FF_KMH": "wind_speed_daily_avg",
}


@dataclass(frozen=True)
class SrfForecastPoint:
    variable: str
    valid_at: datetime
    value: Optional[float]


def parse_forecast_response(payload: Any) -> list[SrfForecastPoint]:
    """**v0.1.8**: rebuilt against a confirmed real response body rather
    than documentation or further guesswork. The real shape is
    `{"forecast": {"day": [...]}}` — `forecast` is a dict, not a list, so
    the earlier `payload.get("forecast", [])` silently produced an empty
    list every time via the "not a list" defensive check added in
    v0.1.4 (which correctly avoided crashing, but also correctly found
    nothing, since it really was looking in the wrong place). Still
    defends against every level being an unexpected shape, same
    discipline as before — this response family has surprised this
    project three times already.
    """
    if isinstance(payload, dict):
        forecast = payload.get("forecast")
    else:
        forecast = None

    if isinstance(forecast, dict):
        day_entries = forecast.get("day", [])
    elif isinstance(forecast, list):
        # In case a future/different response variant puts the array
        # directly under "forecast" rather than "forecast.day".
        day_entries = forecast
    else:
        day_entries = []

    if not isinstance(day_entries, list):
        day_entries = []

    points: list[SrfForecastPoint] = []
    for entry in day_entries:
        if not isinstance(entry, dict):
            continue
        valid_at_str = entry.get("local_date_time")
        if not valid_at_str:
            continue
        valid_at = datetime.fromisoformat(valid_at_str)
        # v0.1.19 fix: this only handled the naive case (assume UTC),
        # but left offset-AWARE timestamps (e.g. "...+02:00" CEST, which
        # is what the daily endpoint actually returns per the "local" in
        # local_date_time) with their original offset intact instead of
        # normalizing to UTC. Model A's blend and the reconciliation
        # queries in storage/db.py compare/sort valid_at as exact ISO
        # strings, so an un-normalized "...+02:00" row would never match
        # (or would sort incorrectly against) the UTC "...+00:00" keys
        # every other source and the forecastpoint path already use —
        # the row would look stored but be invisible to the blend. Same
        # normalization _parse_entry_datetime already applies below for
        # the hourly/forecastpoint path, now applied consistently here
        # too, whether the timestamp is naive or already offset-aware.
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=timezone.utc)
        else:
            valid_at = valid_at.astimezone(timezone.utc)
        for srf_key, internal_name in _DAY_FIELD_MAP.items():
            if srf_key in entry and entry[srf_key] is not None:
                points.append(
                    SrfForecastPoint(
                        variable=internal_name, valid_at=valid_at, value=entry[srf_key]
                    )
                )
    return points


# v0.1.18: field names confirmed against a real, successful response from
# the NEW v2/forecastpoint endpoint (build_forecastpoint_url above) —
# same discipline as every other SRF field mapping in this file: verified
# against a live call, not assumed from documentation or another
# reference's guesses (which turned out wrong in places — TTTFEEL_C not
# FEELSTTT_C, UVI not UV_INDEX).
#
# Split into two maps because "hours"/"three_hours" entries and "days"
# entries have genuinely different field sets (days adds SUNRISE/SUNSET/
# SUN_H/UVI/TX_C/TN_C in place of a single current temperature).
#
# The five measurements Model A's blend actually looks up
# (temperature/humidity/pressure/precip/wind_speed) use the SAME names
# every other source uses, so SRF's hourly data can finally participate
# in the blend rather than being permanently excluded from it. Every
# other confirmed field is prefixed srf_ specifically so it can never be
# mistaken for one of those five and accidentally picked up by the blend
# coordinator's generic queries.
_HOURLY_SIMPLE_FIELD_MAP = {
    "TTT_C": "temperature",
    "RELHUM_PERCENT": "humidity",
    "PRESSURE_HPA": "pressure",
    "RRR_MM": "precip",
    "TTL_C": "srf_temp_low_bound",
    "TTH_C": "srf_temp_high_bound",
    "DEWPOINT_C": "srf_dewpoint",
    "TTTFEEL_C": "srf_feels_like",
    "FRESHSNOW_MM": "srf_freshsnow",
    "SUN_MIN": "srf_sun_minutes",
    "IRRADIANCE_WM2": "srf_irradiance",
    "PROBPCP_PERCENT": "srf_precip_probability",
    "DD_DEG": "srf_wind_direction",
    "symbol_code": "srf_symbol_code",
    "symbol24_code": "srf_symbol24_code",
}
# FF_KMH (-> wind_speed) and FX_KMH (-> srf_wind_gust) both need the
# km/h -> m/s conversion, handled separately from the simple map above —
# see KMH_TO_MS.
_HOURLY_WIND_FIELD_MAP = {"FF_KMH": "wind_speed", "FX_KMH": "srf_wind_gust"}

# Daily fields keep the existing v0.1.8 naming for the four already in
# use (temperature_daily_max/min, precip_daily_total) — new confirmed
# extras are prefixed the same way the hourly ones are. wind_speed_daily_avg
# also needs the km/h conversion, handled alongside FX_KMH below.
# SUNRISE/SUNSET are timestamps, not numbers — not stored in
# forecast_snapshots (a REAL/float column, and not a learning-relevant
# quantity in the first place — Home Assistant's own sun entity already
# tracks this astronomically). Not "lost" so much as genuinely redundant
# with something HA already provides.
_DAILY_SIMPLE_FIELD_MAP = {
    "TX_C": "temperature_daily_max",
    "TN_C": "temperature_daily_min",
    "RRR_MM": "precip_daily_total",
    "UVI": "srf_daily_uv_index",
    "SUN_H": "srf_daily_sun_hours",
    "PROBPCP_PERCENT": "srf_daily_precip_probability",
    "DD_DEG": "srf_daily_wind_direction",
    "symbol_code": "srf_daily_symbol_code",
    "symbol24_code": "srf_daily_symbol24_code",
}
_DAILY_WIND_FIELD_MAP = {"FF_KMH": "wind_speed_daily_avg", "FX_KMH": "srf_daily_wind_gust"}


def _parse_entry_datetime(entry: dict[str, Any]) -> Optional[datetime]:
    date_time_str = entry.get("date_time")
    if not date_time_str:
        return None
    try:
        valid_at = datetime.fromisoformat(date_time_str)
    except ValueError:
        return None
    if valid_at.tzinfo is None:
        valid_at = valid_at.replace(tzinfo=timezone.utc)
    # Stored in UTC throughout this project, regardless of the offset
    # (+02:00 CEST, confirmed) the API itself returns.
    return valid_at.astimezone(timezone.utc)


def _points_from_hourly_entries(
    entries: list,
) -> dict[datetime, dict[str, SrfForecastPoint]]:
    """Returns points grouped by valid_at AND THEN by internal variable
    name (not a flat list per timestamp) — the caller merges "hours" and
    "three_hours" results together, preferring "hours" for any (timestamp,
    variable) pair both cover, so this needs to be keyed at the variable
    level, not just the timestamp level, for that merge to be safe.

    **v0.1.19 fix**: this used to return a flat `list[SrfForecastPoint]`
    per timestamp. The caller then did a dict-level `.update()` keyed only
    by valid_at, which replaced the ENTIRE list for a timestamp rather
    than merging field-by-field — so if "three_hours" had a field (e.g.
    precip) that "hours" didn't report for the same valid_at, "hours"
    winning at the whole-entry level silently discarded it. Keying by
    (valid_at, variable) instead means the merge below can compare and
    combine at the field level, which is what the docstring below always
    said the intent was.
    """
    by_valid_at: dict[datetime, dict[str, SrfForecastPoint]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        valid_at = _parse_entry_datetime(entry)
        if valid_at is None:
            continue
        variables: dict[str, SrfForecastPoint] = {}
        for srf_key, internal_name in _HOURLY_SIMPLE_FIELD_MAP.items():
            if srf_key in entry and entry[srf_key] is not None:
                variables[internal_name] = SrfForecastPoint(
                    variable=internal_name, valid_at=valid_at, value=entry[srf_key]
                )
        for srf_key, internal_name in _HOURLY_WIND_FIELD_MAP.items():
            if srf_key in entry and entry[srf_key] is not None:
                variables[internal_name] = SrfForecastPoint(
                    variable=internal_name,
                    valid_at=valid_at,
                    value=_kmh_to_ms(entry[srf_key]),
                )
        by_valid_at[valid_at] = variables
    return by_valid_at


def parse_forecastpoint_response(payload: Any) -> list[SrfForecastPoint]:
    """**v0.1.18**: parses the NEW v2/forecastpoint endpoint's confirmed
    response shape — "hours", "three_hours", and "days" as top-level
    siblings alongside "geolocation", not wrapped in a "forecast" key.

    "hours" and "three_hours" cover overlapping time ranges (confirmed:
    99 hourly entries starting at the top of the current day, 70
    three-hourly entries starting 2 hours later the same day) — for any
    (timestamp, variable) pair both provide data for, "hours" wins (finer
    granularity), with "three_hours" filling in whatever extends beyond
    what "hours" covers, INCLUDING variables "hours" simply doesn't report
    for a timestamp both sources otherwise share. Without this merge, both
    would insert rows for the same (source, variable, valid_at), and which
    one the blend coordinator's bulk query picks up would depend on
    insertion order rather than being a deliberate choice.

    **v0.1.19 fix**: the merge used to be a dict `.update()` keyed only by
    valid_at, which replaced three_hours' entire point list for a
    timestamp with hours' list whenever both covered it — discarding any
    field three_hours had that hours didn't, even though they weren't
    actually in conflict. Now merges per (valid_at, variable): three_hours
    is the base layer, and hours only overwrites the specific variables it
    itself provides at a given timestamp, leaving three_hours-only fields
    at that same timestamp intact.
    """
    if not isinstance(payload, dict):
        return []

    # three_hours is the base layer (broader, coarser coverage); hours
    # then overwrites only the specific (timestamp, variable) pairs it
    # itself provides — see docstring for why this is per-field, not
    # per-timestamp.
    merged: dict[datetime, dict[str, SrfForecastPoint]] = {}
    three_hours = payload.get("three_hours")
    if isinstance(three_hours, list):
        for valid_at, variables in _points_from_hourly_entries(three_hours).items():
            merged.setdefault(valid_at, {}).update(variables)
    hours = payload.get("hours")
    if isinstance(hours, list):
        for valid_at, variables in _points_from_hourly_entries(hours).items():
            merged.setdefault(valid_at, {}).update(variables)

    points: list[SrfForecastPoint] = []
    for variables in merged.values():
        points.extend(variables.values())

    days = payload.get("days")
    if isinstance(days, list):
        for entry in days:
            if not isinstance(entry, dict):
                continue
            valid_at = _parse_entry_datetime(entry)
            if valid_at is None:
                continue
            for srf_key, internal_name in _DAILY_SIMPLE_FIELD_MAP.items():
                if srf_key in entry and entry[srf_key] is not None:
                    points.append(
                        SrfForecastPoint(variable=internal_name, valid_at=valid_at, value=entry[srf_key])
                    )
            for srf_key, internal_name in _DAILY_WIND_FIELD_MAP.items():
                if srf_key in entry and entry[srf_key] is not None:
                    points.append(
                        SrfForecastPoint(
                            variable=internal_name, valid_at=valid_at,
                            value=_kmh_to_ms(entry[srf_key]),
                        )
                    )
    return points


class SrfClient:
    """Requires an aiohttp.ClientSession (HA's shared session).

    diagnostics/latitude/longitude are optional (v0.1.9) — when a
    DiagnosticsRecorder is provided and enabled, raw response bodies for
    the "parsed successfully but found zero usable data" cases are
    recorded there in full (redacted first — see redaction.py), not just
    logged as a truncated string. latitude/longitude are needed for the
    coordinate-value redaction pass specifically.
    """

    def __init__(
        self,
        session: Any,
        consumer_key: str,
        consumer_secret: str,
        *,
        diagnostics: Any = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> None:
        self._session = session
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._token: Optional[CachedToken] = None
        self._geolocation_id: Optional[str] = None
        self._geolocation_coords: Optional[tuple[float, float]] = None
        self._diagnostics = diagnostics
        self._latitude = latitude
        self._longitude = longitude

    def _record_diagnostic(self, *, event_type: str, detail: str, raw_payload: Any) -> None:
        if self._diagnostics is None or not getattr(self._diagnostics, "enabled", False):
            return
        from ..redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(
            raw_payload, latitude=self._latitude or 0.0, longitude=self._longitude or 0.0
        )
        self._diagnostics.record(
            source="srf", event_type=event_type, detail=detail,
            extra={"raw_response": redacted},
        )

    async def _async_ensure_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh or self._token is None or self._token.is_expired():
            headers = {
                "Authorization": build_basic_auth_header(
                    self._consumer_key, self._consumer_secret
                )
            }
            timeout = _client_timeout()
            async with self._session.post(
                TOKEN_URL, headers=headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
            access_token = parse_token_response(payload)
            self._token = CachedToken(
                access_token=access_token, obtained_at=datetime.now(timezone.utc)
            )
        return self._token.access_token

    async def _async_get_with_token_retry(self, url: str, *, params: Optional[dict] = None):
        """v0.1.23 fix (L-12): performs an authenticated GET, and on a 401
        (the cached token was rejected — revoked, invalidated server-side,
        or otherwise no longer valid despite not yet being locally
        expired) clears the cache, refreshes the token exactly once, and
        retries the same request exactly once.

        Previously the only trigger for a token refresh was local expiry
        (CachedToken.is_expired()) — a token invalidated for any other
        reason (revocation, a key rotation on SRF's side, etc.) would
        stay cached and keep being sent as-is until its local expiry
        arrived on its own, causing repeated auth failures on every poll
        in between instead of self-healing after one clean refresh.

        Returns the awaited response body as parsed JSON, matching what
        each call site previously did inline — callers no longer manage
        the `async with` block or the token header themselves.
        """
        token = await self._async_ensure_token()
        resp_json, status, body_text = await self._async_get_raw(url, token=token, params=params)
        if status == 401:
            _LOGGER.warning(
                "SRF request returned 401 with a cached token that wasn't "
                "locally expired — clearing it and refreshing once before "
                "retrying this request."
            )
            token = await self._async_ensure_token(force_refresh=True)
            resp_json, status, body_text = await self._async_get_raw(url, token=token, params=params)
        if status != 200:
            self._raise_for_status(status=status, body_text=body_text)
        return resp_json

    async def _async_get_raw(
        self, url: str, *, token: str, params: Optional[dict] = None
    ) -> tuple[Any, int, str]:
        """Single GET attempt — reads the body regardless of status (SRF
        returns structured error detail in the body on 4xx, see
        parse_srf_error_detail), returning (parsed_json_or_None, status,
        raw_body_text) rather than raising, so callers (the 401-retry
        wrapper above) can decide what to do before committing to an
        error."""
        headers = {"Authorization": f"Bearer {token}"}
        async with self._session.get(
            url, headers=headers, params=params, timeout=_client_timeout()
        ) as resp:
            status = resp.status
            body_text = await resp.text()
        parsed = None
        if status == 200:
            try:
                parsed = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        return parsed, status, body_text

    def _raise_for_status(self, *, status: int, body_text: str) -> None:
        """v0.1.23 fix (L-11): classifies a non-200 response instead of
        always raising a plain, unclassified error. 4xx is treated as
        permanent (raises SrfPermanentError, carrying the status so
        health.py's classify_exception still buckets 401/403 as 'auth'
        distinctly) — the caller must NOT attempt the daily-fallback
        endpoint for these, since a permanent rejection of the primary
        endpoint has no reason to succeed against the fallback either,
        and retrying it every poll just wastes a call and hides the real
        cause. 5xx (or anything else) raises a plain RuntimeError, which
        the coordinator's existing broad `except Exception` still treats
        as fallback-eligible, matching the pre-fix behavior for genuinely
        transient failures.
        """
        srf_detail = parse_srf_error_detail(body_text)
        if 400 <= status < 500:
            detail_suffix = f": {srf_detail}" if srf_detail else ""
            raise SrfPermanentError(
                f"SRF request rejected (HTTP {status}){detail_suffix}. This "
                f"is very likely a permanent SRG-SSR account/API-plan "
                f"restriction or an authentication problem (e.g. the free "
                f"plan's one-registered-location limit — confirmed as the "
                f"real cause once before), not a transient network issue — "
                f"not retrying via the fallback endpoint. Check the app's "
                f"registered locations at https://developer.srgssr.ch or "
                f"contact meteo.api@srgssr.ch.",
                status=status,
            )
        raise RuntimeError(f"SRF request failed with HTTP {status}: {body_text[:500]}")

    async def _async_ensure_geolocation_id(self, latitude: float, longitude: float) -> str:
        # Cache by coordinates — only re-resolve if the configured location
        # actually changes, not on every poll.
        if self._geolocation_id is not None and self._geolocation_coords == (
            latitude,
            longitude,
        ):
            return self._geolocation_id

        params = {"latitude": latitude, "longitude": longitude}
        payload = await self._async_get_with_token_retry(GEOLOCATION_URL, params=params)
        geolocation_id = parse_geolocation_response(payload)
        if geolocation_id is None:
            # v0.1.4: log what was actually received rather than just
            # raising a generic message — this is the third distinct SRF
            # response-shape surprise, so capturing real evidence here
            # beats guessing again if this happens once more.
            _LOGGER.error(
                "SRF geolocation lookup returned no usable result. Raw "
                "response (truncated): %s",
                repr(payload)[:DIAGNOSTIC_LOG_TRUNCATION_CHARS],
            )
            # v0.1.9: when diagnostic logging is enabled, also capture the
            # FULL (redacted, not truncated) payload for download via
            # Home Assistant's own Diagnostics button — no more guessing
            # at a truncation length that might cut off the useful part.
            self._record_diagnostic(
                event_type="raw_response",
                detail="SRF geolocation lookup returned no usable result",
                raw_payload=payload,
            )
            raise ValueError("SRF geolocation lookup returned no results")
        # v0.1.20 fix: this branch (successful resolution) never recorded
        # anything in diagnostics — only the "no usable result" failure
        # path above did. That meant a *successful* geolocation lookup
        # that nonetheless resolved to an ID v2/forecastpoint later
        # rejects (see the v0.1.20 changelog entry on the 400 Bad Request
        # investigation) left no trace of what SRF actually returned —
        # how many candidates, what shape, whether the chosen result
        # even looks like a real registered point. Recording it here
        # (only need to do this once per coordinate change, thanks to
        # the cache above, so this doesn't spam diagnostics every poll).
        self._record_diagnostic(
            event_type="raw_response",
            detail=f"SRF geolocation lookup resolved to id={geolocation_id!r}",
            raw_payload=payload,
        )
        self._geolocation_id = geolocation_id
        self._geolocation_coords = (latitude, longitude)
        return geolocation_id

    async def async_fetch_forecast(
        self, *, latitude: float, longitude: float
    ) -> list[SrfForecastPoint]:
        geolocation_id = await self._async_ensure_geolocation_id(latitude, longitude)
        url = build_forecast_url(geolocation_id)
        # v0.1.23 fix (L-12): _async_get_with_token_retry replaces the
        # previous inline token+GET, adding the 401-clear-and-refresh-once
        # behavior uniformly to every SRF request, not just this one.
        payload = await self._async_get_with_token_retry(url)
        points = parse_forecast_response(payload)
        if not points:
            # v0.1.4: same reasoning as the geolocation lookup above —
            # parsing is now defensive (skips what it doesn't recognize
            # rather than crashing on it), which means a genuinely
            # unexpected response shape could otherwise fail silently
            # instead of loudly. Logging the raw body here means the next
            # surprise comes with real evidence attached.
            _LOGGER.warning(
                "SRF forecast response produced no usable data points. Raw "
                "response (truncated): %s",
                repr(payload)[:DIAGNOSTIC_LOG_TRUNCATION_CHARS],
            )
            self._record_diagnostic(
                event_type="raw_response",
                detail="SRF forecast response produced no usable data points",
                raw_payload=payload,
            )
        else:
            # v0.1.9: also capture successful responses in full when
            # diagnostics is enabled — specifically useful for the still-
            # open question of whether the response contains an "hour" or
            # "three_hours" array beyond what any fixed log-truncation
            # length happened to reach (see DEVELOPER.md).
            self._record_diagnostic(
                event_type="raw_response",
                detail=f"SRF forecast response parsed successfully ({len(points)} points)",
                raw_payload=payload,
            )
        return points

    async def async_fetch_forecastpoint(
        self, *, latitude: float, longitude: float
    ) -> list[SrfForecastPoint]:
        """**v0.1.18**: the primary fetch method going forward — one HTTP
        call to the confirmed-working v2/forecastpoint endpoint, returning
        genuine hourly, three-hourly, AND daily data together (superseding
        async_fetch_forecast above, which only ever had daily data).
        Deliberately still one call, not two — this endpoint already
        returns everything the old one did plus much more, so there's no
        reason to also call the old endpoint separately.

        If this fails for any reason (confirmed working today, but SRF's
        API has surprised this project enough times that a graceful
        fallback is worth having), the caller falls back to
        async_fetch_forecast — daily-only data is better than none.

        v0.1.23 fix (L-11/L-12): now goes through
        _async_get_with_token_retry, which (a) clears and refreshes a
        rejected cached token once before giving up (L-12), and (b)
        raises SrfPermanentError specifically for 4xx responses (L-11) —
        the caller (SrfCoordinator._async_update_data) must NOT treat
        that as fallback-eligible the way it treats a genuine transient
        failure, since a permanent rejection of this endpoint has no
        reason to succeed against the fallback either.
        """
        geolocation_id = await self._async_ensure_geolocation_id(latitude, longitude)
        url = build_forecastpoint_url(geolocation_id)
        payload = await self._async_get_with_token_retry(url)
        points = parse_forecastpoint_response(payload)
        if not points:
            _LOGGER.warning(
                "SRF forecastpoint response produced no usable data points. "
                "Raw response (truncated): %s",
                repr(payload)[:DIAGNOSTIC_LOG_TRUNCATION_CHARS],
            )
            self._record_diagnostic(
                event_type="raw_response",
                detail="SRF forecastpoint response produced no usable data points",
                raw_payload=payload,
            )
        else:
            self._record_diagnostic(
                event_type="raw_response",
                detail=f"SRF forecastpoint response parsed successfully ({len(points)} points)",
                raw_payload=payload,
            )
        return points
