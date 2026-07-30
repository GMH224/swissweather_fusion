"""Coordinators for SwissWeather Fusion.

Home Assistant's DataUpdateCoordinator is built around one polling interval
per instance — this project has several genuinely different cadences
(continuous 5-min CombiPrecip, metadata-driven CH1/CH2/D2, a seasonal
meteoblue schedule, a daily Meteonomiqs keep-alive plus event-triggered
bonus calls), so this file has several coordinator classes rather than one,
each owning the schedule that actually fits its source. See DEVELOPER.md
for the full per-source reasoning; this file is the wiring, not the "why".

All blocking storage calls go through hass.async_add_executor_job() —
storage/db.py is deliberately synchronous and framework-independent (see
its own docstring), so this is the one place that bridges it to HA's event
loop.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .clients.combiprecip import CombiPrecipClient
from .clients.meteoblue import BonusCallTracker, MeteoblueClient, should_fire_scheduled_call
from .clients.meteonomiqs import AnnualCallBudget, MeteonomiqsClient, needs_keepalive_call
from .clients.open_meteo import OpenMeteoClient
from .clients.srf import SrfClient
from .health import SourceHealth
from .const import (
    ALL_FORECAST_SOURCES,
    METEONOMIQS_ANNUAL_CALL_BUDGET,
    METEONOMIQS_FORECAST_CALL_HOUR_LOCAL,
    METEONOMIQS_FORECAST_SEASON_MONTHS,
    METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
    MODEL_B_SCORING_INTERVAL,
    OPEN_METEO_CHECK_INTERVAL,
    SOURCE_CH1,
    SOURCE_CH2,
    SOURCE_ICON_D2,
    SRF_POLL_INTERVAL,
    STATION_POLL_INTERVAL,
    STORM_PREDICTION_UPPER_CROSSING_THRESHOLD,
    UPWIND_BEARING_DEGREES,
    UPWIND_DISTANCES_KM,
    UPWIND_POINT_LABELS,
)
from .models import model_b
from .storage.db import SwissWeatherDB

_LOGGER = logging.getLogger(__name__)


class OpenMeteoCoordinator(DataUpdateCoordinator):
    """Handles CH1, CH2, and ICON-D2 — one coordinator since they share the
    same API and the same "check before fetching" logic. Polls frequently
    (OPEN_METEO_CHECK_INTERVAL) but only actually performs a forecast fetch
    for a given model when that model's own run schedule suggests fresh
    data should be available — see DEVELOPER.md for why a fixed buffer was
    replaced with this check.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        api_key: Optional[str] = None,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_open_meteo",
            update_interval=OPEN_METEO_CHECK_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = OpenMeteoClient(async_get_clientsession(hass), api_key=api_key)
        self._last_issued_at: dict[str, datetime] = {}
        # One health tracker per model, not one for the whole coordinator —
        # CH1 can fail while CH2/D2 succeed (e.g. a MeteoSwiss-side issue
        # specific to one model), and that distinction is exactly what
        # makes per-source diagnostics useful rather than just knowing
        # "Open-Meteo is having a bad day".
        self.health: dict[str, SourceHealth] = {
            SOURCE_CH1: SourceHealth(),
            SOURCE_CH2: SourceHealth(),
            SOURCE_ICON_D2: SourceHealth(),
        }

    async def _async_update_data(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for source in (SOURCE_CH1, SOURCE_CH2, SOURCE_ICON_D2):
            start = time.monotonic()
            try:
                parsed = await self._client.async_fetch_forecast(
                    source=source, latitude=self._latitude, longitude=self._longitude
                )
            except Exception as err:  # noqa: BLE001
                duration_ms = (time.monotonic() - start) * 1000
                kind = self.health[source].record_error(err, duration_ms=duration_ms)
                _LOGGER.warning(
                    "Open-Meteo fetch failed for %s (%s error): %s", source, kind, err
                )
                if self._diagnostics is not None:
                    self._diagnostics.record(
                        source=source, event_type="poll_failure", detail=str(err)
                    )
                continue
            duration_ms = (time.monotonic() - start) * 1000
            self.health[source].record_success(duration_ms=duration_ms)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source=source, event_type="poll_success",
                    detail=f"{len(parsed.points)} points",
                )

            previous_issued = self._last_issued_at.get(source)
            if previous_issued is not None and parsed.issued_at <= previous_issued:
                # No new run since last successful fetch — nothing to store.
                # (Simplification note: this project treats "poll time" as
                # issued_at rather than the upstream model's true reference
                # time, per open_meteo.py's own docstring — this comparison
                # is therefore approximate, not a precise run-identity
                # check. Revisit if lead-time bucketing looks wrong once
                # real accuracy data exists.)
                continue
            self._last_issued_at[source] = parsed.issued_at

            rows = [
                (
                    source,
                    parsed.issued_at.isoformat(),
                    point.valid_at.isoformat(),
                    point.variable,
                    point.value,
                    "scheduled",
                )
                for point in parsed.points
            ]
            await self.hass.async_add_executor_job(
                self._db.insert_forecast_snapshots_bulk, rows
            )
            results[source] = parsed
        return results


class SrfCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        consumer_key: str,
        consumer_secret: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_srf",
            update_interval=SRF_POLL_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = SrfClient(
            async_get_clientsession(hass),
            consumer_key,
            consumer_secret,
            diagnostics=diagnostics,
            latitude=latitude,
            longitude=longitude,
        )
        self.health = SourceHealth()

    async def _async_update_data(self) -> list[Any]:
        start = time.monotonic()
        if self._diagnostics is not None:
            self._diagnostics.record(source="srf", event_type="poll_start", detail="polling")
        try:
            # v0.1.6: an outer backstop timeout, in addition to the
            # per-request timeouts added in the client itself — belt and
            # suspenders against a hang happening somewhere other than
            # the three explicit HTTP calls (e.g. during the token cache
            # check, or a retry loop), given the whole point is to never
            # again see a coordinator silently stop updating with no
            # error recorded.
            async with asyncio.timeout(60):
                points = await self._client.async_fetch_forecast(
                    latitude=self._latitude, longitude=self._longitude
                )
        except Exception as err:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000
            kind = self.health.record_error(err, duration_ms=duration_ms)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="srf", event_type="poll_failure",
                    detail=f"{kind} error: {err}",
                )
            if kind == "auth":
                # This is the "API key expired" case specifically — surface
                # it distinctly rather than let it look like an ordinary
                # transient fetch failure, since retrying won't fix it.
                _LOGGER.error(
                    "SRF authentication failed — credentials likely need "
                    "to be re-entered (reauth flow): %s",
                    err,
                )
            raise UpdateFailed(f"SRF fetch failed ({kind} error): {err}") from err
        duration_ms = (time.monotonic() - start) * 1000
        self.health.record_success(duration_ms=duration_ms)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="srf", event_type="poll_success",
                detail=f"{len(points)} points", extra={"point_count": len(points)},
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            ("srf", now_iso, p.valid_at.isoformat(), p.variable, p.value, "scheduled")
            for p in points
        ]
        await self.hass.async_add_executor_job(self._db.insert_forecast_snapshots_bulk, rows)
        return points


