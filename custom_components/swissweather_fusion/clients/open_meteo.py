"""Shared client for ICON-CH1-EPS, ICON-CH2-EPS, and DWD ICON-D2, all via
Open-Meteo's free, no-key JSON API.

One client for all three models since they share the same API shape — only
the `models=` parameter differs. See DEVELOPER.md for why this project uses
Open-Meteo rather than MeteoSwiss's own raw GRIB feed (24h retention limit,
heavier parsing) and why ICON-D2 is a Model A blend expert only, never a
trigger source (it reruns every 3h at the source, same as CH1 — an early
poll gets nothing new).

URL-building and response-parsing are pure functions, deliberately separate
from the async I/O, so they're unit-testable without a live network call —
see tests/test_open_meteo.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

FORECAST_BASE_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_BASE_URL = "https://api.open-meteo.com/v1/elevation"

MODEL_PARAM = {
    # Corrected in v0.1.1: these were invented plausible-looking names
    # (icon_ch1_eps/icon_ch2_eps) rather than checked against Open-Meteo's
    # actual docs, causing every CH1/CH2 request to 400. The real values
    # confirmed from open-meteo.com/en/docs/meteoswiss-api:
    "ch1": "meteoswiss_icon_ch1",
    "ch2": "meteoswiss_icon_ch2",
    # dwd_icon_d2 was already correct — matches the dwd_icon_seamless/
    # dwd_icon_eu/dwd_icon_d2 naming pattern confirmed on DWD's own docs
    # page, so this one needed no change.
    "icon_d2": "dwd_icon_d2",
}

# Variables requested for every model — kept identical across CH1/CH2/D2 so
# Model A's blend always has the same measurement set to work with, even
# though the underlying models don't all expose identical native fields.
HOURLY_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
    "precipitation",
    "wind_speed_10m",
)


def build_forecast_url(
    *, source: str, latitude: float, longitude: float
) -> str:
    """Build the forecast request URL for one of ch1/ch2/icon_d2."""
    if source not in MODEL_PARAM:
        raise ValueError(f"Unknown Open-Meteo source: {source!r}")
    variables = ",".join(HOURLY_VARIABLES)
    return (
        f"{FORECAST_BASE_URL}?latitude={latitude}&longitude={longitude}"
        f"&hourly={variables}&models={MODEL_PARAM[source]}&timeformat=iso8601"
        f"&timezone=UTC"
    )


def build_elevation_url(*, latitude: float, longitude: float) -> str:
    return f"{ELEVATION_BASE_URL}?latitude={latitude}&longitude={longitude}"


@dataclass(frozen=True)
class ForecastPoint:
    variable: str
    valid_at: datetime
    value: Optional[float]


@dataclass(frozen=True)
class ParsedForecast:
    issued_at: datetime
    points: list[ForecastPoint]


# Maps Open-Meteo's hourly response keys back to this project's internal
# measurement names (matching bucket_stats.measurement and
# forecast_snapshots.variable).
_VARIABLE_NAME_MAP = {
    "temperature_2m": "temperature",
    "relative_humidity_2m": "humidity",
    "surface_pressure": "pressure",
    "precipitation": "precip",
    "wind_speed_10m": "wind_speed",
}


def parse_forecast_response(payload: dict[str, Any]) -> ParsedForecast:
    """Parse Open-Meteo's JSON response into a flat list of forecast points.

    `issued_at` uses Open-Meteo's own current time as a stand-in for the
    true model run/reference time — Open-Meteo doesn't surface the
    upstream model's exact reference time in this response shape, so the
    lead-time bucket derivation (model_a.derive_lead_time_bucket) treats
    "time of this successful poll" as issued_at. This is a deliberate
    simplification worth revisiting if lead-time bucketing looks wrong
    against real accuracy data once the system is running.
    """
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    issued_at = datetime.now(timezone.utc)

    points: list[ForecastPoint] = []
    for open_meteo_key, internal_name in _VARIABLE_NAME_MAP.items():
        values = hourly.get(open_meteo_key)
        if values is None:
            continue
        for t_str, value in zip(times, values):
            valid_at = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
            points.append(
                ForecastPoint(variable=internal_name, valid_at=valid_at, value=value)
            )
    return ParsedForecast(issued_at=issued_at, points=points)


def parse_elevation_response(payload: dict[str, Any]) -> Optional[float]:
    elevations = payload.get("elevation")
    if not elevations:
        return None
    return float(elevations[0])


def extract_error_reason(payload: dict[str, Any]) -> Optional[str]:
    """Open-Meteo's error responses include a human-readable "reason"
    field (e.g. "Cannot initialize model from invalid String value
    icon_ch1_eps for key models") — surfacing this instead of just an
    HTTP status code is exactly what would have made the wrong model
    identifier bug (v0.1.1) immediately obvious in the logs, rather than a
    bare "400, message='Bad Request'" with no indication of which
    parameter was wrong.
    """
    if payload.get("error"):
        return payload.get("reason")
    return None


# ---------------------------------------------------------------------------
# Async I/O — thin wrapper, kept separate from the pure functions above.
# ---------------------------------------------------------------------------


class OpenMeteoClient:
    """Requires an aiohttp.ClientSession, normally HA's shared session via
    homeassistant.helpers.aiohttp_client.async_get_clientsession(hass).
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def async_fetch_forecast(
        self, *, source: str, latitude: float, longitude: float
    ) -> ParsedForecast:
        url = build_forecast_url(source=source, latitude=latitude, longitude=longitude)
        async with self._session.get(url) as resp:
            if resp.status == 400:
                # Read the body before raise_for_status discards it — this
                # is what should have caught the v0.1.1 wrong-model-name
                # bug immediately instead of a bare "400 Bad Request".
                error_payload = await resp.json()
                reason = extract_error_reason(error_payload)
                if reason:
                    raise ValueError(f"Open-Meteo rejected the request: {reason}")
            resp.raise_for_status()
            payload = await resp.json()
        return parse_forecast_response(payload)

    async def async_fetch_elevation(
        self, *, latitude: float, longitude: float
    ) -> Optional[float]:
        url = build_elevation_url(latitude=latitude, longitude=longitude)
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_elevation_response(payload)
