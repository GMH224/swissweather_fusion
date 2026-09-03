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

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    MIN_SAMPLES_TO_TRUST_BUCKET,
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
        ForecastAccuracySensor(entry, runtime),
        ActiveSourcesSensor(entry, db, runtime),
        LastLearningASensor(entry, runtime),
        LastLearningBSensor(entry, db),
        StormOnsetProbabilitySensor(entry, runtime),
        StorageSizeSensor(entry, runtime),
        PressureReferenceDeltaSensor(entry, runtime),
        BlendAccuracySensor(entry, runtime),
        BestSourceAccuracySensor(entry, runtime),
        LearningProgressSensor(entry, runtime),
        TrustedBucketCountSensor(entry, runtime),
    ]
    for source in ALL_FORECAST_SOURCES:
        entities.append(ExpertWeightSensor(entry, runtime, source))
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


# v0.2.1 (SWF-P2-008): sources that legitimately run once a day must not
# be reported as unhealthy simply because they have not run yet.
#
# Meteonomiqs calls once daily and meteoblue three times daily. Under the
# v0.1.24 rule below, both counted as unhealthy from every restart until
# their next scheduled slot — so the integration reported "Degraded" for
# hours at a time with nothing actually wrong, which is precisely the
# way a health indicator becomes ignored.
#
# Grace periods are generous multiples of each source's real cadence:
# long enough that a genuine outage still surfaces, short enough that it
# surfaces the same day.
NEVER_SUCCEEDED_GRACE = {
    "meteonomiqs": timedelta(hours=26),   # once daily, gated on local noon
    "meteoblue": timedelta(hours=26),     # three scheduled slots per day
    "combiprecip": timedelta(minutes=30),
    "srf": timedelta(hours=3),
}
DEFAULT_NEVER_SUCCEEDED_GRACE = timedelta(hours=1)


