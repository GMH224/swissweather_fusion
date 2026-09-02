"""binary_sensor.<name>_degraded — one glance-able health signal instead
of checking each per-source sensor individually (plan doc §7).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import build_device_info
from .sensor import ALL_TELEMETRY_SOURCES, _get_health, is_source_healthy


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DegradedBinarySensor(entry, runtime)])


class DegradedBinarySensor(BinarySensorEntity):

    # v0.1.24 fix (IND-08): without a device class this rendered as a
    # generic on/off rather than OK/Problem, which is the semantic every
    # dashboard and automation actually wants from it.
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_has_entity_name = True
    _attr_name = "Degraded"

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_degraded"
        self._attr_device_info = build_device_info(entry)

    @property
    def is_on(self) -> bool:
        # v0.1.15 fix: this used to check only the 6 source coordinators'
        # own last_update_success flags — a coarse, coordinator-level
        # signal that can't see CH1/CH2/D2 individually (they share one
        # coordinator, so one model failing while the others succeed
        # never made the coordinator itself report failure) or a
        # Meteonomiqs failure specifically (that coordinator catches every
        # internal error and always returns normally, so its own
        # last_update_success stays True regardless). Confirmed by an
        # outside code review as a real gap: this sensor could show
        # "not degraded" while a source was genuinely down. Now uses the
        # same per-source health check StatusSensor already correctly
        # uses — every individual source, not just the coordinator
        # wrapping it.
        return any(
            # v0.1.24 fix (IND-03): "no failures yet" was treated as
            # healthy, so a cold start reported "not degraded" before any
            # source had ever succeeded — on the one entity most likely
            # to be wired into an automation. See sensor.is_source_healthy.
            # v0.2.2 fix (SWF-021-007): pass the source name, so the
            # per-source grace periods added in v0.2.1 apply here too.
            # Without it this entity used the default one-hour grace for
            # every source and still reported Degraded for hours after a
            # restart because Meteonomiqs runs once daily — which is the
            # exact symptom SWF-P2-008 was meant to fix, surviving on the
            # one entity most likely to be wired into an automation.
            not is_source_healthy(health, source)
            for source in ALL_TELEMETRY_SOURCES
            if (health := _get_health(self._runtime, source)) is not None
        )
