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

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

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

TOKEN_LIFETIME = timedelta(days=7)
# Refresh a bit before actual expiry rather than reacting only to a 401.
TOKEN_REFRESH_MARGIN = timedelta(days=1)


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
    """SRF's geolocation search returns matches; take the closest one.

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
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=timezone.utc)
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


def _points_from_hourly_entries(entries: list) -> dict[datetime, list[SrfForecastPoint]]:
    """Returns points grouped by valid_at, not a flat list — the caller
    merges "hours" and "three_hours" results together, preferring "hours"
    for any timestamp both cover, so this needs to be keyed for that.
    """
    by_valid_at: dict[datetime, list[SrfForecastPoint]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        valid_at = _parse_entry_datetime(entry)
        if valid_at is None:
            continue
        points: list[SrfForecastPoint] = []
        for srf_key, internal_name in _HOURLY_SIMPLE_FIELD_MAP.items():
            if srf_key in entry and entry[srf_key] is not None:
                points.append(SrfForecastPoint(variable=internal_name, valid_at=valid_at, value=entry[srf_key]))
        for srf_key, internal_name in _HOURLY_WIND_FIELD_MAP.items():
            if srf_key in entry and entry[srf_key] is not None:
                points.append(
                    SrfForecastPoint(
                        variable=internal_name, valid_at=valid_at, value=entry[srf_key] * KMH_TO_MS
                    )
                )
        by_valid_at[valid_at] = points
    return by_valid_at


def parse_forecastpoint_response(payload: Any) -> list[SrfForecastPoint]:
    """**v0.1.18**: parses the NEW v2/forecastpoint endpoint's confirmed
    response shape — "hours", "three_hours", and "days" as top-level
    siblings alongside "geolocation", not wrapped in a "forecast" key.

    "hours" and "three_hours" cover overlapping time ranges (confirmed:
    99 hourly entries starting at the top of the current day, 70
    three-hourly entries starting 2 hours later the same day) — for any
    timestamp both provide data for, "hours" wins (finer granularity),
    with "three_hours" filling in whatever extends beyond what "hours"
    covers. Without this merge, both would insert rows for the same
    (source, variable, valid_at), and which one the blend coordinator's
    bulk query picks up would depend on insertion order rather than being
    a deliberate choice.
    """
    if not isinstance(payload, dict):
        return []

    # three_hours populated first (broader, coarser coverage), then
    # overwritten by hours for any timestamp both have — see docstring.
    merged: dict[datetime, list[SrfForecastPoint]] = {}
    three_hours = payload.get("three_hours")
    if isinstance(three_hours, list):
        merged.update(_points_from_hourly_entries(three_hours))
    hours = payload.get("hours")
    if isinstance(hours, list):
        merged.update(_points_from_hourly_entries(hours))

    points: list[SrfForecastPoint] = []
    for entry_points in merged.values():
        points.extend(entry_points)

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
                            variable=internal_name, valid_at=valid_at, value=entry[srf_key] * KMH_TO_MS
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

    async def _async_ensure_token(self) -> str:
        if self._token is None or self._token.is_expired():
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

    async def _async_ensure_geolocation_id(self, latitude: float, longitude: float) -> str:
        # Cache by coordinates — only re-resolve if the configured location
        # actually changes, not on every poll.
        if self._geolocation_id is not None and self._geolocation_coords == (
            latitude,
            longitude,
        ):
            return self._geolocation_id

        token = await self._async_ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {"latitude": latitude, "longitude": longitude}
        async with self._session.get(
            GEOLOCATION_URL, headers=headers, params=params, timeout=_client_timeout()
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
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
        self._geolocation_id = geolocation_id
        self._geolocation_coords = (latitude, longitude)
        return geolocation_id

    async def async_fetch_forecast(
        self, *, latitude: float, longitude: float
    ) -> list[SrfForecastPoint]:
        token = await self._async_ensure_token()
        geolocation_id = await self._async_ensure_geolocation_id(latitude, longitude)
        headers = {"Authorization": f"Bearer {token}"}
        url = build_forecast_url(geolocation_id)
        async with self._session.get(
            url, headers=headers, timeout=_client_timeout()
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
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
        """
        token = await self._async_ensure_token()
        geolocation_id = await self._async_ensure_geolocation_id(latitude, longitude)
        headers = {"Authorization": f"Bearer {token}"}
        url = build_forecastpoint_url(geolocation_id)
        async with self._session.get(
            url, headers=headers, timeout=_client_timeout()
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
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
