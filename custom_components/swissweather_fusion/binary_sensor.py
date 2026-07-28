"""binary_sensor.<name>_degraded — one glance-able health signal instead
of checking each per-source sensor individually (plan doc §7).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import build_device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DegradedBinarySensor(entry, runtime)])


class DegradedBinarySensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Degraded"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_degraded"
        self._attr_device_info = build_device_info(entry)

    @property
    def is_on(self) -> bool:
        coordinators = [
            self._runtime["station_coordinator"],
            self._runtime["open_meteo_coordinator"],
            self._runtime["srf_coordinator"],
            self._runtime["meteoblue_coordinator"],
            self._runtime["combiprecip_coordinator"],
            self._runtime["meteonomiqs_coordinator"],
        ]
        return any(not c.last_update_success for c in coordinators)
