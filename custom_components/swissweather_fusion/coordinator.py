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
    METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT,
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
        actual_elevation_m: Optional[float] = None,
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
        # v0.1.15 fix: apply_lapse_rate_precorrection existed and was
        # tested since early in this project, but nothing ever called it
        # — confirmed by an outside code review as unused configuration.
        # Applied here specifically, to Open-Meteo's temperature values,
        # since Open-Meteo's response confirmed includes the model grid
        # cell's own elevation as a top-level field — the one piece of
        # data the correction actually needs and the only source this
        # project has confirmed elevation data for. Not applied to
        # SRF/meteoblue/Meteonomiqs, since their responses' own grid/
        # station elevation isn't currently captured.
        self._actual_elevation_m = actual_elevation_m
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
        from .models import model_a

        results: dict[str, Any] = {}
        for source in (SOURCE_CH1, SOURCE_CH2, SOURCE_ICON_D2):
            start = time.monotonic()
            try:
                # v0.1.14: an outer backstop timeout, per source — same
                # defense-in-depth reasoning as SRF's existing one (v0.1.6),
                # applied here after an outside code review confirmed most
                # coordinators had no equivalent protection at all.
                async with asyncio.timeout(60):
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

            # v0.1.15 fix: wires apply_lapse_rate_precorrection into the
            # actual blend path — see __init__'s comment for the full
            # story. Only applied to temperature, and only when both the
            # grid's own elevation (from this response) and the
            # configured actual elevation are known; otherwise values
            # pass through unchanged, same as before this fix existed.
            grid_elevation = parsed.grid_elevation_m
            apply_correction = (
                grid_elevation is not None and self._actual_elevation_m is not None
            )
            rows = []
            for point in parsed.points:
                value = point.value
                if apply_correction and point.variable == "temperature" and value is not None:
                    value = model_a.apply_lapse_rate_precorrection(
                        raw_temperature=value,
                        source_grid_elevation_m=grid_elevation,
                        actual_elevation_m=self._actual_elevation_m,
                    )
                rows.append(
                    (
                        source,
                        parsed.issued_at.isoformat(),
                        point.valid_at.isoformat(),
                        point.variable,
                        value,
                        "scheduled",
                    )
                )
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
        used_fallback = False
        try:
            # v0.1.6: an outer backstop timeout, in addition to the
            # per-request timeouts added in the client itself — belt and
            # suspenders against a hang happening somewhere other than
            # the three explicit HTTP calls (e.g. during the token cache
            # check, or a retry loop), given the whole point is to never
            # again see a coordinator silently stop updating with no
            # error recorded.
            async with asyncio.timeout(60):
                try:
                    # v0.1.18: the confirmed-working v2/forecastpoint
                    # endpoint is now the primary fetch — genuine hourly
                    # data, not just daily. Falls back to the old
                    # daily-only endpoint below if this fails for any
                    # reason; better to have some data than none, and SRF's
                    # API has surprised this project enough times that a
                    # graceful fallback is worth keeping rather than
                    # removing the old code path entirely.
                    points = await self._client.async_fetch_forecastpoint(
                        latitude=self._latitude, longitude=self._longitude
                    )
                except Exception as primary_err:  # noqa: BLE001
                    _LOGGER.warning(
                        "SRF v2/forecastpoint fetch failed, falling back to "
                        "the daily-only endpoint: %s",
                        primary_err,
                    )
                    used_fallback = True
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
                detail=f"{len(points)} points" + (" (fallback endpoint)" if used_fallback else ""),
                extra={"point_count": len(points), "used_fallback": used_fallback},
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
        # v0.1.15 fix: reserves the slot atomically before the fetch, not
        # after — the original race window was specifically the await
        # below (the HTTP call), where a second concurrent trigger could
        # pass the same can_use_bonus_call check before either recorded
        # usage. This does mean a failed fetch still counts against the
        # daily allowance rather than being refunded — a deliberate,
        # simpler trade-off given how rare and already-protected (by the
        # calling coordinator's own overlap protection) this path is.
        if not self._bonus_tracker.try_use_bonus_call(today=today):
            return False
        await self._async_fetch_and_store(trigger_reason="storm_trigger")
        return True

    async def _async_fetch_and_store(self, *, trigger_reason: str) -> None:
        start = time.monotonic()
        # v0.1.14: same defense-in-depth backstop added to every other
        # coordinator — see OpenMeteoCoordinator's comment for the reason.
        async with asyncio.timeout(60):
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
            #
            # v0.1.14: outer backstop timeout, longer than most other
            # coordinators' (120s vs 60s) since this is the one client
            # downloading an actual binary file plus running an
            # executor-wrapped HDF5 parse, not just a small JSON response.
            async with asyncio.timeout(120):
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
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has.
        async with asyncio.timeout(30):
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
        # v0.1.17 fix: previously had no per-day cap on bonus calls at
        # all — see async_request_bonus_call and const.py's
        # METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT for the full story.
        self._bonus_tracker = BonusCallTracker(
            max_calls_per_day=METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT
        )
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
        # v0.1.17 fix: this used to only check the overall annual budget
        # (self._budget.can_call), with no per-day cap at all — confirmed
        # in production allowing it to fire every 5 minutes (whatever the
        # underlying reason the trigger kept re-evaluating true), unlike
        # meteoblue's equivalent path which was always protected by
        # BonusCallTracker. This check is deliberately placed FIRST and
        # short-circuits before the annual-budget check — a repeatedly
        # firing trigger should be stopped by the daily cap long before
        # it's even a question of remaining annual budget.
        if not self._bonus_tracker.can_use_bonus_call(today=today):
            return False
        # v0.1.15: AnnualCallBudget.try_call() exists (added alongside
        # BonusCallTracker.try_use_bonus_call() for the same TOCTOU fix),
        # but is deliberately NOT used here — _async_fetch_nowcast below
        # already calls self._budget.record_call() internally on success
        # (shared with the regular daily-keepalive path), so reserving
        # via try_call() here too would double-count every bonus call.
        # This check remains a plain pre-filter, not a full atomic
        # reservation — an acceptable, low-risk gap given how rarely this
        # path fires and the overlap protection already provided by
        # ModelBCoordinator being a single, non-reentrant coordinator.
        if not self._budget.can_call(today=today):
            _LOGGER.warning(
                "Meteonomiqs annual budget exhausted; skipping bonus call"
            )
            return False
        self._bonus_tracker.record_bonus_call_used(today=today)
        await self._async_fetch_nowcast(today=today)
        return True

    async def _async_fetch_nowcast(self, *, today: date) -> None:
        start = time.monotonic()
        try:
            # v0.1.14: same defense-in-depth backstop as every other
            # coordinator now has.
            async with asyncio.timeout(60):
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
            async with asyncio.timeout(60):
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
        # v0.1.15 fix: "local_now" used to be datetime.now(timezone.utc) —
        # the same class of bug already fixed for meteoblue in v0.1.6, but
        # never checked here too, caught by an outside code review. In
        # Switzerland (CEST = UTC+2) this shifted the noon cutoff by 2
        # hours. Now uses HA's own configured-timezone helper, matching
        # the meteoblue fix.
        local_now = dt_util.now()
        today = local_now.date()

        # v0.1.15 fix: this used to be gated behind needs_keepalive_call()
        # (only True once ~30 days had passed since the last successful
        # call) wrapping the entire method below — meaning the "daily"
        # seasonal forecast call this project's own design docs describe
        # never actually fired more than once every 30 days, contradicting
        # the documented intent. Confirmed by an outside code review
        # against this exact code. The daily-once-per-day check below is
        # now the actual gate; the 30-day threshold is only a loud warning
        # if the daily logic somehow hasn't produced a successful call in
        # that long — a real problem worth surfacing, not something that
        # should have been gating every attempt in the first place.
        if needs_keepalive_call(
            last_successful_call_date=self._last_successful_call_date,
            today=today,
            max_days_between_calls=METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
        ):
            _LOGGER.warning(
                "Meteonomiqs hasn't had a successful call in %s+ days — "
                "the daily keepalive logic may not be working, and the "
                "API key risks revocation from inactivity.",
                METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
            )

        if self._last_successful_call_date == today:
            return None

        in_forecast_season = local_now.month in METEONOMIQS_FORECAST_SEASON_MONTHS
        past_noon = local_now.hour >= METEONOMIQS_FORECAST_CALL_HOUR_LOCAL

        try:
            if in_forecast_season and past_noon:
                await self._async_fetch_hourly_forecast(today=today)
            elif not in_forecast_season:
                # Nov-Feb: nowcast is a pure keepalive with no time-of-day
                # data-quality reason to wait, unlike the seasonal forecast
                # call above — fire as soon as a new day starts.
                await self._async_fetch_nowcast(today=today)
            # else: in forecast season but before local noon today — this
            # coordinator checks every 6h and may run before noon; simply
            # wait for a later check today (guaranteed within the same day,
            # since a 6h interval always has at least one check past noon).
        except Exception as err:  # noqa: BLE001
            # A failed keep-alive is worth logging loudly — losing API
            # access entirely from inactivity is worse than a routine
            # data-fetch error elsewhere in this system. Deliberately not
            # re-raised: the next scheduled check today (or tomorrow) will
            # retry via the same daily-gate logic above.
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

    **v0.1.13 fix**: moving the work into one executor job wasn't enough
    on its own — the job itself was still doing up to ~8,400 individual
    sequential database round trips every single cycle (168 hours × 5
    measurements × up to 5 sources, each needing its own
    get_forecast_values_for_valid_at *and* get_bucket_stats call). Found
    while investigating a reported multi-hour freeze affecting every
    coordinator simultaneously — whether or not this was the full
    explanation, an executor job potentially taking a very long time
    every 10 minutes is a real problem on its own, tying up a thread far
    longer than it needs to. Now: two bulk queries
    (get_forecast_snapshots_in_window, get_all_bucket_stats) fetch
    everything needed for the whole 168-hour computation up front, and
    _blend_at becomes a pure in-memory lookup with no database access at
    all — the same math, just no longer paying for a round trip per
    individual lookup.

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
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has. Generous (120s) since this job now does
        # two bulk queries plus in-memory processing of a potentially
        # large result set (v0.1.13's fix), still bounded but with
        # headroom.
        async with asyncio.timeout(120):
            return await self.hass.async_add_executor_job(self._compute_blend)

    def _blend_at(
        self,
        measurement: str,
        target_hour: datetime,
        *,
        latest_forecast: dict[tuple[str, str, str], tuple[float, datetime]],
        bucket_lookup: dict[tuple, Any],
    ) -> Optional[float]:
        """**v0.1.13**: pure in-memory lookup, no database access at all —
        both dicts are built once per cycle in _compute_blend from two
        bulk queries, not fetched here. Same blending math as before,
        just no longer paying for a round trip per (hour, measurement,
        source) combination.
        """
        from .models import model_a

        hour_of_day = target_hour.hour
        season = model_a.derive_season(target_hour)
        target_iso = target_hour.replace(minute=0, second=0, microsecond=0).isoformat()

        contributions: list[model_a.SourceContribution] = []
        for source in ALL_FORECAST_SOURCES:
            entry = latest_forecast.get((source, measurement, target_iso))
            if entry is None:
                continue
            raw_value, issued_at = entry
            lead_time_bucket = model_a.derive_lead_time_bucket(issued_at, target_hour)
            bucket = bucket_lookup.get(
                (hour_of_day, season, lead_time_bucket, source, measurement)
            )
            if bucket is None:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=raw_value,
                        ema_bias=0.0, ema_weight=1.0, sample_count=0,
                    )
                )
            else:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=raw_value,
                        ema_bias=bucket.ema_bias, ema_weight=bucket.ema_weight,
                        sample_count=bucket.sample_count,
                    )
                )
        return model_a.blend(contributions)

    def _compute_blend(self) -> dict[str, Any]:
        from .models import model_a
        from .storage.db import BucketStats

        now = model_a.utcnow().replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(hours=self.FORECAST_HOURS_AHEAD)

        # Two bulk queries for the whole cycle, replacing what used to be
        # up to ~8,400 individual round trips — see the class docstring.
        raw_rows = self._db.get_forecast_snapshots_in_window(
            start_valid_at=now.isoformat(), end_valid_at=end.isoformat()
        )
        latest_forecast: dict[tuple[str, str, str], tuple[float, datetime]] = {}
        for row in raw_rows:
            if row["value"] is None:
                continue
            key = (row["source"], row["variable"], row["valid_at"])
            if key in latest_forecast:
                continue  # already have the freshest (rows are issued_at DESC)
            latest_forecast[key] = (row["value"], datetime.fromisoformat(row["issued_at"]))

        bucket_rows = self._db.get_all_bucket_stats()
        bucket_lookup: dict[tuple, BucketStats] = {}
        for row in bucket_rows:
            key = (
                row["hour_of_day"], row["season"], row["lead_time_bucket"],
                row["source"], row["measurement"],
            )
            bucket_lookup[key] = BucketStats(
                ema_bias=row["ema_bias"], ema_abs_error=row["ema_abs_error"],
                ema_weight=row["ema_weight"], sample_count=row["sample_count"],
                last_updated=row["last_updated"],
            )

        current = {
            m: self._blend_at(m, now, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            for m in self.MEASUREMENTS
        }

        # v0.1.14 fix: ExpertWeightSensor used to call self._db.get_bucket_stats()
        # directly inside its native_value property — a plain (non-
        # CoordinatorEntity) property that HA polls directly on the event
        # loop, completely bypassing the executor-job pattern used
        # everywhere else in this project. Computed here instead, for
        # free — bucket_lookup is already fetched above for the blend
        # itself, so extracting the "current hour/season/short lead time"
        # weight per source costs nothing extra.
        from .const import LEAD_TIME_SHORT
        from .models import model_a as _model_a_for_weights

        season_now = _model_a_for_weights.derive_season(now)
        expert_weights: dict[str, Optional[float]] = {}
        for source in ALL_FORECAST_SOURCES:
            bucket = bucket_lookup.get(
                (now.hour, season_now, LEAD_TIME_SHORT, source, "temperature")
            )
            expert_weights[source] = bucket.ema_weight if bucket else None

        hourly_forecast: list[dict[str, Any]] = []
        for i in range(self.FORECAST_HOURS_AHEAD):
            target = now + timedelta(hours=i)
            temperature = self._blend_at("temperature", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            humidity = self._blend_at("humidity", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            pressure = self._blend_at("pressure", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            precip = self._blend_at("precip", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            wind_speed = self._blend_at("wind_speed", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
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
            "expert_weights": expert_weights,
            "hourly_forecast": hourly_forecast,
            # Built from the same hourly data above — no extra DB access,
            # just reshaped, per the request to have precipitation (mm)
            # available at daily and twice-daily granularity too, not just
            # hourly.
            # v0.1.15 fix: these used to always group by UTC calendar day
            # regardless of the configured local timezone — confirmed by
            # an outside code review. dt_util.now().tzinfo is the same
            # proven pattern already used for meteoblue/Meteonomiqs's own
            # local-time fixes (v0.1.6/v0.1.15), not a new API.
            "daily_forecast": model_a.aggregate_daily_forecast(
                hourly_forecast, local_tz=dt_util.now().tzinfo
            ),
            "twice_daily_forecast": model_a.aggregate_twice_daily_forecast(
                hourly_forecast, local_tz=dt_util.now().tzinfo
            ),
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
    # v0.1.15 fix: how long to keep retrying a row that couldn't find a
    # matching station reading, before treating the gap as permanent and
    # letting the watermark advance past it. Without this, the watermark
    # used to advance to "now" unconditionally every cycle regardless of
    # skipped rows — a station outage lasting even a few minutes longer
    # than the matching tolerance would permanently drop that hour's
    # learning sample forever, with no distinction between "genuinely
    # unrecoverable" and "just hasn't been retried yet". Confirmed by an
    # outside code review against this exact loop. 48 hours gives several
    # retry cycles (every 20 minutes) before concluding a gap is real,
    # without letting the retry window grow unbounded if a gap turns out
    # to be permanent.
    RETRY_GIVE_UP_AGE = timedelta(hours=48)

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
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has.
        async with asyncio.timeout(120):
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
        # v0.1.15 fix: tracks the earliest valid_at among rows that
        # couldn't be matched to a station reading but are still young
        # enough to be worth retrying (see RETRY_GIVE_UP_AGE above) — the
        # watermark below only advances up to this point, not
        # unconditionally to "now", so these rows get another chance on
        # the next cycle instead of being silently dropped forever.
        earliest_retry_valid_at: Optional[datetime] = None
        for fs_row in rows_to_reconcile:
            if fs_row["value"] is None:
                # The stored forecast value itself is null — this can
                # never change no matter how many times it's retried, so
                # there's nothing to gain by holding the watermark back
                # for it specifically.
                continue
            measurement = fs_row["variable"]
            valid_at = datetime.fromisoformat(fs_row["valid_at"])
            issued_at = datetime.fromisoformat(fs_row["issued_at"])

            actual_value = model_a.find_nearest_observation(
                target=valid_at, candidates=candidates_by_measurement[measurement]
            )
            if actual_value is None:
                if (now - valid_at) < self.RETRY_GIVE_UP_AGE:
                    if earliest_retry_valid_at is None or valid_at < earliest_retry_valid_at:
                        earliest_retry_valid_at = valid_at
                # else: old enough that this gap is treated as permanent
                # (e.g. a genuine, lasting station outage) — let the
                # watermark advance past it rather than holding the retry
                # window open forever for something that will never
                # resolve.
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

        # v0.1.15 fix: cap the new watermark at the earliest still-retryable
        # skipped row's valid_at, if there is one — otherwise advance fully
        # to now, same as before. See the loop above and RETRY_GIVE_UP_AGE.
        new_watermark = (
            earliest_retry_valid_at.isoformat()
            if earliest_retry_valid_at is not None
            else until_iso
        )
        self._db.set_reconciliation_watermark(new_watermark)
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
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has. Generous (90s) since this method can also
        # trigger meteoblue/Meteonomiqs bonus calls (each already
        # independently timed-out, but bounding the whole cycle here too
        # is cheap insurance).
        async with asyncio.timeout(90):
            return await self._async_update_data_inner()

    async def _async_update_data_inner(self) -> float:
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
        base_probability = model_b.score_v0_graduated(features)
        probability = base_probability

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
            # v0.1.15 fix: these bonus calls used to be unguarded — a
            # transient failure in either (a timeout, a rate limit —
            # plausible exactly during a real storm scenario when these
            # APIs may be under more load) would raise all the way out of
            # this method, meaning the freshly computed probability above
            # was never saved to current_probability at all. Confirmed by
            # an independent review as a real bug in the specific feature
            # (storm-onset detection for blinds automation) this project
            # was built for — exactly the moment reliability matters most.
            # Now isolated: a bonus-call failure is logged but never
            # prevents the base scoring result from being persisted and
            # exposed below.
            try:
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
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Cross-model trigger's bonus calls failed (base "
                    "probability %.2f is still saved normally): %s",
                    base_probability,
                    err,
                )

        # v0.1.15 fix: this used to persist the pre-refinement probability
        # (computed before the trigger/refinement block above), while
        # current_probability below got the post-refinement value — the
        # same storm event could show two different numbers depending on
        # whether you looked at history or the live sensor. Now persists
        # after refinement, and stores both values explicitly so the
        # refinement's effect (when it fires) stays visible in history
        # rather than being silently overwritten.
        await self.hass.async_add_executor_job(
            self._db.insert_storm_prediction,
            datetime.now(timezone.utc).isoformat(),
            probability,
            {
                "delta_pressure_30min": features.delta_pressure_30min,
                "delta_humidity_30min": features.delta_humidity_30min,
                "radar_points": {p.label: p.precip_rate_mmh for p in radar_points},
                "base_probability": base_probability,
                "refined_probability": probability,
            },
        )

        self._previous_probability = probability
        self.current_probability = probability
        return probability