def is_source_healthy(health: Any, source: Optional[str] = None) -> bool:
    """Whether a source is genuinely working, as opposed to untried.

    **v0.1.24 fix (P2-11 / IND-03).** Health was derived from
    `consecutive_failures == 0` alone — equally true of a source that has
    never been polled, since zero is the attribute's initial value. A
    cold start therefore reported every source active before a single
    successful fetch.

    **v0.2.1 refinement (SWF-P2-008).** Requiring `last_success_time` was
    right for a source polling every five minutes and wrong for one that
    runs daily by design. A never-succeeded source is now treated as
    *pending* rather than failed until its grace period elapses; after
    that it is unhealthy, so a genuinely dead source still surfaces.

    `source` is optional so existing callers keep working; without it the
    default grace applies.
    """
    if health is None:
        return False
    if health.consecutive_failures > 0:
        return False
    if health.last_success_time is not None:
        return True

    # Never succeeded: healthy only while still within its grace window.
    started = getattr(health, "created_at", None)
    if started is None:
        # No start time recorded — fall back to the strict v0.1.24 rule
        # rather than assuming health we cannot evidence.
        return False
    grace = NEVER_SUCCEEDED_GRACE.get(source or "", DEFAULT_NEVER_SUCCEEDED_GRACE)
    return (datetime.now(timezone.utc) - started) < grace


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
        # v0.1.24 (IND-03): "no failures yet" is not the same as
        # "working" — see is_source_healthy.
        failure_counts = [
            0 if is_source_healthy(_get_health(self._runtime, src), src) else 1
            for src in ALL_TELEMETRY_SOURCES
            if _get_health(self._runtime, src) is not None
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
    """Learned mean absolute temperature error across all buckets.

    **v0.1.28 fix (SWF-P1-007).** This sensor was blank for four
    releases, in two different ways, and the second was worse than the
    first.

    Through v0.1.23 it returned None by design — an honest, documented
    stub. v0.1.24's P3-02 fix set out to implement it from
    `bucket_stats.ema_abs_error`, which is real, durable, continuously
    updated data. But it iterated `get_all_bucket_stats()` — a list of
    sqlite3.Row — as though it were a dict, raising AttributeError on
    every single call, and wrapped that in a blanket
    `except Exception: return None`. The result looked implemented and
    behaved like a stub. Its own test asserted None-when-nothing-learned,
    which is indistinguishable from None-because-it-crashed, so the test
    passed against permanently broken code.

    It also queried SQLite from a property, which Home Assistant polls on
    the event loop.

    Both are fixed by moving the computation into
    ModelALearningCoordinator, which already reads bucket_stats inside an
    executor job every 20 minutes. This entity now just reads the cached
    result — no database access, no blanket except, and a genuine number
    to display.
    """

    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        # Entity key and unique_id deliberately unchanged, so existing
        # installations keep their entity_id and history.
        super().__init__(
            entry, "forecast_accuracy", "Forecast accuracy (learned temperature MAE)"
        )
        self._runtime = runtime

    @property
    def _mae(self) -> Optional[dict[str, Any]]:
        coordinator = self._runtime.get("learning_coordinator")
        return getattr(coordinator, "temperature_mae", None) if coordinator else None

    @property
    def native_value(self) -> Optional[float]:
        mae = self._mae
        return mae["value"] if mae else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The methodology is disclosed on the entity itself, so anyone
        reading this number in the UI can see what it actually measures
        (v0.1.24, P3-02)."""
        mae = self._mae
        return {
            "methodology": (
                "sample-count-weighted mean of bucket_stats.ema_abs_error "
                "across temperature buckets; an EMA of |forecast - observed|, "
                "not a fixed-window MAE"
            ),
            "temperature_bucket_count": mae["bucket_count"] if mae else 0,
            "total_sample_count": mae["sample_count"] if mae else 0,
            # v0.2.4 (SWF-024-001): the falsifiability attributes. The
            # headline value above is the average error of the INPUTS;
            # these say whether the blended output does better than the
            # best of them. blend_beats_best_source is the honest
            # scoreboard for the whole approach.
            "blend_mae": mae.get("blend_mae") if mae else None,
            "best_source": mae.get("best_source") if mae else None,
            "best_source_mae": mae.get("best_source_mae") if mae else None,
            "blend_beats_best_source": (
                mae.get("blend_beats_best_source") if mae else None
            ),
        }


class ActiveSourcesSensor(_BaseSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT

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
            # v0.1.24 fix (P2-11): a source that has never succeeded is
            # not active. See is_source_healthy.
            if is_source_healthy(_get_health(self._runtime, source), source):
                active += 1
        return active


class LastLearningASensor(_BaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    """When Model A's EMA buckets last updated. **Fixed in v0.1.7**: this
    was a permanent stub (always None) because nothing in production code
    actually ran the reconciliation step — see ModelALearningCoordinator
    in coordinator.py for the full story. Now reports that coordinator's
    last successful run, plus how many forecast snapshots it reconciled,
    as a real heartbeat for "is Model A actually learning" rather than an
    always-empty placeholder.
    """

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "last_learning_a", "Model A last learning update")
        self._runtime = runtime

    @property
    def native_value(self) -> Optional[datetime]:
        coordinator = self._runtime.get("learning_coordinator")
        if coordinator is None:
            return None
        return coordinator.data

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self._runtime.get("learning_coordinator")
        if coordinator is None:
            return {}
        return {"reconciled_last_run": getattr(coordinator, "last_reconciled_count", 0)}


class LastLearningBSensor(_BaseSensor):
    """Model B retrains on a much slower cycle than Model A (a full storm
    season for v1) — this reports when v1 was last (re)trained, distinct
    from the live scoring cadence.
    """

    # v0.1.24 fix (P3-01): hidden by default on new installations. An
    # entity that is permanently None looks broken, and this one is
    # permanently None BY DESIGN — Model B v0 is a fixed heuristic with
    # no training step at all.
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, db: SwissWeatherDB) -> None:
        # The entity key and unique_id ("last_learning_b") are
        # deliberately UNCHANGED. Renaming would orphan every existing
        # installation's entity_id, automations and history — a worse
        # outcome than the labelling problem being fixed. Only the
        # user-visible name and attributes change.
        super().__init__(
            entry,
            "last_learning_b",
            "Model B training status (not applicable — v0 uses fixed rules)",
        )
        self._db = db

    @property
    def native_value(self) -> Optional[datetime]:
        return None  # None until v1 training actually happens — v0 is a
                     # fixed rule, not something that "learns" per se

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "not_applicable": True,
            "reason": (
                "Model B v0 is a fixed threshold heuristic with no training "
                "step. This becomes meaningful only once a v1 classifier is "
                "trained on accumulated storm_events."
            ),
        }


class ExpertWeightSensor(CoordinatorEntity, _BaseSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    """One per Model A source — for debugging the live blend, per plan
    doc §7. Exposes the current-hour/season/short-lead-time weight as a
    representative snapshot rather than every bucket (which would be a lot
    of numbers for a single sensor state).

    **v0.1.14 fix**: this used to call self._db.get_bucket_stats()
    directly inside native_value — a plain property with no
    CoordinatorEntity backing, meaning HA's own polling called it directly
    on the event loop, completely bypassing the executor-job pattern used
    everywhere else in this project. An outside code review (checked
    directly against the actual source, not assumed) confirmed this was
    real. Now reads a cached value from ModelABlendCoordinator, which
    computes it during its own executor-job-wrapped refresh — no direct
    database access from this entity at all.
    """

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(runtime["blend_coordinator"])
        _BaseSensor.__init__(self, entry, f"expert_weight_{source}", f"Expert weight: {source}")
        self._source = source

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data
        if not data:
            return None
        return data.get("expert_weights", {}).get(self._source)


class StormOnsetProbabilitySensor(_BaseSensor):
    """Model B's live output — the "storm in ~30 minutes" indicator this
    integration was specifically extended for (blinds automation, etc).
    """

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        # v0.1.24 fix (P2-06): renamed from "Storm onset probability".
        # Calling it a probability implies statistical calibration the v0
        # heuristic does not have — the score is max() of two
        # hand-authored signals, optionally averaged with an external
        # risk index. "Risk score" is what it actually is.
        #
        # The entity key and unique_id are deliberately unchanged, for
        # the same orphaning reason as LastLearningBSensor above.
        super().__init__(entry, "storm_onset_probability", "Storm onset risk score")
        self._runtime = runtime

    @property
    def native_value(self) -> Optional[float]:
        coordinator = self._runtime.get("model_b_coordinator")
        if coordinator is None:
            return None
        return round(coordinator.current_probability * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """v0.1.24 fix (P2-06 / P2-07): disclose the methodology on the
        entity itself — reachable from the UI, templates and the REST
        API — rather than only in a source comment that no user will
        ever read."""
        return {
            "is_calibrated_probability": False,
            "methodology": (
                "v0 heuristic: the higher of a station-tendency threshold "
                "score and a distance-graded radar score. When an "
                "independent Meteonomiqs nowcast is available it is blended "
                "50/50 — a weighting with no statistical basis beyond there "
                "being no measured reason to favour either source."
            ),
        }


class LastSuccessSensor(_BaseSensor):
    # v0.1.24 fix (IND-08): native_value returns a datetime, and Home
    # Assistant requires SensorDeviceClass.TIMESTAMP for that to be
    # stored and rendered as a timestamp rather than coerced to a string.
    # No entity in this integration declared a device class, a state
    # class or an entity category before this release.
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
    # v0.1.24 (IND-08): MEASUREMENT is what makes this eligible for Home
    # Assistant's long-term statistics, so poll latency can actually be
    # charted over time rather than only inspected as a live value.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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

    _attr_entity_category = EntityCategory.DIAGNOSTIC

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

    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any], source: str) -> None:
        super().__init__(entry, f"{source}_consecutive_failures", f"{source}: consecutive failures")
        self._runtime = runtime
        self._source = source

    @property
    def native_value(self) -> int:
        health = _get_health(self._runtime, self._source)
        return health.consecutive_failures if health else 0


class StorageSizeSensor(_BaseSensor):
    """Size of the learning database on disk.

    **v0.2.1 (architecture review AR-02).** `get_storage_stats()` has
    existed in the storage layer since v0.1.24 and nothing ever exposed
    it, so database growth was invisible until a disk filled.

    That mattered little at five fused variables. v0.2.0 took Open-Meteo
    from 5 to 18 hourly variables, roughly 3.4x the rows per run, so a
    measured 389 bytes/row puts a 90-day window near 6.5 GB. Growth is
    also effectively one-way within a deployment: purge_older_than() does
    not VACUUM, so SQLite reuses freed pages rather than returning them,
    and the file size stops rising but does not fall. Watching row counts
    alongside bytes is the only way to see a purge working.
    """

    _attr_native_unit_of_measurement = "MB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "storage_size", "Database size")
        self._runtime = runtime

    @property
    def _stats(self) -> Optional[dict[str, Any]]:
        coordinator = self._runtime.get("retention_coordinator")
        return getattr(coordinator, "storage_stats", None) if coordinator else None

    @property
    def native_value(self) -> Optional[float]:
        stats = self._stats
        if not stats or stats.get("file_size_bytes") is None:
            return None
        return round(stats["file_size_bytes"] / 1_000_000, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self._stats or {}
        return {k: v for k, v in stats.items() if k.endswith("_rows")}


class PressureReferenceDeltaSensor(_BaseSensor):
    """Station pressure minus the provider consensus.

    **v0.2.3 (SWF-023-001).** A live installation ran for a day with its
    station pressure 65 hPa above every provider, because a
    sea-level-normalised reading was being reduced to sea level a second
    time. Nothing surfaced the disagreement; it was only noticed when the
    blended pressure had visibly drifted.

    The relationship was self-diagnosing the whole time and nothing was
    looking. This sensor is the looking.

    Near zero means the station and the models agree, so the datum
    configuration is right. A persistent offset of tens of hPa means the
    'pressure sensor already reports sea-level pressure' option is set
    incorrectly. A few hPa is normal — models are not perfect and neither
    is a domestic barometer.
    """

    _attr_native_unit_of_measurement = "hPa"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, "pressure_reference_delta", "Pressure vs providers")
        self._runtime = runtime

    @property
    def native_value(self) -> Optional[float]:
        coordinator = self._runtime.get("station_coordinator")
        return getattr(coordinator, "pressure_reference_delta", None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        from .const import STATION_PRESSURE_REFERENCE_TOLERANCE_HPA

        value = self.native_value
        return {
            "tolerance_hpa": STATION_PRESSURE_REFERENCE_TOLERANCE_HPA,
            "within_tolerance": (
                None if value is None
                else abs(value) <= STATION_PRESSURE_REFERENCE_TOLERANCE_HPA
            ),
            "interpretation": (
                "Station reading minus the median provider mean-sea-level "
                "pressure for the same hour. A few hPa is normal. A "
                "persistent offset of tens of hPa means the 'pressure sensor "
                "already reports sea-level pressure' option is set wrongly."
            ),
        }


class _LearningStatSensor(_BaseSensor):
    """Shared base for the learning-progress sensors.

    **v0.2.4 (SWF-024-005).** These exist because Home Assistant records
    long-term statistics for a sensor's STATE and never for its
    attributes. v0.2.4 first put blend_mae, best_source_mae and the
    sample counts on ForecastAccuracySensor as attributes — where the
    current value is visible but the TREND, which is the entire question
    being asked, cannot be charted.

    Putting each on its own state makes the story readable: learning
    progress climbs to a plateau, blend error falls and flattens, and the
    gap between blend and best-source is either positive or it is not.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, entry: ConfigEntry, runtime: dict[str, Any], suffix: str, name: str
    ) -> None:
        super().__init__(entry, suffix, name)
        self._runtime = runtime

    @property
    def _mae(self) -> Optional[dict[str, Any]]:
        coordinator = self._runtime.get("learning_coordinator")
        return getattr(coordinator, "temperature_mae", None) if coordinator else None

    @property
    def _progress(self) -> Optional[dict[str, Any]]:
        coordinator = self._runtime.get("learning_coordinator")
        return getattr(coordinator, "learning_progress", None) if coordinator else None