class MeteoblueCoordinator(DataUpdateCoordinator):
    """Seasonal schedule (Mar-Oct vs Nov-Feb, both 3 calls/day) plus the
    one-bonus-call-per-storm-scenario allowance from the cross-model
    trigger. Polls every few minutes just to *check* whether it's a
    scheduled slot or a bonus call is due — most checks do nothing, which
    is the intended, credit-neutral behavior (see DEVELOPER.md).
    """

    CHECK_INTERVAL = timedelta(minutes=5)

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        api_key: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_meteoblue",
            update_interval=self.CHECK_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = MeteoblueClient(async_get_clientsession(hass), api_key)
        self._bonus_tracker = BonusCallTracker()
        self._last_scheduled_call_hour: Optional[datetime] = None
        self.health = SourceHealth()

    async def async_request_bonus_call(self) -> bool:
        """Called by the cross-model trigger (see ModelBCoordinator). Returns
        True if a bonus call was actually made, False if the daily
        allowance was already used.
        """
        today = datetime.now(timezone.utc).date()
        if not self._bonus_tracker.can_use_bonus_call(today=today):
            return False
        await self._async_fetch_and_store(trigger_reason="storm_trigger")
        self._bonus_tracker.record_bonus_call_used(today=today)
        return True

    async def _async_fetch_and_store(self, *, trigger_reason: str) -> None:
        start = time.monotonic()
        parsed = await self._client.async_fetch_forecast(
            latitude=self._latitude, longitude=self._longitude
        )
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteoblue", event_type="poll_success",
                detail=f"{len(parsed.points)} points ({trigger_reason})",
            )
        rows = [
            (
                "meteoblue",
                parsed.issued_at.isoformat(),
                p.valid_at.isoformat(),
                p.variable,
                p.value,
                trigger_reason,
            )
            for p in parsed.points
        ]
        await self.hass.async_add_executor_job(self._db.insert_forecast_snapshots_bulk, rows)

    async def _async_update_data(self) -> None:
        # v0.1.6 fix: this used hardcoded UTC ("local_dt" was a misnomer —
        # it wasn't local at all). In summer (CEST = UTC+2), that meant
        # meteoblue was actually polling at 14:00/18:00/22:00 local time
        # instead of the intended 12:00/16:00/20:00 — a real 2-hour
        # scheduling offset, caught from a production log showing
        # meteoblue hadn't polled yet at a time it should have. Now uses
        # HA's own configured-timezone "now" helper, the standard pattern
        # for this rather than assuming UTC equals local time.
        local_dt = dt_util.now()
        if not should_fire_scheduled_call(
            local_dt=local_dt, last_scheduled_call_hour=self._last_scheduled_call_hour
        ):
            return None
        try:
            await self._async_fetch_and_store(trigger_reason="scheduled")
            self._last_scheduled_call_hour = local_dt
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteoblue", event_type="poll_failure", detail=str(err)
                )
            raise UpdateFailed(f"meteoblue fetch failed: {err}") from err
        return None


class CombiPrecipCoordinator(DataUpdateCoordinator):
    """Continuous 5-min polling — this is a Model B feature source, not a
    Model A blend expert, so results are stored in radar_observations, not
    forecast_snapshots (see storage/db.py and DEVELOPER.md).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        *,
        diagnostics: Any = None,
    ) -> None:
        from .const import COMBIPRECIP_POLL_INTERVAL

        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_combiprecip",
            update_interval=COMBIPRECIP_POLL_INTERVAL,
        )
        self._db = db
        self._diagnostics = diagnostics
        self._client = CombiPrecipClient(
            async_get_clientsession(hass),
            latitude,
            longitude,
            bearing_degrees=UPWIND_BEARING_DEGREES,
            distances_km=UPWIND_DISTANCES_KM,
            labels=UPWIND_POINT_LABELS,
        )
        self.health = SourceHealth()

    async def _async_update_data(self) -> list[Any]:
        start = time.monotonic()
        try:
            # v0.1.7 fix: this used to do `with tempfile.TemporaryDirectory()`
            # directly here, with the actual file write happening inside
            # the awaited client call — HA's own loop-blocking detector
            # caught both the file write and the temp-dir cleanup
            # happening synchronously on the event loop. Now: async
            # download only, then the entire blocking sequence (temp dir,
            # write, h5py parse, cleanup) runs via one executor job.
            data = await self._client.async_fetch_latest_bytes()
            values = await self.hass.async_add_executor_job(
                self._client.write_temp_and_extract, data
            )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="combiprecip", event_type="poll_failure", detail=str(err)
                )
            raise UpdateFailed(f"CombiPrecip fetch failed: {err}") from err
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="combiprecip", event_type="poll_success",
                detail=f"{len(values)} points extracted",
            )

        # Only the "local" point goes into radar_observations (const.py
        # schema — one row per scan for the configured location); the
        # upwind points are Model B-only features and are passed straight
        # through to ModelBCoordinator rather than persisted separately,
        # since their value is in the live signal, not historical trend.
        local = next((v for v in values if v.label == "local"), None)
        if local is not None:
            await self.hass.async_add_executor_job(
                self._db.insert_radar_observation,
                local.valid_at.isoformat(),
                local.precip_rate_mmh,
                None,
            )
        return values


class StationCoordinator(DataUpdateCoordinator):
    """Reads the configured local sensor entities and logs them — this is
    the ground truth everything else gets corrected against.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        temp_entity: str,
        humidity_entity: str,
        pressure_entity: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_station",
            update_interval=STATION_POLL_INTERVAL,
        )
        self._db = db
        self._temp_entity = temp_entity
        self._humidity_entity = humidity_entity
        self._pressure_entity = pressure_entity
        self._diagnostics = diagnostics

    def _read_float_state(self, entity_id: str) -> Optional[float]:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    async def _async_update_data(self) -> dict[str, Optional[float]]:
        temperature = self._read_float_state(self._temp_entity)
        humidity = self._read_float_state(self._humidity_entity)
        pressure = self._read_float_state(self._pressure_entity)
        now_iso = datetime.now(timezone.utc).isoformat()
        await self.hass.async_add_executor_job(
            self._db.insert_station_observation, now_iso, temperature, humidity, pressure
        )
        return {"temperature": temperature, "humidity": humidity, "pressure": pressure}


