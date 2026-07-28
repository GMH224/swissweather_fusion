"""SwissWeather Fusion — MeteoSwiss + DWD + SRF + meteoblue + Meteonomiqs
fused into a locally-corrected forecast, plus a summer storm-onset
classifier. See DEVELOPER.md for the full architecture rationale.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ELEVATION_EFFECTIVE,
    CONF_ELEVATION_OVERRIDE,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    CONF_STATION_HUMIDITY_ENTITY,
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_TEMP_ENTITY,
    DB_FILENAME,
    DOMAIN,
)
from .coordinator import (
    CombiPrecipCoordinator,
    MeteoblueCoordinator,
    MeteonomiqsCoordinator,
    ModelBCoordinator,
    OpenMeteoCoordinator,
    SrfCoordinator,
    StationCoordinator,
)
from .storage.db import SwissWeatherDB

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.WEATHER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    latitude = data[CONF_LATITUDE]
    longitude = data[CONF_LONGITUDE]
    elevation_effective = data.get(CONF_ELEVATION_OVERRIDE) or data.get("elevation_looked_up")

    db_path = hass.config.path(f".storage/{DOMAIN}_{entry.entry_id}_{DB_FILENAME}")
    db = await hass.async_add_executor_job(SwissWeatherDB, db_path)

    station_entities = entry.options or {}
    temp_entity = station_entities.get(CONF_STATION_TEMP_ENTITY, data[CONF_STATION_TEMP_ENTITY])
    humidity_entity = station_entities.get(
        CONF_STATION_HUMIDITY_ENTITY, data[CONF_STATION_HUMIDITY_ENTITY]
    )
    pressure_entity = station_entities.get(
        CONF_STATION_PRESSURE_ENTITY, data[CONF_STATION_PRESSURE_ENTITY]
    )

    station_coordinator = StationCoordinator(
        hass, db, temp_entity, humidity_entity, pressure_entity
    )
    open_meteo_coordinator = OpenMeteoCoordinator(hass, db, latitude, longitude)
    srf_coordinator = SrfCoordinator(
        hass,
        db,
        latitude,
        longitude,
        data[CONF_SRF_CONSUMER_KEY],
        data[CONF_SRF_CONSUMER_SECRET],
    )
    meteoblue_coordinator = MeteoblueCoordinator(
        hass, db, latitude, longitude, data[CONF_METEOBLUE_API_KEY]
    )
    combiprecip_coordinator = CombiPrecipCoordinator(hass, db, latitude, longitude)
    meteonomiqs_coordinator = MeteonomiqsCoordinator(
        hass, latitude, longitude, data[CONF_METEONOMIQS_API_KEY]
    )
    model_b_coordinator = ModelBCoordinator(
        hass,
        db,
        station_coordinator,
        combiprecip_coordinator,
        meteoblue_coordinator,
        meteonomiqs_coordinator,
    )

    # First refresh for each. **Fixed in v0.1.1**: this used to be a bare
    # loop where any single coordinator raising (e.g. the SRF crash) would
    # propagate all the way up and fail async_setup_entry entirely — HA
    # then reports the *whole integration* as "failed setup, will retry",
    # even though station/meteoblue/CombiPrecip/etc. would have worked
    # fine. That's exactly the opposite of the graceful-degradation
    # principle this project was designed around (plan doc §6/§12,
    # DEVELOPER.md) — one flaky source should degrade, not take everything
    # down with it. Each coordinator's first refresh is now isolated: a
    # failure is logged clearly and that coordinator simply starts with no
    # data (its entities show unavailable/unknown until its own next
    # scheduled refresh succeeds), rather than blocking setup for sources
    # that are working.
    for coordinator in (
        station_coordinator,
        open_meteo_coordinator,
        srf_coordinator,
        meteoblue_coordinator,
        combiprecip_coordinator,
        meteonomiqs_coordinator,
    ):
        try:
            await coordinator.async_config_entry_first_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Initial refresh failed for %s, continuing setup with the "
                "other sources — this coordinator will retry on its own "
                "schedule: %s",
                coordinator.name,
                err,
            )

    try:
        await model_b_coordinator.async_config_entry_first_refresh()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Initial Model B scoring failed, will retry on its own schedule: %s", err
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "db": db,
        "latitude": latitude,
        "longitude": longitude,
        "elevation_effective": elevation_effective,
        "station_coordinator": station_coordinator,
        "open_meteo_coordinator": open_meteo_coordinator,
        "srf_coordinator": srf_coordinator,
        "meteoblue_coordinator": meteoblue_coordinator,
        "combiprecip_coordinator": combiprecip_coordinator,
        "meteonomiqs_coordinator": meteonomiqs_coordinator,
        "model_b_coordinator": model_b_coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(runtime["db"].close)
    return unloaded
