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
# structure (see _DAY_FIELD_MAP below) — but it's still unknown whether
# the response also contains an "hour" or "three_hours" sibling array
# with genuine hourly data (a community-documented example of this same
# API family shows day/three_hours/hour arrays all present together).
# Raised well past what the full day array needs, so the next capture (if
# still needed) can confirm one way or the other.
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
    (e.g. the coordinate itself, like "47.5536,8.9120") rather than an
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


class SrfClient:
    """Requires an aiohttp.ClientSession (HA's shared session)."""

    def __init__(self, session: Any, consumer_key: str, consumer_secret: str) -> None:
        self._session = session
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._token: Optional[CachedToken] = None
        self._geolocation_id: Optional[str] = None
        self._geolocation_coords: Optional[tuple[float, float]] = None

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
        return points
