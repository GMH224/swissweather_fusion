"""SwissWeather Fusion — MeteoSwiss + DWD + SRF + meteoblue + Meteonomiqs
fused into a locally-corrected forecast, plus a summer storm-onset
classifier. See DEVELOPER.md for the full architecture rationale.
"""
from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DIAGNOSTIC_LOGGING_ENABLED,
    CONF_ELEVATION_EFFECTIVE,
    CONF_ELEVATION_OVERRIDE,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_OPEN_METEO_API_KEY,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    CONF_STATION_HUMIDITY_ENTITY,
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_TEMP_ENTITY,
    DB_FILENAME,
    DEFAULT_DIAGNOSTIC_LOGGING_ENABLED,
    DIAGNOSTIC_EVENT_BUFFER_SIZE,
    DOMAIN,
)
from .coordinator import (
    CombiPrecipCoordinator,
    MeteoblueCoordinator,
    MeteonomiqsCoordinator,
    ModelABlendCoordinator,
    ModelALearningCoordinator,
    ModelBCoordinator,
    OpenMeteoCoordinator,
    SrfCoordinator,
    StationCoordinator,
)
from .diagnostics_recorder import DiagnosticsRecorder
from .storage.db import SwissWeatherDB

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.WEATHER, Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.data
    latitude = data[CONF_LATITUDE]
    longitude = data[CONF_LONGITUDE]
    # v0.1.15 fix: this used to be `data.get(CONF_ELEVATION_OVERRIDE) or
    # data.get("elevation_looked_up")`, which treats a legitimate 0.0
    # override as falsy and silently falls through to the looked-up value
    # instead — confirmed by an outside code review, same root bug as the
    # config_flow.py write side. Also now checks entry.options first
    # (options-first, data-fallback, same pattern as everything else) —
    # the elevation override field just added to the options flow would
    # otherwise have no actual effect, the same gap already fixed for
    # credentials back in v0.1.2.
    options_first = entry.options or {}
    override = options_first.get(CONF_ELEVATION_OVERRIDE, data.get(CONF_ELEVATION_OVERRIDE))
    elevation_effective = override if override is not None else data.get("elevation_looked_up")

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

    # v0.1.2 fix: credentials were only ever read from entry.data (the
    # initial-setup values), so the options flow's new credential fields
    # (added in the same fix) would have had no actual effect — the
    # options flow writes to entry.options, and this was never checked.
    # Same options-first, data-fallback pattern as the station entities
    # above, for consistency.
    options = entry.options or {}
    srf_consumer_key = options.get(CONF_SRF_CONSUMER_KEY, data[CONF_SRF_CONSUMER_KEY])
    srf_consumer_secret = options.get(CONF_SRF_CONSUMER_SECRET, data[CONF_SRF_CONSUMER_SECRET])
    meteoblue_api_key = options.get(CONF_METEOBLUE_API_KEY, data[CONF_METEOBLUE_API_KEY])
    meteonomiqs_api_key = options.get(CONF_METEONOMIQS_API_KEY, data[CONF_METEONOMIQS_API_KEY])
    # Optional (v0.1.3) — None is a valid, expected value (free tier, the
    # default), not a missing-config error, so .get() with no required
    # fallback to data[...] is correct here unlike the other credentials.
    open_meteo_api_key = options.get(
        CONF_OPEN_METEO_API_KEY, data.get(CONF_OPEN_METEO_API_KEY)
    )

    # v0.1.9: toggleable diagnostic logging — off by default. Since any
    # options change already triggers a full reload (see the update
    # listener below), toggling this naturally recreates the recorder
    # fresh each time, consistent with it being in-memory-only (see
    # diagnostics_recorder.py for why that's a deliberate trade-off, not
    # an oversight).
    diagnostic_logging_enabled = options.get(
        CONF_DIAGNOSTIC_LOGGING_ENABLED, DEFAULT_DIAGNOSTIC_LOGGING_ENABLED
    )
    diagnostics_recorder = DiagnosticsRecorder(max_events=DIAGNOSTIC_EVENT_BUFFER_SIZE)
    diagnostics_recorder.set_enabled(diagnostic_logging_enabled)

    station_coordinator = StationCoordinator(
        hass, db, temp_entity, humidity_entity, pressure_entity,
        diagnostics=diagnostics_recorder,
    )
    open_meteo_coordinator = OpenMeteoCoordinator(
        hass, db, latitude, longitude, api_key=open_meteo_api_key,
        diagnostics=diagnostics_recorder,
        actual_elevation_m=elevation_effective,
    )
    srf_coordinator = SrfCoordinator(
        hass,
        db,
        latitude,
        longitude,
        srf_consumer_key,
        srf_consumer_secret,
        diagnostics=diagnostics_recorder,
    )
    meteoblue_coordinator = MeteoblueCoordinator(
        hass, db, latitude, longitude, meteoblue_api_key,
        diagnostics=diagnostics_recorder,
    )
    combiprecip_coordinator = CombiPrecipCoordinator(
        hass, db, latitude, longitude, diagnostics=diagnostics_recorder
    )
    meteonomiqs_coordinator = MeteonomiqsCoordinator(
        hass, latitude, longitude, meteonomiqs_api_key,
        diagnostics=diagnostics_recorder,
    )
    model_b_coordinator = ModelBCoordinator(
        hass,
        db,
        station_coordinator,
        combiprecip_coordinator,
        meteoblue_coordinator,
        meteonomiqs_coordinator,
    )
    # v0.1.5: computes Model A's current values + hourly/daily/twice-daily
    # forecast in one batched executor job — see coordinator.py for why
    # this replaced logic that used to live directly in weather.py.
    blend_coordinator = ModelABlendCoordinator(hass, db)
    # v0.1.7: the actual learning step — without this, bucket_stats never
    # gets populated at all, and Model A's blend is only ever an
    # unweighted average of raw forecasts. See coordinator.py for the
    # full story of how this gap was found.
    learning_coordinator = ModelALearningCoordinator(hass, db)

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
    #
    # v0.1.14 fix: this used to be a strictly sequential for-loop —
    # await'ing all six source coordinators' first refreshes one after
    # another. An outside code review flagged this directly: nine
    # coordinators' worth of sequential awaits (some involving multiple
    # HTTP calls, like SRF's token+geolocation+forecast sequence) could
    # make async_setup_entry itself take long enough to risk Home
    # Assistant's own setup-timeout handling — and critically, this issue
    # would be essentially unique to an integration with this many
    # coordinators, which fits the reported symptom of only this
    # integration (not others) freezing. Now: the six source coordinators
    # run concurrently via asyncio.gather, then the three coordinators
    # that depend on the sources' data (Model B reads combiprecip's data
    # directly; blend/learning read what the sources already wrote to the
    # database) run as a second concurrent group — preserving the real
    # dependency order between the two groups while making each group's
    # own execution concurrent rather than fully sequential.
    source_coordinators = (
        station_coordinator,
        open_meteo_coordinator,
        srf_coordinator,
        meteoblue_coordinator,
        combiprecip_coordinator,
        meteonomiqs_coordinator,
    )
    results = await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in source_coordinators),
        return_exceptions=True,
    )
    for coordinator, result in zip(source_coordinators, results):
        if isinstance(result, Exception):
            _LOGGER.warning(
                "Initial refresh failed for %s, continuing setup with the "
                "other sources — this coordinator will retry on its own "
                "schedule: %s",
                coordinator.name,
                result,
            )

    derived_coordinators = (model_b_coordinator, blend_coordinator, learning_coordinator)
    derived_labels = ("Model B scoring", "Model A blend computation", "Model A learning reconciliation")
    derived_results = await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in derived_coordinators),
        return_exceptions=True,
    )
    for label, result in zip(derived_labels, derived_results):
        if isinstance(result, Exception):
            _LOGGER.warning(
                "Initial %s failed, will retry on its own schedule: %s", label, result
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
        "blend_coordinator": blend_coordinator,
        "learning_coordinator": learning_coordinator,
        "diagnostics_recorder": diagnostics_recorder,
    }

    # v0.1.15 fix — critical resource-lifecycle gap found in a clean,
    # independent review: nothing ever registered a shutdown for any of
    # the 9 coordinators. A coordinator's periodic refresh isn't
    # automatically tied to the entities that read it — Home Assistant's
    # own documented pattern is `entry.async_on_unload(coordinator.async_shutdown)`
    # per coordinator, which is what actually cancels its scheduled
    # refresh. Without this, every reload of this integration (any
    # options change, every redeploy during this project's own extensive
    # debugging) could have left the *previous* set of coordinators still
    # running in the background — holding a reference to an
    # already-closed database connection — while a brand new set also
    # started, all sharing the same underlying executor pool. That's a
    # plausible contributor to some of the confusing, hard-to-reproduce
    # symptoms seen throughout this project, though it can't be confirmed
    # in hindsight without reproducing the exact failure.
    for coordinator in (
        station_coordinator,
        open_meteo_coordinator,
        srf_coordinator,
        meteoblue_coordinator,
        combiprecip_coordinator,
        meteonomiqs_coordinator,
        model_b_coordinator,
        blend_coordinator,
        learning_coordinator,
    ):
        entry.async_on_unload(coordinator.async_shutdown)

    # v0.1.16 fix — very likely the actual root cause of the reported
    # multi-hour freeze, found from a third outside review and confirmed
    # directly against Home Assistant's own source history (a core PR
    # titled "Only schedule a refresh if listeners", changing
    # DataUpdateCoordinator to stop automatically rescheduling itself once
    # it has zero registered listeners — this was intentional, added to
    # prevent a coordinator nobody reads from polling forever). Checked
    # directly against this project's own entities: CoordinatorEntity is
    # used in exactly two places (the weather entity and
    # ExpertWeightSensor), and both are tied to blend_coordinator only.
    # Every other coordinator here — station, open_meteo, srf, meteoblue,
    # combiprecip, meteonomiqs, model_b, learning — has never had a single
    # registered listener. That's an exact match for the observed
    # pattern: one guaranteed first refresh via
    # async_config_entry_first_refresh(), then nothing, forever, for
    # every one of them simultaneously — because Home Assistant's own
    # coordinator framework had no reason to reschedule any of them.
    # Every previous fix attempt (the SQLite lock, the query-count
    # reduction, the HTTP/coordinator timeouts) addressed real, separate
    # problems, but none of them touched this — a coordinator that isn't
    # even being asked to run again isn't helped by making its own run
    # faster or safer.
    #
    # Fixed with a genuine (if functionally no-op) listener registered for
    # every coordinator lacking one, removed cleanly on unload. Sensors
    # reading these coordinators' data still do so via plain attribute
    # access (not a CoordinatorEntity conversion) — a larger refactor
    # deferred in favor of this minimal, direct, low-risk fix for the
    # actual scheduling problem.
    def _noop() -> None:
        return None

    for coordinator in (
        station_coordinator,
        open_meteo_coordinator,
        srf_coordinator,
        meteoblue_coordinator,
        combiprecip_coordinator,
        meteonomiqs_coordinator,
        model_b_coordinator,
        learning_coordinator,
    ):
        entry.async_on_unload(coordinator.async_add_listener(_noop))

    # v0.1.15 fix — the shutdown registrations above only fire when Home
    # Assistant unloads this entry normally; they do nothing if setup
    # itself fails partway through. Without this, a failure in
    # async_forward_entry_setups (all 9 coordinators already started and
    # refreshed by this point) would leave those coordinators running
    # with no cleanup, while Home Assistant retries the whole setup from
    # scratch — a second full set of coordinators, sharing the same
    # database path as the first.
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        for coordinator in (
            station_coordinator,
            open_meteo_coordinator,
            srf_coordinator,
            meteoblue_coordinator,
            combiprecip_coordinator,
            meteonomiqs_coordinator,
            model_b_coordinator,
            blend_coordinator,
            learning_coordinator,
        ):
            await coordinator.async_shutdown()
        await hass.async_add_executor_job(db.close)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    # v0.1.2 fix: nothing reloaded the integration when options changed —
    # station sensor edits or credential updates via Configure would sit
    # in entry.options unused until a manual restart. This is the
    # standard HA pattern: any options-flow save triggers a full reload,
    # so changes actually take effect.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(runtime["db"].close)
    return unloaded
