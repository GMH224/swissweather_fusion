"""Shared client for ICON-CH1-EPS, ICON-CH2-EPS, and DWD ICON-D2, all via
Open-Meteo's JSON API — free tier needs no key at all; an optional API key
(v0.1.3) switches to their paid/commercial tier for higher rate limits and
dedicated infrastructure. It does NOT make CH1/CH2/D2 refresh more often —
that's fixed by MeteoSwiss/DWD's own model run schedule regardless of
tier.

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

from ..fingerprint import compute_content_fingerprint

FREE_HOST = "api.open-meteo.com"
# Confirmed from Open-Meteo's own docs: using an apikey requires this
# customer- prefixed hostname, not just adding the parameter to the
# regular one — a plain apikey= on the free host is silently ignored.
CUSTOMER_HOST = "customer-api.open-meteo.com"

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
# v0.2.0: expanded from five variables to seventeen.
#
# The architecture review's governing rule is "do not infer a value when
# an upstream model provides it directly". Until v0.2.0 this client asked
# for five variables and the integration then INFERRED snow from
# temperature and cloud from humidity — guessing at answers ICON was
# willing to state outright.
#
# All of these are free-tier hourly variables on the same request, so the
# expansion costs no extra API calls and no quota. It does cost storage:
# roughly 3.4x the rows per run. See DEVELOPER.md.
HOURLY_VARIABLES = (
    # Class A — learned against the local station
    "temperature_2m",
    "relative_humidity_2m",
    "pressure_msl",
    # Class B — fused, not learned
    "precipitation",
    "rain",
    "showers",
    "snowfall",
    "snow_depth",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "dew_point_2m",
    "apparent_temperature",
    "cloud_cover",
    "visibility",
    # v0.2.5 — convective and vertical-structure variables.
    #
    # These were the missing half of Model B. const.py and model_b.py
    # both recorded that CAPE "was hoped for but turned out to require a
    # paid tier" — that conclusion was drawn from Meteonomiqs' /forecast2
    # and then carried for several releases without being re-checked
    # against the other providers. Open-Meteo offers CAPE and convective
    # inhibition FREE for all three ICON models, on the same request we
    # already make.
    #
    # Until now Model B had no instability input at all: it was a
    # rain-approach detector using station tendency and upwind radar,
    # with nothing describing whether the atmosphere was actually capable
    # of convection.
    "cape",
    "convective_inhibition",
    # Freezing level is the honest way to decide rain vs snow at a given
    # altitude, replacing a surface-temperature guess.
    "freezing_level_height",
    "snowfall_height",
    "cloud_base",
    "sunshine_duration",
    # Class C — categorical, never averaged
    "weather_code",
)

# v0.2.1: uv_index is requested SEPARATELY from the set above.
#
# It is a documented /v1/forecast hourly variable, but it is a derived
# product rather than a raw model field, and this client restricts each
# request to a single model with &models=. Whether every model accepts it
# is unverified against the live API — and the whole variable list goes in
# one URL, so a rejection would fail the request for that model entirely.
# Three sources dying at once to gain one nice-to-have is a bad trade.
#
# So it is opt-out: requested by default, and OpenMeteoCoordinator retries
# once without the optional set if a request fails permanently. Same
# reasoning as the v0.1.28 CombiPrecip lesson — do not let an unverified
# assumption about a provider take out a working path.
OPTIONAL_HOURLY_VARIABLES = ("uv_index",)


def _base_url(path: str, api_key: Optional[str]) -> str:
    host = CUSTOMER_HOST if api_key else FREE_HOST
    return f"https://{host}{path}"


def build_forecast_url(
    *,
    source: str,
    latitude: float,
    longitude: float,
    api_key: Optional[str] = None,
    include_optional: bool = True,
) -> str:
    """Build the forecast request URL for one of ch1/ch2/icon_d2."""
    if source not in MODEL_PARAM:
        raise ValueError(f"Unknown Open-Meteo source: {source!r}")
    variables = ",".join(
        HOURLY_VARIABLES + (OPTIONAL_HOURLY_VARIABLES if include_optional else ())
    )
    url = (
        f"{_base_url('/v1/forecast', api_key)}?latitude={latitude}&longitude={longitude}"
        f"&hourly={variables}&models={MODEL_PARAM[source]}&timeformat=iso8601"
        # v0.1.5 fix: Open-Meteo defaults wind speed to km/h, but
        # meteoblue's confirmed test response used values (0.94, 1.85,
        # etc.) consistent with m/s, not km/h — a real cross-source unit
        # mismatch, same class of bug as the earlier surface-vs-sea-level
        # pressure issue. Explicitly requesting m/s here to match, rather
        # than converting meteoblue's values, since this is the source
        # whose unit is actually configurable via a URL parameter.
        f"&wind_speed_unit=ms"
        f"&timezone=UTC"
    )
    if api_key:
        url += f"&apikey={api_key}"
    return url


def build_elevation_url(
    *, latitude: float, longitude: float, api_key: Optional[str] = None
) -> str:
    url = f"{_base_url('/v1/elevation', api_key)}?latitude={latitude}&longitude={longitude}"
    if api_key:
        url += f"&apikey={api_key}"
    return url


@dataclass(frozen=True)
class ForecastPoint:
    variable: str
    valid_at: datetime
    value: Optional[float]


@dataclass(frozen=True)
class ParsedForecast:
    issued_at: datetime
    points: list[ForecastPoint]
    # v0.1.15: the model grid cell's own elevation, confirmed present as a
    # top-level field in every real Open-Meteo response (not per-hour,
    # per-query — one number for the whole response). This is what makes
    # the lapse-rate pre-correction in models/model_a.py usable: without
    # knowing the grid's own elevation, there's nothing to compare the
    # configured actual elevation against.
    grid_elevation_m: Optional[float] = None
    # v0.1.19 fix: names of hourly variables whose value array length
    # didn't match the "time" axis length. Previously `zip(times, values)`
    # would silently stop at the shorter of the two with no signal
    # anywhere that it happened, hiding provider regressions/malformed
    # responses behind what still looked like a normal, if slightly
    # short, forecast. The mismatched variable's points for the
    # unmatched tail are simply not included (same truncation as before —
    # this field only adds visibility, it doesn't change what data is
    # kept), so callers (the coordinator) can log/record a diagnostic
    # event instead of the mismatch being invisible.
    array_length_mismatches: tuple[str, ...] = ()
    # v0.1.19: deterministic content hash of the hourly time/value series
    # — see _compute_run_fingerprint and parse_forecast_response's
    # docstring. Used by OpenMeteoCoordinator to detect an unchanged
    # upstream run instead of the always-advancing issued_at.
    run_fingerprint: Optional[str] = None


# Maps Open-Meteo's hourly response keys back to this project's internal
# measurement names (matching bucket_stats.measurement and
# forecast_snapshots.variable).
_VARIABLE_NAME_MAP = {
    "temperature_2m": "temperature",
    # v0.2.0 additions. Names on the right are the project's common
    # vocabulary, defined once in forecast_parameters.PARAMETERS.
    "rain": "rain",
    "showers": "showers",
    "snowfall": "snowfall",
    "snow_depth": "snow_depth",
    "precipitation_probability": "precip_probability",
    "wind_gusts_10m": "wind_gust_speed",
    "wind_direction_10m": "wind_bearing",
    "dew_point_2m": "dew_point",
    "apparent_temperature": "apparent_temperature",
    "cloud_cover": "cloud_coverage",
    "visibility": "visibility",
    "weather_code": "weather_code",
    "uv_index": "uv_index",
    # v0.2.5
    "cape": "cape",
    "convective_inhibition": "convective_inhibition",
    "freezing_level_height": "freezing_level_height",
    "snowfall_height": "snowfall_height",
    "cloud_base": "cloud_base",
    "sunshine_duration": "sunshine_duration",
    "relative_humidity_2m": "humidity",
    # v0.1.2 fix: was surface_pressure (pressure at the source's own grid
    # elevation), which doesn't match what SRF/meteoblue/the local station
    # report — all of those use sea-level-adjusted pressure. Blending
    # surface pressure from some sources with sea-level pressure from
    # others isn't just "a bit off", it's mixing two different physical
    # quantities (they differ by ~12 hPa per 100m of elevation) — the
    # 966.2 hPa reading that prompted this fix matches uncorrected surface
    # pressure at a few hundred meters' elevation almost exactly.
    "pressure_msl": "pressure",
    "precipitation": "precip",
    "wind_speed_10m": "wind_speed",
}


def _compute_run_fingerprint(hourly: dict[str, Any]) -> str:
    """A deterministic identity for "this specific set of hourly values",
    independent of wall-clock poll time — see parse_forecast_response's
    docstring and the v0.1.19 fix note on OpenMeteoCoordinator for why
    this exists. Built only from the "time" axis plus the variables this
    project actually maps (_VARIABLE_NAME_MAP), sorted deterministically,
    so it isn't sensitive to unrelated response fields (e.g. irrelevant
    metadata) or to Open-Meteo's own key ordering.

    v0.1.23: now a thin wrapper around the shared fingerprint.py helper
    (also used by Meteoblue and SRF, see fingerprint.py's module
    docstring) instead of a locally-duplicated hash routine. The actual
    hash inputs/algorithm are unchanged, so existing persisted
    fingerprints from before this refactor remain valid.
    """
    fingerprint_source = {"time": hourly.get("time", [])}
    for open_meteo_key in _VARIABLE_NAME_MAP:
        if open_meteo_key in hourly:
            fingerprint_source[open_meteo_key] = hourly[open_meteo_key]
    return compute_content_fingerprint(fingerprint_source)


def _parse_utc(value: str) -> datetime:
    """Parse a provider timestamp to an aware UTC datetime.

    v0.1.24 fix (P1-25) — see the same helper in clients/meteoblue.py for
    the full reasoning. Short version: fromisoformat(s).replace(tzinfo=utc)
    is correct only for naive input; on aware input it changes the
    instant the value represents.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc)