class MeteonomiqsCoordinator(DataUpdateCoordinator):
    """Daily keep-alive (unconditional, prevents the ~30-day inactivity
    revocation) plus event-triggered bonus calls from the cross-model
    trigger. See DEVELOPER.md ("Why Meteonomiqs needs a daily heartbeat").

    During Mar-Oct (the same storm-season window as meteoblue's schedule),
    the daily keep-alive call uses /forecast/hourly (pressure,
    precipitation) at local noon instead of /nowcast — this is NOT
    an additional call, either satisfies the same keep-alive requirement,
    so the annual budget is unaffected; it's just a more useful payload on
    the day it's needed. Outside that window, or if noon has already
    passed without a call happening yet that day, the plain nowcast
    keep-alive is used as the fallback — the priority is never missing a
    day, not always hitting noon exactly.
    """

    CHECK_INTERVAL = timedelta(hours=6)

    def __init__(
        self,
        hass: HomeAssistant,
        latitude: float,
        longitude: float,
        api_key: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_meteonomiqs",
            update_interval=self.CHECK_INTERVAL,
        )
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = MeteonomiqsClient(async_get_clientsession(hass), api_key)
        self._budget = AnnualCallBudget(METEONOMIQS_ANNUAL_CALL_BUDGET)
        self._last_successful_call_date: Optional[date] = None
        self.last_nowcast: Optional[Any] = None
        self.last_hourly_forecast: Optional[list[Any]] = None
        self.health = SourceHealth()

    async def async_request_bonus_call(self) -> bool:
        """Cross-model trigger bonus call — always nowcast (the fast,
        radar-based signal), regardless of season, since this is about an
        immediate storm check, not the daily outlook the noon call gives.
        """
        today = datetime.now(timezone.utc).date()
        if not self._budget.can_call(today=today):
            _LOGGER.warning(
                "Meteonomiqs annual budget exhausted; skipping bonus call"
            )
            return False
        await self._async_fetch_nowcast(today=today)
        return True

    async def _async_fetch_nowcast(self, *, today: date) -> None:
        start = time.monotonic()
        try:
            self.last_nowcast = await self._client.async_fetch_nowcast(
                latitude=self._latitude, longitude=self._longitude
            )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteonomiqs", event_type="poll_failure",
                    detail=f"nowcast: {err}",
                )
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteonomiqs", event_type="poll_success", detail="nowcast",
            )
        self._budget.record_call(today=today)
        self._last_successful_call_date = today

    async def _async_fetch_hourly_forecast(self, *, today: date) -> None:
        start = time.monotonic()
        try:
            self.last_hourly_forecast = await self._client.async_fetch_hourly_forecast(
                latitude=self._latitude, longitude=self._longitude
            )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteonomiqs", event_type="poll_failure",
                    detail=f"hourly_forecast: {err}",
                )
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteonomiqs", event_type="poll_success", detail="hourly_forecast",
            )
        self._budget.record_call(today=today)
        self._last_successful_call_date = today

    async def _async_update_data(self) -> None:
        local_now = datetime.now(timezone.utc)
        today = local_now.date()

        if not needs_keepalive_call(
            last_successful_call_date=self._last_successful_call_date,
            today=today,
            max_days_between_calls=METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
        ):
            return None

        in_forecast_season = local_now.month in METEONOMIQS_FORECAST_SEASON_MONTHS
        already_called_today = self._last_successful_call_date == today
        past_noon = local_now.hour >= METEONOMIQS_FORECAST_CALL_HOUR_LOCAL

        try:
            if in_forecast_season and not already_called_today and past_noon:
                await self._async_fetch_hourly_forecast(today=today)
            else:
                # Either outside the summer window, or summer but noon
                # hasn't arrived yet today (this coordinator checks every
                # 6h, so it may run before noon) — nowcast keeps the key
                # alive in the meantime without pre-empting the richer
                # noon call. If needs_keepalive_call is still true days
                # later, this branch also acts as the ultimate fallback so
                # a day is never silently missed.
                await self._async_fetch_nowcast(today=today)
        except Exception as err:  # noqa: BLE001
            # A failed keep-alive is worth logging loudly — losing API
            # access entirely from inactivity is worse than a routine
            # data-fetch error elsewhere in this system.
            _LOGGER.error("Meteonomiqs keep-alive call failed: %s", err)
        return None


