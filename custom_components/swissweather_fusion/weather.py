"""The main fused weather entity — Model A's blend, exposed as weather.*.

Reads bucket_stats to debias each source's latest forecast_snapshots row
for the current hour/season/lead-time, then blends per models/model_a.py.
This entity does not fetch anything itself — all data arrives via the
coordinators in coordinator.py; this class only reads what's already in
storage and applies the blend.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from homeassistant.components.weather import WeatherEntity, WeatherEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPressure,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALL_FORECAST_SOURCES, DOMAIN, MIN_SAMPLES_TO_TRUST_BUCKET
from .models import model_a
from .storage.db import BucketKey, SwissWeatherDB


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SwissWeatherFusionWeather(hass, entry, runtime)])


class SwissWeatherFusionWeather(CoordinatorEntity, WeatherEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_supported_features = WeatherEntityFeature.FORECAST_HOURLY

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(runtime["open_meteo_coordinator"])
        self._hass = hass
        self._entry = entry
        self._db: SwissWeatherDB = runtime["db"]
        self._attr_unique_id = f"{entry.entry_id}_weather"

    def _blend_measurement(self, measurement: str) -> Optional[float]:
        now = model_a.utcnow()
        hour_of_day = now.hour
        season = model_a.derive_season(now)

        contributions: list[model_a.SourceContribution] = []
        for source in ALL_FORECAST_SOURCES:
            rows = self._db.get_forecast_values_for_valid_at(
                source=source, variable=measurement, valid_at=now.replace(minute=0, second=0, microsecond=0).isoformat()
            )
            if not rows:
                continue
            latest_row = rows[0]  # already ordered by issued_at DESC
            issued_at = datetime.fromisoformat(latest_row["issued_at"])
            lead_time_bucket = model_a.derive_lead_time_bucket(issued_at, now)
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
                        source=source,
                        raw_value=latest_row["value"],
                        ema_bias=0.0,
                        ema_weight=1.0,
                        sample_count=0,
                    )
                )
            else:
                contributions.append(
                    model_a.SourceContribution(
                        source=source,
                        raw_value=latest_row["value"],
                        ema_bias=bucket.ema_bias,
                        ema_weight=bucket.ema_weight,
                        sample_count=bucket.sample_count,
                    )
                )
        return model_a.blend(contributions)

    @property
    def native_temperature(self) -> Optional[float]:
        return self._blend_measurement("temperature")

    @property
    def humidity(self) -> Optional[float]:
        return self._blend_measurement("humidity")

    @property
    def native_pressure(self) -> Optional[float]:
        return self._blend_measurement("pressure")

    @property
    def condition(self) -> Optional[str]:
        # A precipitation-derived condition is a reasonable v0.1 default;
        # richer condition mapping (cloud cover, weather codes per source)
        # is a natural v0.2 enhancement once real accuracy data suggests
        # where the current blend is weakest.
        precip = self._blend_measurement("precip")
        if precip is None:
            return None
        return "rainy" if precip > 0.1 else "sunny"

    async def async_forecast_hourly(self) -> list[dict[str, Any]]:
        # v0.1: a minimal single-point-ahead stub. Full multi-hour blended
        # forecasts (blending each source's own hourly series, not just
        # "now") is the natural next iteration — flagged in DEVELOPER.md
        # rather than built speculatively before the core loop is proven.
        return []