def parse_forecast_response(payload: dict[str, Any]) -> ParsedForecast:
    """Parse Open-Meteo's JSON response into a flat list of forecast points.

    `issued_at` uses Open-Meteo's own current time as a stand-in for the
    true model run/reference time — Open-Meteo doesn't surface the
    upstream model's exact reference time in this response shape, so the
    lead-time bucket derivation (model_a.derive_lead_time_bucket) treats
    "time of this successful poll" as issued_at. This is a deliberate
    simplification worth revisiting if lead-time bucketing looks wrong
    against real accuracy data once the system is running.

    **v0.1.19 fix (DEF-02)**: because `issued_at` is always
    `datetime.now(timezone.utc)`, the coordinator's old dedup check
    (`parsed.issued_at <= previous_issued`) could essentially never be
    true — every poll looked like a brand-new model run even when the
    upstream data hadn't changed at all, inflating forecast_snapshots and
    learning samples. `run_fingerprint` is a hash of the actual returned
    time/value series (see _compute_run_fingerprint), so the coordinator
    can now detect "nothing changed since last poll" by comparing content,
    not an always-advancing poll timestamp.
    """
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    issued_at = datetime.now(timezone.utc)
    grid_elevation_m = payload.get("elevation")
    run_fingerprint = _compute_run_fingerprint(hourly)

    points: list[ForecastPoint] = []
    mismatches: list[str] = []
    for open_meteo_key, internal_name in _VARIABLE_NAME_MAP.items():
        values = hourly.get(open_meteo_key)
        if values is None:
            continue
        # v0.1.19 fix: `zip(times, values)` alone silently stops at the
        # shorter of the two arrays with no signal anywhere that it
        # happened — a provider regression or a malformed/partial
        # response would just look like a normal, slightly-short
        # forecast. Recording the mismatch here (see
        # ParsedForecast.array_length_mismatches) lets the coordinator
        # log a warning and record a diagnostics event instead. The
        # truncation behavior itself is unchanged (still pairs by index,
        # front-aligned) — this only adds visibility.
        if len(values) != len(times):
            mismatches.append(internal_name)
        for t_str, value in zip(times, values):
            # v0.1.24 fix (P1-25): .replace(tzinfo=...) relabels without
            # converting, so an already-aware timestamp would be shifted
            # by its offset rather than converted. See _parse_utc.
            valid_at = _parse_utc(t_str)
            points.append(
                ForecastPoint(variable=internal_name, valid_at=valid_at, value=value)
            )
    return ParsedForecast(
        issued_at=issued_at,
        points=points,
        grid_elevation_m=grid_elevation_m,
        array_length_mismatches=tuple(mismatches),
        run_fingerprint=run_fingerprint,
    )


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

    api_key is optional — leave it unset for the free tier (the default,
    no account needed). See the module docstring for what a key actually
    changes (rate limits/infrastructure, not model freshness).
    """

    def __init__(self, session: Any, api_key: Optional[str] = None) -> None:
        self._session = session
        self._api_key = api_key

    async def async_fetch_forecast(
        self,
        *,
        source: str,
        latitude: float,
        longitude: float,
        include_optional: bool = True,
    ) -> ParsedForecast:
        import aiohttp

        url = build_forecast_url(
            source=source, latitude=latitude, longitude=longitude,
            api_key=self._api_key, include_optional=include_optional,
        )
        # v0.1.14: none of this client's HTTP calls had an explicit
        # timeout — an outside code review caught this directly against
        # the source (confirmed real: only srf.py had one, from the
        # v0.1.6 fix). Same 30s bound as SRF, for the same reason: a
        # stalled connection should raise something catchable, not hang
        # the coordinator's await indefinitely.
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
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
        import aiohttp

        url = build_elevation_url(latitude=latitude, longitude=longitude, api_key=self._api_key)
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        return parse_elevation_response(payload)
