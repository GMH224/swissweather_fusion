"""SwissWeather Fusion — MeteoSwiss + DWD + SRF + meteoblue + Meteonomiqs
fused into a locally-corrected forecast, plus a summer storm-onset
classifier. See DEVELOPER.md for the full architecture rationale.
"""
from __future__ import annotations

import asyncio
import os
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_STATION_PRESSURE_IS_SEA_LEVEL,
    DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
    CONF_DIAGNOSTIC_LOGGING_ENABLED,
    CONF_ELEVATION_OVERRIDE,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_OPEN_METEO_API_KEY,
    CONF_PURGE_DAYS,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    CONF_STATION_HUMIDITY_ENTITY,
    CONF_STATION_PRESSURE_ENTITY,
    CONF_STATION_TEMP_ENTITY,
    DB_FILENAME,
    DEFAULT_DIAGNOSTIC_LOGGING_ENABLED,
    DIAGNOSTIC_EVENT_BUFFER_SIZE,
    DEFAULT_PURGE_DAYS,
    DOMAIN,
)
from homeassistant.exceptions import ConfigEntryAuthFailed

from .coordinator import (
    StormEventReconciliationCoordinator,
    CombiPrecipCoordinator,
    MeteoblueCoordinator,
    MeteonomiqsCoordinator,
    ModelABlendCoordinator,
    ModelALearningCoordinator,
    ModelBCoordinator,
    OpenMeteoCoordinator,
    RetentionCoordinator,
    SrfCoordinator,
    StationCoordinator,
)
from .diagnostics_recorder import DiagnosticsRecorder
from .storage.db import SCHEMA_VERSION, SwissWeatherDB

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.WEATHER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    # v0.2.1: hosts the diagnostic "Reset learning" recovery control.
    Platform.BUTTON,
]


