"""button.<name>_reset_learning — recovery from a poisoned learned state.

v0.2.1. Added after a live installation learned a fabricated -66.8 hPa
pressure bias from a misconfigured sea-level setting (SWF-P1-009).

**Why a button rather than a service action.** This is a recovery
control a user needs to find when something looks wrong, and the
diagnostic section of the device page is where they will already be
looking. A service action works but has to be known about first.

**Why one button rather than one per measurement.** A per-measurement
version was built first and rejected. Resetting only pressure leaves the
learned state at mixed vintages: pressure starting from zero while
temperature and humidity carry history from before the problem was
understood. Bucket confidence then means different things for different
measurements, which is a subtler and longer-lived problem than simply
relearning everything. Consistency of the learned state is worth more
than a few hours of samples.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
# v0.2.5: canonical home since the entity-category move; the
# helpers.entity alias is deprecated.
from homeassistant.const import EntityCategory
# v0.2.5: AddEntitiesCallback is superseded by the
# config-entry-specific callback type.
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
)

from .const import DOMAIN
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ResetLearningButton(entry, runtime)])


class ResetLearningButton(ButtonEntity):
    """Discards Model A's learned state and rebuilds it from stored forecasts.

    Safe, but not free: bias correction is unavailable until buckets pass
    MIN_SAMPLES_TO_TRUST_BUCKET again. Because recent forecasts are
    re-opened for reconciliation rather than discarded, that is usually a
    matter of hours rather than the days a cold start would take.
    """

    _attr_has_entity_name = True
    _attr_name = "Reset learning"
    _attr_icon = "mdi:brain"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # v0.2.2: visible by default.
    #
    # v0.2.1 hid this, reasoning that an always-visible reset invites
    # accidental presses. That was caution applied on the user's behalf
    # to a control they had explicitly asked for, and it meant the
    # recovery button could not be found when it was needed. Anyone
    # placing it on a dashboard should add a confirmation to the tap
    # action; the entity itself no longer hides.
    _attr_entity_registry_enabled_default = True

    def __init__(self, entry: ConfigEntry, runtime: dict[str, Any]) -> None:
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_reset_learning"
        self._attr_device_info = build_device_info(entry)
        self._last_result: Optional[dict[str, int]] = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the last press actually did.

        A button with no feedback leaves the user unable to distinguish a
        successful reset from a no-op, which for a recovery control is
        the difference between "fixed" and "still broken".
        """
        result = self._last_result or {}
        return {
            "last_reset_buckets_cleared": result.get("buckets_cleared"),
            "last_reset_observations_cleared": result.get("observations_cleared"),
            "last_reset_forecasts_reopened": result.get("forecasts_reopened"),
            "effect": (
                "Discards all learned bias and error statistics, clears stored "
                "observations that are physically implausible, and re-opens "
                "recent forecasts for reconciliation so learning rebuilds from "
                "data already held. Raw forecasts and valid observations are "
                "preserved. Use after correcting a sensor misconfiguration."
            ),
        }

    async def async_press(self) -> None:
        db = self._runtime.get("db")
        if db is None:  # pragma: no cover - defensive
            _LOGGER.error("Cannot reset learning: database unavailable")
            return

        # Executor job, not the event loop — the rule every other
        # database access in this integration follows.
        self._last_result = await self.hass.async_add_executor_job(
            db.reset_all_learning
        )

        # Start relearning now rather than up to 20 minutes from now, so
        # the user sees the effect while they are still looking.
        learning = self._runtime.get("learning_coordinator")
        if learning is not None:
            await learning.async_request_refresh()

        self.async_write_ha_state()
