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

_LOGGER = logging.getLogger(__name__)

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


# Fields expected in the V2 forecast response, per what was documented
# during planning. NOT yet verified against a live call for this project
# specifically — see plan doc §0 checklist. Verify before trusting this
# mapping in production.
_FIELD_MAP = {
    "temperature": "temperature",
    "relativeHumidity": "humidity",
    "meanSeaLevelPressure": "pressure",
    "precipitation": "precip",
    "windSpeed": "wind_speed",
}


@dataclass(frozen=True)
class SrfForecastPoint:
    variable: str
    valid_at: datetime
    value: Optional[float]


def parse_forecast_response(payload: Any) -> list[SrfForecastPoint]:
    """**Fixed in v0.1.1**: handles both a bare top-level list and a dict
    wrapping one, since the actual shape wasn't confirmed before the crash
    that surfaced this.

    **v0.1.4**: this is the more likely of the two parsers to have caused
    the 'str' object has no attribute 'get' crash, since it's the code
    path newly reached after the v0.1.3 URL fix — we'd never actually
    gotten a real 200 response with a real body to parse here before.
    Now defends against every level being a different shape than
    expected: payload itself being a bare string, entries not being a
    list at all, and individual entries being strings rather than dicts.
    Skips anything that doesn't fit rather than crashing on it.
    """
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("forecast", [])
    else:
        entries = []

    if not isinstance(entries, list):
        entries = []

    points: list[SrfForecastPoint] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        valid_at_str = entry.get("localDateTime") or entry.get("time")
        if not valid_at_str:
            continue
        valid_at = datetime.fromisoformat(valid_at_str)
        if valid_at.tzinfo is None:
            valid_at = valid_at.replace(tzinfo=timezone.utc)
        for srf_key, internal_name in _FIELD_MAP.items():
            if srf_key in entry:
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
            async with self._session.post(TOKEN_URL, headers=headers) as resp:
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
            GEOLOCATION_URL, headers=headers, params=params
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
                repr(payload)[:500],
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
        async with self._session.get(url, headers=headers) as resp:
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
                repr(payload)[:500],
            )
        return points