class ModelABlendCoordinator(DataUpdateCoordinator):
    """Computes Model A's blended values — both "now" and a genuine
    multi-hour forecast — in one batched executor job per refresh cycle.

    **v0.1.5 fix**: this replaces logic that used to live directly in
    weather.py's entity properties, which queried the database
    synchronously on the event loop — every other part of this project
    routes DB access through an executor job except that one. Moving the
    computation here, run once per refresh rather than once per property
    read, fixes that and is also what makes a real hourly forecast
    practical: computing 48 hours × 5 measurements as 240 individual
    blocking property-reads would have been much worse than the same
    work batched into one executor job.

    Also the home of wind_speed exposure, which was already flowing
    through Model A's blend (every client already reports it) but was
    never actually surfaced on the weather entity — data that existed
    with nothing reading it.
    """

    MEASUREMENTS = ("temperature", "humidity", "pressure", "precip", "wind_speed")
    # 7 days rather than 2 — needed for meaningful daily/twice-daily
    # coverage (added alongside precipitation-in-mm for those views), and
    # matches roughly CH2/meteoblue's own horizons. Sources with shorter
    # horizons (CH1's ~33-45h) simply taper off within this window rather
    # than every hour having full coverage from every source.
    FORECAST_HOURS_AHEAD = 168

    def __init__(self, hass: HomeAssistant, db: SwissWeatherDB) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_blend",
            update_interval=timedelta(minutes=10),
        )
        self._db = db

    async def _async_update_data(self) -> dict[str, Any]:
        return await self.hass.async_add_executor_job(self._compute_blend)

    def _blend_at(self, measurement: str, target_hour: datetime) -> Optional[float]:
        """Synchronous — only ever called from inside _compute_blend,
        which itself only ever runs inside the executor job above. Same
        logic that used to live in weather.py, relocated rather than
        rewritten, since the logic itself was already correct — only
        where it ran was the problem.
        """
        from .models import model_a
        from .storage.db import BucketKey

        hour_of_day = target_hour.hour
        season = model_a.derive_season(target_hour)
        target_iso = target_hour.replace(minute=0, second=0, microsecond=0).isoformat()

        contributions: list[model_a.SourceContribution] = []
        for source in ALL_FORECAST_SOURCES:
            rows = self._db.get_forecast_values_for_valid_at(
                source=source, variable=measurement, valid_at=target_iso
            )
            if not rows:
                continue
            latest_row = rows[0]  # already ordered by issued_at DESC
            issued_at = datetime.fromisoformat(latest_row["issued_at"])
            lead_time_bucket = model_a.derive_lead_time_bucket(issued_at, target_hour)
            bucket = self._db.get_bucket_stats(
                BucketKey(
                    hour_of_day=hour_of_day,
                    season=season,
                    lead_time_bucket=lead_time_bucket,
                    source=source,
                    measurement=measurement,
                )
            )
            if bucket is None:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=latest_row["value"],
                        ema_bias=0.0, ema_weight=1.0, sample_count=0,
                    )
                )
            else:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=latest_row["value"],
                        ema_bias=bucket.ema_bias, ema_weight=bucket.ema_weight,
                        sample_count=bucket.sample_count,
                    )
                )
        return model_a.blend(contributions)

    def _compute_blend(self) -> dict[str, Any]:
        from .models import model_a

        now = model_a.utcnow().replace(minute=0, second=0, microsecond=0)

        current = {m: self._blend_at(m, now) for m in self.MEASUREMENTS}

        hourly_forecast: list[dict[str, Any]] = []
        for i in range(self.FORECAST_HOURS_AHEAD):
            target = now + timedelta(hours=i)
            temperature = self._blend_at("temperature", target)
            humidity = self._blend_at("humidity", target)
            pressure = self._blend_at("pressure", target)
            precip = self._blend_at("precip", target)
            wind_speed = self._blend_at("wind_speed", target)
            # Skip hours with literally nothing from any source — no
            # point showing an all-None row, and sources with shorter
            # horizons (CH1's ~33-45h) will naturally taper off within
            # this 48h window rather than every hour having full coverage.
            if all(v is None for v in (temperature, humidity, pressure, precip, wind_speed)):
                continue
            hourly_forecast.append(
                {
                    "datetime": target.isoformat(),
                    "native_temperature": temperature,
                    "humidity": humidity,
                    "native_pressure": pressure,
                    "native_precipitation": precip,
                    "native_wind_speed": wind_speed,
                    "condition": "rainy" if (precip or 0) > 0.1 else "sunny",
                }
            )

        return {
            "current": current,
            "hourly_forecast": hourly_forecast,
            # Built from the same hourly data above — no extra DB access,
            # just reshaped, per the request to have precipitation (mm)
            # available at daily and twice-daily granularity too, not just
            # hourly.
            "daily_forecast": model_a.aggregate_daily_forecast(hourly_forecast),
            "twice_daily_forecast": model_a.aggregate_twice_daily_forecast(hourly_forecast),
        }