class BlendAccuracySensor(_LearningStatSensor):
    """Mean absolute temperature error of the BLENDED output.

    This is the curve that should visibly bend downward over the first
    days and then flatten — unlike the per-provider average, which
    measures how noisy ICON inherently is and which no amount of learning
    can change.
    """

    _attr_native_unit_of_measurement = "°C"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, runtime, "blend_accuracy", "Blend accuracy (MAE)")

    @property
    def native_value(self) -> Optional[float]:
        mae = self._mae
        return mae.get("blend_mae") if mae else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mae = self._mae or {}
        return {
            "beats_best_source": mae.get("blend_beats_best_source"),
            "methodology": (
                "EMA of |blend forecast - observed| for temperature, from "
                "blend output recorded as a pseudo-source and reconciled "
                "like any provider. Chart against 'Best source accuracy' "
                "to see whether fusion is earning its complexity."
            ),
        }


class BestSourceAccuracySensor(_LearningStatSensor):
    """Mean absolute temperature error of the single best provider.

    The honest benchmark. If the blend cannot beat this, the learned bias
    correction is not worth its complexity — and that is worth seeing
    rather than assuming.
    """

    _attr_native_unit_of_measurement = "°C"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(
            entry, runtime, "best_source_accuracy", "Best source accuracy (MAE)"
        )

    @property
    def native_value(self) -> Optional[float]:
        mae = self._mae
        return mae.get("best_source_mae") if mae else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mae = self._mae or {}
        return {"best_source": mae.get("best_source")}