def _integration_version(hass: HomeAssistant) -> str:
    """The version Home Assistant itself has already loaded for us.

    **v0.1.26 fix.** v0.1.25 read manifest.json with a plain open() from
    async_setup_entry, which is a blocking file read on the event loop —
    Home Assistant detects and warns about exactly this:

        Detected blocking call to open with args
        ('/config/custom_components/swissweather_fusion/manifest.json',)
        inside the event loop

    Embarrassing in a diagnostic helper whose entire purpose is to make
    problems easier to see. It is also unnecessary work: Home Assistant
    parses every custom integration's manifest during startup and keeps
    it in the loader's integration cache, so the value is already in
    memory. `hass.data["integrations"]` is not a public API, hence the
    defensive access and the fallback — but reading a cached value costs
    nothing and touches no disk.
    """
    try:
        integration = hass.data.get("integrations", {}).get(DOMAIN)
        version = getattr(integration, "version", None)
        if version is not None:
            return str(version)
    except Exception:  # noqa: BLE001 - diagnostics must never break setup
        pass
    return "unknown"


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

    # v0.1.25: log the loaded version at INFO on every setup.
    #
    # Added after a v0.1.24 upgrade failure was reported as "still
    # broken" when the fixed files had in fact never been installed —
    # the traceback line numbers were the only way to tell the two
    # builds apart, which is not a reasonable diagnostic burden. Now the
    # log says plainly which build is running, so "did the update
    # actually land" is answerable in one line.
    _LOGGER.info(
        "SwissWeather Fusion %s starting up (database schema v%s)",
        _integration_version(hass),
        SCHEMA_VERSION,
    )

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
    # v0.1.23 fix (L-10): same options-first, data-fallback pattern as
    # every other config value above — purge_days is set once in
    # entry.data during initial setup and can be changed afterward via
    # the options flow.
    purge_days = options.get(CONF_PURGE_DAYS, data.get(CONF_PURGE_DAYS, DEFAULT_PURGE_DAYS))

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

    # v0.1.24 (P1-22): the station coordinator now needs to know both
    # whether the configured pressure entity is already sea-level
    # normalised and the site elevation, so it can reduce a station-level
    # reading before storing it. Resolved options-first like every other
    # setting here.
    pressure_is_sea_level = entry.options.get(
        CONF_STATION_PRESSURE_IS_SEA_LEVEL,
        entry.data.get(
            CONF_STATION_PRESSURE_IS_SEA_LEVEL,
            DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
        ),
    )
    # v0.2.2 fix (SWF-021-012): coordinator CONSTRUCTION is guarded.
    #
    # The pre-existing cleanup path covers the FIRST-REFRESH stage. A
    # constructor raising before that — a bad option value, a client
    # rejecting a credential's shape, an unexpected None — propagated
    # out of async_setup_entry with the SQLite connection still open.
    # Home Assistant then retries setup and opens a SECOND connection
    # to the same file, repeating on every retry until it gives up.
    try:
        station_coordinator = StationCoordinator(
            hass, db, temp_entity, humidity_entity, pressure_entity,
            pressure_is_sea_level=pressure_is_sea_level,
            elevation_m=elevation_effective,
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
            hass, db, latitude, longitude, meteonomiqs_api_key,
            diagnostics=diagnostics_recorder,
        )
        # v0.2.5 (SWF-025-001): the blend is constructed FIRST so Model B
        # can read fused CAPE and convective inhibition from it. The blend
        # has no dependency on Model B, so the order is free to change.
        blend_coordinator = ModelABlendCoordinator(hass, db)
        # v0.1.7: the actual learning step — without this, bucket_stats never
        # gets populated at all, and Model A's blend is only ever an
        # unweighted average of raw forecasts. See coordinator.py for the
        # full story of how this gap was found.
        # v0.1.24 fix (P2-03 / P2-04): ONE lock object, constructed here on
        # the event loop and injected into both coordinators that write
        # forecast_snapshots. Two independently-created locks would serialize
        # each coordinator against itself and nothing against the other,
        # which is precisely the race being closed. Constructed directly
        # rather than via an executor job, unlike the database connection —
        # asyncio.Lock must be created on the loop it will be awaited on.
        shared_learning_lock = asyncio.Lock()
        learning_coordinator = ModelALearningCoordinator(
            hass, db, reconcile_lock=shared_learning_lock
        )
        model_b_coordinator = ModelBCoordinator(
            hass,
            db,
            station_coordinator,
            combiprecip_coordinator,
            meteoblue_coordinator,
            meteonomiqs_coordinator,
            blend_coordinator=blend_coordinator,
            diagnostics=diagnostics_recorder,
        )
        # v0.1.5: computes Model A's current values + hourly/daily/twice-daily
        # forecast in one batched executor job — see coordinator.py for why
        # this replaced logic that used to live directly in weather.py.
        # v0.1.23 fix (L-10): the only caller of db.purge_older_than() — see
        # RetentionCoordinator's docstring in coordinator.py. purge_days=0
        # (the default) makes this coordinator a permanent no-op, matching
        # the documented "0 = forever" meaning of the setting.
        retention_coordinator = RetentionCoordinator(
            hass,
            db,
            purge_days=purge_days,
            retention_lock=shared_learning_lock,
            diagnostics=diagnostics_recorder,
        )
        # v0.1.24 (P2-08): the first-ever writer to storm_events.
        storm_reconciliation_coordinator = StormEventReconciliationCoordinator(
            hass, db, diagnostics=diagnostics_recorder
        )
    except Exception:
        # Nothing is registered yet at this point, so closing the
        # connection is the whole of the required cleanup.
        await hass.async_add_executor_job(db.close)
        raise

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
    # v0.1.24 (P2-12): named before the gather below so the auth-failure
    # cleanup path can shut down EVERY already-constructed coordinator,
    # not only the source ones.
    derived_coordinators_for_cleanup = (
        model_b_coordinator,
        blend_coordinator,
        learning_coordinator,
        retention_coordinator,
        storm_reconciliation_coordinator,
    )
    results = await asyncio.gather(
        *(c.async_config_entry_first_refresh() for c in source_coordinators),
        return_exceptions=True,
    )
    for coordinator, result in zip(source_coordinators, results):
        if isinstance(result, ConfigEntryAuthFailed):
            # v0.1.24 fix (P2-12): a genuinely bad credential entered
            # during initial setup used to be logged and swallowed like
            # any other failure, so setup finished looking successful and
            # no reauth prompt was ever shown. ConfigEntryAuthFailed only
            # triggers Home Assistant's reauth flow when it is RAISED OUT
            # OF async_setup_entry — catching and logging it inside does
            # nothing.
            #
            # This raise happens BEFORE the single pre-existing cleanup
            # try/except further down the file, so without explicit
            # cleanup here it would leak the open database connection and
            # every already-constructed coordinator. Every coordinator is
            # shut down, not just the one that failed.
            _LOGGER.error(
                "Authentication failed for %s during setup — requesting "
                "reauthentication", coordinator.name,
            )
            for started in (*source_coordinators, *derived_coordinators_for_cleanup):
                await started.async_shutdown()
            await hass.async_add_executor_job(db.close)
            raise result
        if isinstance(result, Exception):
            _LOGGER.warning(
                "Initial refresh failed for %s, continuing setup with the "
                "other sources — this coordinator will retry on its own "
                "schedule: %s",
                coordinator.name,
                result,
            )

    derived_coordinators = (
        model_b_coordinator, blend_coordinator, learning_coordinator,
        retention_coordinator, storm_reconciliation_coordinator,
    )
    derived_labels = (
        "Model B scoring", "Model A blend computation",
        "Model A learning reconciliation", "retention purge",
        "storm event reconciliation",
    )
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
        "retention_coordinator": retention_coordinator,
        "storm_reconciliation_coordinator": storm_reconciliation_coordinator,
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
    # v0.1.24 fix (P0-03), CRITICAL: the
    # `entry.async_on_unload(coordinator.async_shutdown)` loop that used
    # to live here has been REMOVED, and async_unload_entry now awaits
    # every coordinator's shutdown explicitly instead.
    #
    # The v0.1.15 fix above was right that shutdowns were missing, but
    # async_on_unload was the wrong mechanism for this particular pairing.
    # Home Assistant fires those callbacks from
    # ConfigEntries.async_unload() AFTER the integration's own
    # async_unload_entry returns — not as part of it. Since
    # async_unload_entry closes the database directly, db.close() ALWAYS
    # ran before any coordinator had actually stopped, leaving a window
    # in which an in-flight or about-to-fire refresh could reach a closed
    # SQLite connection. Every options change triggers a reload, so this
    # sat on a routine path, and it is a plausible source of the
    # intermittent ProgrammingError-shaped symptoms this project has
    # chased before.
    #
    # See _all_coordinators() and async_unload_entry below.

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

    # v0.1.23: retention_coordinator added to this list for the same
    # reason as every coordinator already here — it has no CoordinatorEntity
    # of its own either, so without a listener it would run its one
    # guaranteed first refresh and then never be rescheduled again.
    for coordinator in (
        station_coordinator,
        open_meteo_coordinator,
        srf_coordinator,
        meteoblue_coordinator,
        combiprecip_coordinator,
        meteonomiqs_coordinator,
        model_b_coordinator,
        learning_coordinator,
        retention_coordinator,
        # v0.2.2 fix (SWF-021-005): the storm reconciliation coordinator
        # was constructed, first-refreshed and shut down correctly, but
        # omitted from this loop. DataUpdateCoordinator only schedules
        # its recurring refresh while it has at least one listener, so
        # its 30-minute cycle never ran after the initial refresh —
        # storm_events could still only fill on a restart. The table
        # that the entire Model B v1 plan depends on was, in practice,
        # still not being populated.
        storm_reconciliation_coordinator,
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
            retention_coordinator,
            storm_reconciliation_coordinator,
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


def _all_coordinators(runtime: dict) -> list:
    """Every coordinator in a runtime dict, in shutdown order.

    v0.1.24 (P0-03): the setup-failure cleanup path keeps its own
    explicit tuple rather than calling this, because at that point the
    runtime dict does not exist yet. The two lists must always agree; a
    silent divergence would reintroduce exactly the leak both are there
    to prevent, which is why tests/test_lifecycle.py asserts they match.
    """
    keys = (
        "station_coordinator",
        "open_meteo_coordinator",
        "srf_coordinator",
        "meteoblue_coordinator",
        "combiprecip_coordinator",
        "meteonomiqs_coordinator",
        "model_b_coordinator",
        "blend_coordinator",
        "learning_coordinator",
        "retention_coordinator",
        "storm_reconciliation_coordinator",
    )
    return [runtime[k] for k in keys if k in runtime]


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = hass.data[DOMAIN].pop(entry.entry_id)
        # v0.1.24 fix (P0-03): stop every coordinator FIRST, and await
        # each one, before closing the connection they all hold a
        # reference to. Deterministic ordering here is the entire fix —
        # see the comment in async_setup_entry for why
        # entry.async_on_unload could not provide it.
        for coordinator in _all_coordinators(runtime):
            await coordinator.async_shutdown()
        await hass.async_add_executor_job(runtime["db"].close)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's database when the integration is removed.

    **v0.1.24 fix (IND-05).** No removal handler existed, so removing the
    integration orphaned
    `.storage/{DOMAIN}_{entry_id}_{DB_FILENAME}` — plus its `-wal` and
    `-shm` sidecars — permanently, holding the full station observation
    history and, implicitly, the configured location. Re-adding the
    integration produces a new entry_id and therefore a new database, so
    the old file was not merely undeleted but unreachable.

    Failure to delete is logged rather than raised: Home Assistant has
    already removed the entry by this point, and making removal fail over
    a leftover file would leave the user with no way to complete it.
    """
    # Same path construction as async_setup_entry above — kept
    # deliberately identical rather than factored out, since a divergence
    # would silently delete nothing.
    db_path = hass.config.path(f".storage/{DOMAIN}_{entry.entry_id}_{DB_FILENAME}")

    def _remove() -> None:
        for suffix in ("", "-wal", "-shm"):
            path = db_path + suffix
            try:
                os.remove(path)
            except FileNotFoundError:
                continue
            except OSError as err:
                _LOGGER.warning("Could not remove %s: %s", path, err)

    await hass.async_add_executor_job(_remove)
    _LOGGER.info("Removed SwissWeather Fusion database for entry %s", entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the current version.

    **v0.1.24 fix (IND-05).** SwissWeatherFusionConfigFlow declared
    VERSION = 1 with no migration handler at all, which is fine right up
    until the entry's data shape changes — and this release changes it,
    adding CONF_STATION_PRESSURE_IS_SEA_LEVEL (P1-22). Without a handler,
    Home Assistant refuses to load an entry whose version is lower than
    the flow's, so upgrading would have broken every existing
    installation.

    v1 -> v2 fills in the new key with its default. Existing users are
    asked to confirm it rather than being silently assumed correct — the
    default is "station pressure, needs reduction", which is right for a
    Netatmo absolute-pressure entity but wrong for a normalised one, and
    there is no way to tell from the entity itself. See
    CONF_STATION_PRESSURE_IS_SEA_LEVEL in const.py.
    """
    if entry.version == 1:
        data = dict(entry.data)
        data.setdefault(
            CONF_STATION_PRESSURE_IS_SEA_LEVEL,
            DEFAULT_STATION_PRESSURE_IS_SEA_LEVEL,
        )
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.info(
            "Migrated SwissWeather Fusion config entry to version 2 "
            "(added station pressure reference; please confirm it under "
            "Configure if your station reports sea-level-normalised pressure)"
        )
    return True
