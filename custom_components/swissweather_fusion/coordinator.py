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

import logging
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .clients.combiprecip import CombiPrecipClient
from .clients.meteoblue import BonusCallTracker, MeteoblueClient, is_scheduled_poll_time
from .clients.meteonomiqs import AnnualCallBudget, MeteonomiqsClient, needs_keepalive_call
from .clients.open_meteo import OpenMeteoClient
from .clients.srf import SrfClient
from .health import SourceHealth
from .const import (
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
                continue
            duration_ms = (time.monotonic() - start) * 1000
            self.health[source].record_success(duration_ms=duration_ms)

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
        self._client = SrfClient(
            async_get_clientsession(hass), consumer_key, consumer_secret
        )
        self.health = SourceHealth()

    async def _async_update_data(self) -> list[Any]:
        start = time.monotonic()
        try:
            points = await self._client.async_fetch_forecast(
                latitude=self._latitude, longitude=self._longitude
            )
        except Exception as err:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000
            kind = self.health.record_error(err, duration_ms=duration_ms)
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
        self, hass: HomeAssistant, db: SwissWeatherDB, latitude: float, longitude: float, api_key: str
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
        # local_dt should use the configured HA timezone; UTC is used here
        # as the simplest correct default — see DEVELOPER.md if local-time
        # scheduling behaves unexpectedly across a DST transition.
        local_dt = datetime.now(timezone.utc)
        if not is_scheduled_poll_time(local_dt=local_dt):
            return None
        # Guard against firing twice within the same scheduled hour, since
        # this coordinator checks every 5 minutes.
        if (
            self._last_scheduled_call_hour is not None
            and self._last_scheduled_call_hour.hour == local_dt.hour
            and self._last_scheduled_call_hour.date() == local_dt.date()
        ):
            return None
        try:
            await self._async_fetch_and_store(trigger_reason="scheduled")
            self._last_scheduled_call_hour = local_dt
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err)
            raise UpdateFailed(f"meteoblue fetch failed: {err}") from err
        return None


class CombiPrecipCoordinator(DataUpdateCoordinator):
    """Continuous 5-min polling — this is a Model B feature source, not a
    Model A blend expert, so results are stored in radar_observations, not
    forecast_snapshots (see storage/db.py and DEVELOPER.md).
    """

    def __init__(
        self, hass: HomeAssistant, db: SwissWeatherDB, latitude: float, longitude: float
    ) -> None:
        from .const import COMBIPRECIP_POLL_INTERVAL

        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_combiprecip",
            update_interval=COMBIPRECIP_POLL_INTERVAL,
        )
        self._db = db
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
            with tempfile.TemporaryDirectory() as tmp_dir:
                values = await self._client.async_fetch_latest_values(tmp_dir=tmp_dir)
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            raise UpdateFailed(f"CombiPrecip fetch failed: {err}") from err
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)

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
        self, hass: HomeAssistant, latitude: float, longitude: float, api_key: str
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_meteonomiqs",
            update_interval=self.CHECK_INTERVAL,
        )
        self._latitude = latitude
        self._longitude = longitude
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
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
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
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
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
