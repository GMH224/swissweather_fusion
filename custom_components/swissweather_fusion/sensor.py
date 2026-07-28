"""Sensors — status, learning progress, forecast accuracy, per-source
telemetry, and Model B's live storm probability.

Explicitly required (per the build request this integration was written
for): sensors showing learning progress and forecast accuracy, not just
the final blended forecast. Those are last_learning_a / last_learning_b
and forecast_accuracy below.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ALL_FORECAST_SOURCES,
    DOMAIN,
    SOURCE_CH1,
    SOURCE_CH2,
    SOURCE_COMBIPRECIP,
    SOURCE_ICON_D2,
    SOURCE_METEOBLUE,
    SOURCE_METEONOMIQS,
    SOURCE_SRF,
)
from .device import build_device_info
from .health import SourceHealth
from .storage.db import SwissWeatherDB

ALL_TELEMETRY_SOURCES = ALL_FORECAST_SOURCES + (SOURCE_COMBIPRECIP, SOURCE_METEONOMIQS)


def _get_health(runtime: dict[str, Any], source: str) -> Optional[SourceHealth]:
    """Maps a source name to its SourceHealth, regardless of which
    coordinator owns it. CH1/CH2/ICON-D2 share one coordinator (all three
    are Open-Meteo) but get independent health entries within it, since
    one model can fail while the others succeed.
    """
    if source in (SOURCE_CH1, SOURCE_CH2, SOURCE_ICON_D2):
        return runtime["open_meteo_coordinator"].health.get(source)
    if source == SOURCE_SRF:
        return runtime["srf_coordinator"].health
    if source == SOURCE_METEOBLUE:
        return runtime["meteoblue_coordinator"].health
    if source == SOURCE_COMBIPRECIP:
        return runtime["combiprecip_coordinator"].health
    if source == SOURCE_METEONOMIQS:
        return runtime["meteonomiqs_coordinator"].health
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    db: SwissWeatherDB = runtime["db"]

    entities: list[SensorEntity] = [
        StatusSensor(entry, runtime),
        ForecastAccuracySensor(entry, db),
        ActiveSourcesSensor(entry, db, runtime),
        LastLearningASensor(entry, db),
        LastLearningBSensor(entry, db),
        StormOnsetProbabilitySensor(entry, runtime),
    ]
    for source in ALL_FORECAST_SOURCES:
        entities.append(ExpertWeightSensor(entry, db, source))
    for source in ALL_TELEMETRY_SOURCES:
        entities.append(LastSuccessSensor(entry, runtime, source))
        entities.append(LastPollDurationSensor(entry, runtime, source))
        entities.append(LastDataErrorSensor(entry, runtime, source))
        entities.append(ConsecutiveFailuresSensor(entry, runtime, source))
    # Only SRF has a credential that can expire/be revoked — the other
    # sources are either keyless (Open-Meteo) or key-based without an
    # OAuth exchange (meteoblue, Meteonomiqs), so an "auth error" isn't a
    # meaningful distinct category for them the way it is for SRF.
    entities.append(LastAuthErrorSensor(entry, runtime, SOURCE_SRF))

    async_add_entities(entities)


class _BaseSensor(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, unique_suffix: str, name: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_info = build_device_info(entry)


class StatusSensor(_BaseSensor):
    """Active / Degraded / Error, now backed by real per-source health
    state rather than a single coordinator's generic exception flag.
    """

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "status", "Status")
        self._runtime = runtime

    @property
    def native_value(self) -> str:
        failure_counts = [
            _get_health(self._runtime, source).consecutive_failures
            for source in ALL_TELEMETRY_SOURCES
            if _get_health(self._runtime, source) is not None
        ]
        if not failure_counts:
            return "Active"
        if all(count > 0 for count in failure_counts):
            return "Error"  # every source currently failing — likely a
                             # network-level problem, not one bad credential
        if any(count > 0 for count in failure_counts):
            return "Degraded"  # binary_sensor.*_degraded carries this same
                                # signal; kept here too so a dashboard card
                                # doesn't need to reference two entities
        return "Active"


class ForecastAccuracySensor(_BaseSensor):
    """Rolling 7-day MAE of the blended forecast vs. actual station
    reading, for temperature (the natural headline number — humidity and
    pressure MAE are exposed as attributes rather than separate top-level
    sensors, per plan doc §7).
    """

    _attr_native_unit_of_measurement = "°C"

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB) -> None:
        super().__init__(entry, "forecast_accuracy", "Forecast accuracy (7d MAE)")
        self._db = db
        self._attr_extra_state_attributes: dict[str, Any] = {}

    @property
    def native_value(self) -> Optional[float]:
        # v0.1: computing a true rolling MAE requires joining
        # forecast_snapshots against station_observations by valid_at,
        # which is meaningful work best done once real data exists to
        # validate the join logic against. Exposed as a stub (None) with a
        # clear docstring rather than a fabricated placeholder number —
        # wiring this up properly is flagged as the first thing to build
        # once the core loop has been running for a few days.
        return None


class ActiveSourcesSensor(_BaseSensor):
    """Count of sources whose most recent poll succeeded — now genuinely
    computed from health state, not a hardcoded total.
    """

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "active_sources", "Active sources")
        self._db = db
        self._runtime = runtime

    @property
    def native_value(self) -> int:
        active = 0
        for source in ALL_TELEMETRY_SOURCES:
            health = _get_health(self._runtime, source)
            if health is not None and health.consecutive_failures == 0:
                active += 1
        return active


class LastLearningASensor(_BaseSensor):
    """When Model A's EMA buckets last updated — continuous in principle,
    this reports the most recent bucket_stats.last_updated across all
    buckets, giving a simple heartbeat for "is Model A actually learning".
    """

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB) -> None:
        super().__init__(entry, "last_learning_a", "Model A last learning update")
        self._db = db

    @property
    def native_value(self) -> Optional[datetime]:
        return None  # wired to a MAX(last_updated) query once implemented


class LastLearningBSensor(_BaseSensor):
    """Model B retrains on a much slower cycle than Model A (a full storm
    season for v1) — this reports when v1 was last (re)trained, distinct
    from the live scoring cadence.
    """

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB) -> None:
        super().__init__(entry, "last_learning_b", "Model B last training")
        self._db = db

    @property
    def native_value(self) -> Optional[datetime]:
        return None  # None until v1 training actually happens — v0 is a
                     # fixed rule, not something that "learns" per se


class ExpertWeightSensor(_BaseSensor):
    """One per Model A source — for debugging the live blend, per plan
    doc §7. Exposes the current-hour/season/short-lead-time weight as a
    representative snapshot rather than every bucket (which would be a lot
    of numbers for a single sensor state).
    """

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB, source: str) -> None:
        super().__init__(entry, f"expert_weight_{source}", f"Expert weight: {source}")
        self._db = db
        self._source = source

    @property
    def native_value(self) -> Optional[float]:
        from .models.model_a import derive_season, utcnow
        from .const import LEAD_TIME_SHORT
        from .storage.db import BucketKey

        now = utcnow()
        bucket = self._db.get_bucket_stats(
            BucketKey(
                hour_of_day=now.hour,
                season=derive_season(now),
                lead_time_bucket=LEAD_TIME_SHORT,
                source=self._source,
                measurement="temperature",
            )
        )
        return bucket.ema_weight if bucket else None


class StormOnsetProbabilitySensor(_BaseSensor):
    """Model B's live output — the "storm in ~30 minutes" indicator this
    integration was specifically extended for (blinds automation, etc).
    """

    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "storm_onset_probability", "Storm onset probability")
        self._runtime = runtime

    @property
    def native_value(self) -> float:
        return round(self._runtime["model_b_coordinator"].current_probability * 100, 1)


class LastSuccessSensor(_BaseSensor):
    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_last_success", f"{source}: last success")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> Optional[datetime]:
        health = _get_health(self._runtime, self._source)
        return health.last_success_time if health else None


class LastPollDurationSensor(_BaseSensor):
    _attr_native_unit_of_measurement = "ms"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_last_poll_duration", f"{source}: last poll duration")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> Optional[float]:
        health = _get_health(self._runtime, self._source)
        return health.last_poll_duration_ms if health else None


class LastDataErrorSensor(_BaseSensor):
    """Data errors (malformed response, timeout, non-auth HTTP errors) —
    the graceful-degradation cooldown+retry case, distinct from an auth
    error which won't resolve on its own. See health.py.
    """

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_last_data_error", f"{source}: last data error")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> Optional[str]:
        health = _get_health(self._runtime, self._source)
        return health.last_data_error if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = _get_health(self._runtime, self._source)
        if health is None or health.last_data_error_time is None:
            return {}
        return {"occurred_at": health.last_data_error_time.isoformat()}


class LastAuthErrorSensor(_BaseSensor):
    """The specific scenario this sensor exists for: an expired or revoked
    API credential. Distinct from LastDataErrorSensor precisely because
    the fix is different — re-enter credentials via the reauth flow, not
    wait for a retry that will just fail the same way again.
    """

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_last_auth_error", f"{source}: last auth error")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> Optional[str]:
        health = _get_health(self._runtime, self._source)
        return health.last_auth_error if health else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        health = _get_health(self._runtime, self._source)
        if health is None or health.last_auth_error_time is None:
            return {}
        return {"occurred_at": health.last_auth_error_time.isoformat()}


class ConsecutiveFailuresSensor(_BaseSensor):
    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_consecutive_failures", f"{source}: consecutive failures")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> int:
        health = _get_health(self._runtime, self._source)
        return health.consecutive_failures if health else 0