class LearningProgressSensor(_LearningStatSensor):
    """Share of learned buckets that have enough samples to be trusted.

    Without this, a flat accuracy line is ambiguous: it could mean
    learning has converged, or that it never started. Below
    MIN_SAMPLES_TO_TRUST_BUCKET a bucket contributes at the cold-start
    weight and bias correction is doing nothing for it.

    Expect this to climb toward a plateau, then step DOWN at each season
    boundary — buckets are keyed by season, so a transition empties the
    seasonal ones and they refill. That is expected behaviour, not a
    regression.
    """

    _attr_native_unit_of_measurement = "%"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, runtime, "learning_progress", "Learning progress")

    @property
    def native_value(self) -> Optional[float]:
        progress = self._progress
        return progress.get("trusted_pct") if progress else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        progress = self._progress or {}
        return {
            "buckets_trusted": progress.get("buckets_trusted"),
            "buckets_total": progress.get("buckets_total"),
            "minimum_samples_to_trust": MIN_SAMPLES_TO_TRUST_BUCKET,
            "note": (
                "Percentage is of buckets that exist, not of the full key "
                "space — most of that space is unreachable (ICON-CH1 only "
                "forecasts 33 hours, so it can have no long-lead buckets). "
                "Expect a step down at each season boundary."
            ),
        }


class TrustedBucketCountSensor(_LearningStatSensor):
    """Absolute number of trusted buckets.

    Complements the percentage, which alone hides whether the
    denominator is sane — 100% of four buckets is not convergence.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(entry, runtime, "trusted_buckets", "Trusted buckets")

    @property
    def native_value(self) -> Optional[int]:
        progress = self._progress
        return progress.get("buckets_trusted") if progress else None