class ModelALearningCoordinator(DataUpdateCoordinator):
    """Model A's actual learning step — periodically compares past
    forecasts against what the station actually measured, and folds the
    result into bucket_stats via the EMA.

    **v0.1.7: closes a real gap found during review.**
    `models.model_a.update_bucket_ema` and `storage.db.upsert_bucket_stats`
    existed and were unit-tested in isolation since early in this
    project, but nothing in production code ever actually called them.
    Without this coordinator, `bucket_stats` would stay empty forever —
    not just during a cold-start window — meaning Model A's blend was
    only ever an unweighted average of raw forecasts, never applying the
    learned bias correction that's the actual point of the project.

    Runs every 20 minutes (bias correction is a slow-moving statistic;
    this doesn't need to be frequent) and does the entire batch — finding
    due forecast rows, fetching candidate station readings, matching,
    and updating every bucket — inside one executor job, the same
    pattern as ModelABlendCoordinator.
    """

    RECONCILIATION_INTERVAL = timedelta(minutes=20)
    # Only measurements the local station can actually confirm — precip
    # and wind_speed have no ground truth yet (station has no rain/wind
    # sensors), so forecasts for those are stored but never reconciled.
    RECONCILIATION_MEASUREMENTS = ("temperature", "humidity", "pressure")
    # How far back to look on the very first run ever (no watermark yet).
    # 14 days comfortably covers every source's forecast horizon
    # (meteoblue's ~7-10 days is the longest) without trying to reconcile
    # an unbounded amount of history in one go.
    INITIAL_LOOKBACK = timedelta(days=14)

    def __init__(self, hass: HomeAssistant, db: SwissWeatherDB) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_learning",
            update_interval=self.RECONCILIATION_INTERVAL,
        )
        self._db = db
        self.last_reconciled_count: int = 0

    async def _async_update_data(self) -> Optional[datetime]:
        return await self.hass.async_add_executor_job(self._reconcile)

    def _reconcile(self) -> datetime:
        """Synchronous — only ever called via the executor job above."""
        from .models import model_a
        from .storage.db import BucketKey

        now = model_a.utcnow()
        watermark_str = self._db.get_reconciliation_watermark()
        since = (
            datetime.fromisoformat(watermark_str)
            if watermark_str is not None
            else now - self.INITIAL_LOOKBACK
        )
        since_iso = since.isoformat()
        until_iso = now.isoformat()

        rows_to_reconcile = self._db.get_forecast_snapshots_to_reconcile(
            since_ts=since_iso,
            until_ts=until_iso,
            measurements=self.RECONCILIATION_MEASUREMENTS,
        )
        if not rows_to_reconcile:
            self._db.set_reconciliation_watermark(until_iso)
            self.last_reconciled_count = 0
            return now

        # One station-observation query for the whole batch (padded by
        # the matching tolerance on each side), not one query per forecast
        # row — matches the batching approach already used elsewhere in
        # this project (e.g. ModelABlendCoordinator).
        tolerance = timedelta(minutes=model_a.RECONCILIATION_TOLERANCE_MINUTES)
        station_rows = self._db.get_station_observations_between(
            (since - tolerance).isoformat(), (now + tolerance).isoformat()
        )
        candidates_by_measurement: dict[str, list[tuple[datetime, Any]]] = {
            "temperature": [],
            "humidity": [],
            "pressure": [],
        }
        for row in station_rows:
            ts = datetime.fromisoformat(row["ts"])
            candidates_by_measurement["temperature"].append((ts, row["temperature"]))
            candidates_by_measurement["humidity"].append((ts, row["humidity"]))
            candidates_by_measurement["pressure"].append((ts, row["pressure"]))

        reconciled_count = 0
        for fs_row in rows_to_reconcile:
            if fs_row["value"] is None:
                continue
            measurement = fs_row["variable"]
            valid_at = datetime.fromisoformat(fs_row["valid_at"])
            issued_at = datetime.fromisoformat(fs_row["issued_at"])

            actual_value = model_a.find_nearest_observation(
                target=valid_at, candidates=candidates_by_measurement[measurement]
            )
            if actual_value is None:
                continue

            key = BucketKey(
                hour_of_day=valid_at.hour,
                season=model_a.derive_season(valid_at),
                lead_time_bucket=model_a.derive_lead_time_bucket(issued_at, valid_at),
                source=fs_row["source"],
                measurement=measurement,
            )
            existing = self._db.get_bucket_stats(key)
            if existing is None:
                previous_bias, previous_abs_error, previous_sample_count = 0.0, 0.0, 0
            else:
                previous_bias = existing.ema_bias
                previous_abs_error = existing.ema_abs_error
                previous_sample_count = existing.sample_count

            result = model_a.update_bucket_ema(
                previous_bias=previous_bias,
                previous_abs_error=previous_abs_error,
                previous_sample_count=previous_sample_count,
                forecast_value=fs_row["value"],
                actual_value=actual_value,
                lead_time_bucket=key.lead_time_bucket,
            )
            self._db.upsert_bucket_stats(
                key,
                ema_bias=result.ema_bias,
                ema_abs_error=result.ema_abs_error,
                ema_weight=result.ema_weight,
                sample_count=result.sample_count,
                last_updated=now.isoformat(),
            )
            reconciled_count += 1

        self._db.set_reconciliation_watermark(until_iso)
        self.last_reconciled_count = reconciled_count
        _LOGGER.debug(
            "Model A learning: reconciled %d of %d due forecast snapshots",
            reconciled_count,
            len(rows_to_reconcile),
        )
        return now


