"""The main fused weather entity — Model A's blend, exposed as weather.*.

**v0.1.5 rewrite**: this used to query the database directly and
synchronously inside entity properties — every other part of this project
routes DB access through an executor job except this one had been. All
blending now happens in coordinator.ModelABlendCoordinator, computed once
per refresh cycle inside a single batched executor job; this entity is a
thin CoordinatorEntity that just reads the cached result, the same
pattern used everywhere else in this integration.

Also new in v0.1.5: a genuine multi-hour/daily/twice-daily forecast
(previously removed entirely in v0.1.2 rather than ship a broken
never-resolving spinner), and wind speed exposure (data that was already
flowing through Model A's blend but was never actually surfaced).
"""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.weather import Forecast, WeatherEntity, WeatherEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .device import build_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SwissWeatherFusionWeather(entry, runtime)])


class SwissWeatherFusionWeather(CoordinatorEntity, WeatherEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY
        | WeatherEntityFeature.FORECAST_DAILY
        | WeatherEntityFeature.FORECAST_TWICE_DAILY
    )

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        super().__init__(runtime["blend_coordinator"])
        self._attr_unique_id = f"{entry.entry_id}_weather"
        self._attr_device_info = build_device_info(entry)

    @property
    def _current(self) -> dict[str, Any]:
        data = self.coordinator.data
        if not data:
            return {}
        return data.get("current", {})

    @property
    def native_temperature(self) -> Optional[float]:
        return self._current.get("temperature")

    @property
    def humidity(self) -> Optional[float]:
        return self._current.get("humidity")

    @property
    def native_pressure(self) -> Optional[float]:
        return self._current.get("pressure")

    @property
    def native_wind_speed(self) -> Optional[float]:
        return self._current.get("wind_speed")

    @property
    def condition(self) -> Optional[str]:
        # A precipitation-derived condition is a reasonable v0.1 default;
        # richer condition mapping (cloud cover, weather codes per source)
        # is a natural future enhancement once real accuracy data suggests
        # where the current blend is weakest.
        precip = self._current.get("precip")
        if precip is None:
            return None
        return "rainy" if precip > 0.1 else "sunny"

    async def async_forecast_hourly(self) -> list[Forecast]:
        data = self.coordinator.data
        if not data:
            return []
        return data.get("hourly_forecast", [])

    async def async_forecast_daily(self) -> list[Forecast]:
        data = self.coordinator.data
        if not data:
            return []
        return data.get("daily_forecast", [])

    async def async_forecast_twice_daily(self) -> list[Forecast]:
        data = self.coordinator.data
        if not data:
            return []
        return data.get("twice_daily_forecast", [])
