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
    UnitOfLength,
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
from .models import model_a


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SwissWeatherFusionWeather(entry, runtime)])


def _is_daytime_now(hass: Any) -> bool | None:
    """Whether the sun is currently above the horizon, or None if unknown.

    v0.1.28 (SWF-P2-005). Read from Home Assistant's own `sun.sun`
    entity rather than computed here: core already tracks it, it is
    correct for the installation's real location, and it costs nothing.
    Returning None when the entity is missing or in an unexpected state
    means derive_condition keeps its pre-v0.1.28 answer rather than
    guessing — a wrong "clear-night" at noon would be more conspicuous
    than the bug being fixed.
    """
    try:
        state = hass.states.get("sun.sun")
    except AttributeError:  # pragma: no cover - defensive
        return None
    if state is None:
        return None
    if state.state == "above_horizon":
        return True
    if state.state == "below_horizon":
        return False
    return None


class SwissWeatherFusionWeather(CoordinatorEntity, WeatherEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    # v0.2.1: required for native_visibility to be interpreted correctly.
    # Open-Meteo reports visibility in metres.
    _attr_native_visibility_unit = UnitOfLength.METERS
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

    # v0.2.1 fix (SWF-P2-007): five standard WeatherEntity properties that
    # were never implemented.
    #
    # v0.2.0 fused dew point, apparent temperature, cloud cover,
    # visibility and gusts into the blend, but this entity exposed only
    # temperature, humidity, pressure and wind speed — so a card
    # configured to show them displayed nothing, correctly, because the
    # entity provided nothing.
    #
    # These are all documented members of Home Assistant's WeatherEntity
    # contract, which means they need no custom sensor entities: they
    # reach any card through the standard path (architecture review
    # AR-01/AR-04).
    @property
    def native_dew_point(self) -> Optional[float]:
        return self._current.get("dew_point")

    @property
    def native_apparent_temperature(self) -> Optional[float]:
        return self._current.get("apparent_temperature")

    @property
    def cloud_coverage(self) -> Optional[float]:
        return self._current.get("cloud_coverage")

    @property
    def native_visibility(self) -> Optional[float]:
        return self._current.get("visibility")

    @property
    def native_wind_gust_speed(self) -> Optional[float]:
        return self._current.get("wind_gust_speed")

    @property
    def wind_bearing(self) -> Optional[float]:
        return self._current.get("wind_bearing")

    @property
    def uv_index(self) -> Optional[float]:
        return self._current.get("uv_index")

    @property
    def native_wind_speed(self) -> Optional[float]:
        return self._current.get("wind_speed")

    @property
    def condition(self) -> Optional[str]:
        # v0.1.24 fix (P2-10): this used to be
        # `"rainy" if precip > 0.1 else "sunny"`, which collapsed snow,
        # cloud, overcast and fog all into "sunny" — the weather card
        # showed a sun during a snowstorm. Four separate call sites had
        # the same two-branch logic; all four now share
        # model_a.derive_condition.
        #
        # This site keeps its own 0.1 mm threshold and its own
        # explicit-None behaviour, both deliberately preserved rather
        # than unified with the daily aggregation sites (which use 0.5 mm
        # and treat None as zero) — see derive_condition's docstring.
        return model_a.derive_condition(
            self._current.get("precip"),
            self._current.get("temperature"),
            self._current.get("humidity"),
            is_daytime=_is_daytime_now(self.hass),
        )

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