class ModelBCoordinator(DataUpdateCoordinator):
    """Scores Model B every 5-10 minutes off the local station stream plus
    the live CombiPrecip radar points, and fires the cross-model trigger
    (INCA originally, now: force a fresh meteoblue/Meteonomiqs read) on an
    upward probability crossing. See models/model_b.py and DEVELOPER.md.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        station_coordinator: StationCoordinator,
        combiprecip_coordinator: CombiPrecipCoordinator,
        meteoblue_coordinator: MeteoblueCoordinator,
        meteonomiqs_coordinator: MeteonomiqsCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_model_b",
            update_interval=MODEL_B_SCORING_INTERVAL,
        )
        self._db = db
        self._station_coordinator = station_coordinator
        self._combiprecip_coordinator = combiprecip_coordinator
        self._meteoblue_coordinator = meteoblue_coordinator
        self._meteonomiqs_coordinator = meteonomiqs_coordinator
        self._previous_probability = 0.0
        self.current_probability = 0.0

    async def _async_update_data(self) -> float:
        rows = await self.hass.async_add_executor_job(
            self._db.get_station_observations_since,
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )
        samples = [
            model_b.StationSample(
                ts_epoch_seconds=datetime.fromisoformat(r["ts"]).timestamp(),
                temperature=r["temperature"],
                humidity=r["humidity"],
                pressure=r["pressure"],
            )
            for r in rows
        ]

        radar_values = self._combiprecip_coordinator.data or []
        radar_points = tuple(
            model_b.RadarPointReading(label=v.label, precip_rate_mmh=v.precip_rate_mmh)
            for v in radar_values
        )

        features = model_b.compute_tendency_features(
            samples=samples,
            now_epoch_seconds=time.time(),
            radar_points=radar_points,
        )
        probability = model_b.score_v0_graduated(features)

        await self.hass.async_add_executor_job(
            self._db.insert_storm_prediction,
            datetime.now(timezone.utc).isoformat(),
            probability,
            {
                "delta_pressure_30min": features.delta_pressure_30min,
                "delta_humidity_30min": features.delta_humidity_30min,
                "radar_points": {p.label: p.precip_rate_mmh for p in radar_points},
            },
        )

        decision = model_b.evaluate_cross_model_trigger(
            previous_probability=self._previous_probability,
            current_probability=probability,
            threshold=STORM_PREDICTION_UPPER_CROSSING_THRESHOLD,
        )
        if decision.should_trigger:
            _LOGGER.info(
                "Model B cross-model trigger fired (probability %.2f) — "
                "requesting bonus meteoblue + Meteonomiqs calls",
                probability,
            )
            await self._meteoblue_coordinator.async_request_bonus_call()
            got_meteonomiqs = await self._meteonomiqs_coordinator.async_request_bonus_call()
            if got_meteonomiqs and self._meteonomiqs_coordinator.last_nowcast:
                risk_values = [
                    item.precip_risk_value
                    for item in self._meteonomiqs_coordinator.last_nowcast.items
                    if item.precip_risk_value is not None
                ]
                if risk_values:
                    probability = model_b.refine_with_meteonomiqs(
                        base_probability=probability,
                        meteonomiqs_risk_value=max(risk_values),
                    )

        self._previous_probability = probability
        self.current_probability = probability
        return probability
