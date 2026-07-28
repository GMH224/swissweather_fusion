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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

TOKEN_URL = "https://api.srgssr.ch/oauth/v1/accesstoken?grant_type=client_credentials"
GEOLOCATION_URL = "https://api.srgssr.ch/srf-meteo/v2/geolocations"
FORECAST_URL = "https://api.srgssr.ch/srf-meteo/v2/forecast"

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


def parse_geolocation_response(payload: dict[str, Any]) -> Optional[str]:
    """SRF's geolocation search returns a list; take the closest match.

    Exact response shape not yet confirmed against a live call (flagged in
    the plan doc as still outstanding) — this parses the documented shape
    and should be revisited against a real response before relying on it.
    """
    results = payload.get("geolocations") or payload.get("results") or []
    if not results:
        return None
    first = results[0]
    return first.get("geolocationId") or first.get("id")


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


def parse_forecast_response(payload: dict[str, Any]) -> list[SrfForecastPoint]:
    points: list[SrfForecastPoint] = []
    for entry in payload.get("forecast", []):
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
        params = {"geolocationId": geolocation_id}
        async with self._session.get(
            FORECAST_URL, headers=headers, params=params
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_forecast_response(payload)
