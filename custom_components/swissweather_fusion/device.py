"""Shared device_info so every entity (weather, sensor, binary_sensor)
groups under one device card in the HA UI, instead of a flat list of
entities directly under the integration.

**Fixed in v0.1.2**: this was missing entirely in v0.1/v0.1.1 — no entity
set device_info, so all 42 entities showed as an ungrouped flat list under
the config entry rather than the nested device card HA users expect (the
kind shown by, e.g., weather-fusion-ai's own device grouping).
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "SwissWeather Fusion",
        manufacturer="SwissWeather Fusion (custom integration)",
        model="Fused forecast + storm classifier",
    )
