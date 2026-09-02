"""Coordinators for SwissWeather Fusion.

Home Assistant's DataUpdateCoordinator is built around one polling interval
per instance — this project has several genuinely different cadences
(continuous 5-min CombiPrecip, metadata-driven CH1/CH2/D2, a seasonal
meteoblue schedule, a daily Meteonomiqs keep-alive plus event-triggered
bonus calls), so this file has several coordinator classes rather than one,
each owning the schedule that actually fits its source. See DEVELOPER.md
for the full per-source reasoning; this file is the wiring, not the "why".

All blocking storage calls go through hass.async_add_executor_job() —
storage/db.py is deliberately synchronous and framework-independent (see
its own docstring), so this is the one place that bridges it to HA's event
loop.
"""
from __future__ import annotations

import asyncio
import math
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .clients.combiprecip import CombiPrecipClient
from .clients.meteoblue import BonusCallTracker, MeteoblueClient, should_fire_scheduled_call
from .clients.meteonomiqs import AnnualCallBudget, MeteonomiqsClient, needs_keepalive_call
from .clients.open_meteo import OpenMeteoClient
from .clients.srf import SrfClient
from .health import SourceHealth, classify_exception
from .const import (
    METEOBLUE_ANNUAL_CALL_BUDGET,
    METEOBLUE_SCHEDULED_RETRY_COOLDOWN,
    METEONOMIQS_NOWCAST_TARGET_WINDOW,
    STORM_FOLLOW_UP_WINDOW,
    STORM_RECONCILIATION_INTERVAL,
    STORM_RECONCILIATION_MIN_PROBABILITY,
    V0_PRESSURE_DROP_HPA_THRESHOLD,
    RADAR_PRECIP_ACCUM_MM_THRESHOLD,
    ALL_FORECAST_SOURCES,
    METEONOMIQS_ANNUAL_CALL_BUDGET,
    METEONOMIQS_FORECAST_CALL_HOUR_LOCAL,
    METEONOMIQS_FORECAST_SEASON_MONTHS,
    METEONOMIQS_HOURLY_VARIABLE_PREFIX,
    METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
    METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT,
    MODEL_B_SCORING_INTERVAL,
    OPEN_METEO_CHECK_INTERVAL,
    RETENTION_CHECK_INTERVAL,
    SOURCE_CH1,
    SOURCE_CH2,
    SOURCE_ICON_D2,
    SRF_POLL_INTERVAL,
    STATION_POLL_INTERVAL,
    STORM_PREDICTION_UPPER_CROSSING_THRESHOLD,
    UPWIND_BEARING_DEGREES,
    UPWIND_DISTANCES_KM,
    UPWIND_POINT_LABELS,
)
from .models import model_b
from .redaction import redact_secret_values
from . import provider_validation, unit_conversion
from .storage.db import SwissWeatherDB

_LOGGER = logging.getLogger(__name__)


class OpenMeteoCoordinator(DataUpdateCoordinator):
    """Handles CH1, CH2, and ICON-D2 — one coordinator since they share the
    same API and the same "check before fetching" logic. Polls frequently
    (OPEN_METEO_CHECK_INTERVAL) but only actually performs a forecast fetch
    for a given model when that model's own run schedule suggests fresh
    data should be available — see DEVELOPER.md for why a fixed buffer was
    replaced with this check.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        api_key: Optional[str] = None,
        *,
        diagnostics: Any = None,
        actual_elevation_m: Optional[float] = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_open_meteo",
            update_interval=OPEN_METEO_CHECK_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        # v0.1.15 fix: apply_lapse_rate_precorrection existed and was
        # tested since early in this project, but nothing ever called it
        # — confirmed by an outside code review as unused configuration.
        # Applied here specifically, to Open-Meteo's temperature values,
        # since Open-Meteo's response confirmed includes the model grid
        # cell's own elevation as a top-level field — the one piece of
        # data the correction actually needs and the only source this
        # project has confirmed elevation data for. Not applied to
        # SRF/meteoblue/Meteonomiqs, since their responses' own grid/
        # station elevation isn't currently captured.
        self._actual_elevation_m = actual_elevation_m
        # v0.1.24 (P1-03): retained rather than constructed-and-discarded,
        # so exception text can be scrubbed of it before it reaches a log
        # or a diagnostics record.
        self._api_key = api_key
        self._client = OpenMeteoClient(async_get_clientsession(hass), api_key=api_key)
        self._last_issued_at: dict[str, datetime] = {}
        # v0.1.19 fix (DEF-02): issued_at alone couldn't detect an
        # unchanged upstream run (see open_meteo.py's docstring) — this
        # tracks the last actually-stored run's content fingerprint per
        # source so a repeated identical poll can be recognized and
        # skipped, the way the dedup check was always meant to behave.
        #
        # v0.1.23 fix (L-06): this in-memory cache is now only a
        # same-session fast path — the durable source of truth is
        # SwissWeatherDB.get/set_provider_run_fingerprint(). Previously
        # this dict was the ONLY copy of that state, reset to empty on
        # every Home Assistant restart/reload, so a restart made an
        # unchanged upstream run look "new" again and re-store it. Each
        # source's entry is lazily loaded from the DB the first time it's
        # needed after (re)start — see _get_persisted_fingerprint below.
        self._last_run_fingerprint: dict[str, Optional[str]] = {}
        self._fingerprint_loaded_from_db: set[str] = set()
        # One health tracker per model, not one for the whole coordinator —
        # CH1 can fail while CH2/D2 succeed (e.g. a MeteoSwiss-side issue
        # specific to one model), and that distinction is exactly what
        # makes per-source diagnostics useful rather than just knowing
        # "Open-Meteo is having a bad day".
        self.health: dict[str, SourceHealth] = {
            SOURCE_CH1: SourceHealth(),
            SOURCE_CH2: SourceHealth(),
            SOURCE_ICON_D2: SourceHealth(),
        }

    def _secret_values(self) -> list[str]:
        """Values that must never appear in a log line or diagnostics
        record for this coordinator (v0.1.24, P1-03)."""
        return [v for v in (self._api_key,) if v]

    async def _get_persisted_fingerprint(self, source: str) -> Optional[str]:
        """v0.1.23 fix (L-06): lazily loads this source's last-stored run
        fingerprint from durable storage exactly once per coordinator
        lifetime (i.e. once per HA restart/reload), then relies on the
        in-memory cache for every subsequent cycle — avoiding a DB round
        trip on every single poll while still surviving a restart."""
        if source not in self._fingerprint_loaded_from_db:
            persisted = await self.hass.async_add_executor_job(
                self._db.get_provider_run_fingerprint, source
            )
            if persisted is not None:
                self._last_run_fingerprint[source] = persisted
            self._fingerprint_loaded_from_db.add(source)
        return self._last_run_fingerprint.get(source)

    async def _async_update_data(self) -> dict[str, Any]:
        from .models import model_a

        results: dict[str, Any] = {}
        # v0.1.24 (P1-01): tracked across the loop, raised only after it.
        auth_failure: Optional[str] = None
        for source in (SOURCE_CH1, SOURCE_CH2, SOURCE_ICON_D2):
            start = time.monotonic()
            try:
                # v0.1.14: an outer backstop timeout, per source — same
                # defense-in-depth reasoning as SRF's existing one (v0.1.6),
                # applied here after an outside code review confirmed most
                # coordinators had no equivalent protection at all.
                async with asyncio.timeout(60):
                    parsed = await self._client.async_fetch_forecast(
                        source=source, latitude=self._latitude, longitude=self._longitude
                    )
            except Exception as err:  # noqa: BLE001
                duration_ms = (time.monotonic() - start) * 1000
                kind = self.health[source].record_error(err, duration_ms=duration_ms)
                # v0.1.24 fix (P1-03): aiohttp's exception str() includes
                # the full request URL, and this client embeds the API
                # key directly in that URL (&apikey=...). Logging the raw
                # exception therefore wrote the real key into Home
                # Assistant's core log at WARNING level, where it is
                # readable by anyone with log access and gets included in
                # pasted troubleshooting output.
                safe_err = redact_secret_values(str(err), secrets=self._secret_values())
                _LOGGER.warning(
                    "Open-Meteo fetch failed for %s (%s error): %s", source, kind, safe_err
                )
                if self._diagnostics is not None:
                    self._diagnostics.record(
                        source=source, event_type="poll_failure", detail=safe_err
                    )
                if kind == "auth":
                    auth_failure = safe_err
                continue
            duration_ms = (time.monotonic() - start) * 1000
            self.health[source].record_success(duration_ms=duration_ms)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source=source, event_type="poll_success",
                    detail=f"{len(parsed.points)} points",
                )

            # v0.1.19 fix (DEF-02): previously compared parsed.issued_at
            # to the last stored issued_at, but issued_at is always
            # datetime.now(timezone.utc) — it only ever increases, so
            # that comparison could essentially never suppress a repeat
            # poll of an unchanged upstream run. Now compares a content
            # fingerprint of the actual returned series instead (see
            # open_meteo.py's parse_forecast_response), which is stable
            # across polls when nothing has actually changed upstream.
            previous_fingerprint = await self._get_persisted_fingerprint(source)
            if (
                previous_fingerprint is not None
                and parsed.run_fingerprint == previous_fingerprint
            ):
                # No new run since last successful fetch — nothing to store.
                continue
            self._last_issued_at[source] = parsed.issued_at

            if parsed.array_length_mismatches and self._diagnostics is not None:
                # v0.1.19 fix: surface Open-Meteo array-length mismatches
                # (a variable's value array shorter/longer than the time
                # axis, previously silently truncated by zip()) instead of
                # letting them pass with no trace anywhere.
                self._diagnostics.record(
                    source=source, event_type="parse_warning",
                    detail=(
                        "hourly array length mismatch for: "
                        + ", ".join(parsed.array_length_mismatches)
                    ),
                )
            if parsed.array_length_mismatches:
                _LOGGER.warning(
                    "Open-Meteo %s: hourly array length mismatch for %s — "
                    "the shorter array's tail was truncated silently by "
                    "design (see open_meteo.py), this is just visibility.",
                    source,
                    ", ".join(parsed.array_length_mismatches),
                )

            # v0.1.15 fix: wires apply_lapse_rate_precorrection into the
            # actual blend path — see __init__'s comment for the full
            # story. Only applied to temperature, and only when both the
            # grid's own elevation (from this response) and the
            # configured actual elevation are known; otherwise values
            # pass through unchanged, same as before this fix existed.
            grid_elevation = parsed.grid_elevation_m
            apply_correction = (
                grid_elevation is not None and self._actual_elevation_m is not None
            )
            rows = []
            for point in parsed.points:
                value = point.value
                if apply_correction and point.variable == "temperature" and value is not None:
                    value = model_a.apply_lapse_rate_precorrection(
                        raw_temperature=value,
                        source_grid_elevation_m=grid_elevation,
                        actual_elevation_m=self._actual_elevation_m,
                    )
                rows.append(
                    (
                        source,
                        parsed.issued_at.isoformat(),
                        point.valid_at.isoformat(),
                        point.variable,
                        value,
                        "scheduled",
                    )
                )
            # v0.1.24 fix (P1-23): provider-independent physical-bounds
            # and finite check, applied to every provider immediately
            # before storage. See provider_validation.py.
            rows, rejected = provider_validation.validate_forecast_rows(rows)
            if rejected and self._diagnostics is not None:
                self._diagnostics.record(
                    source=source, event_type="validation_rejected",
                    detail=f"{rejected} value(s) outside physical bounds",
                )

            await self.hass.async_add_executor_job(
                self._db.insert_forecast_snapshots_bulk, rows
            )

            # v0.1.24 fix (P0-04), CRITICAL: the fingerprint is now
            # recorded ONLY AFTER the rows are durably stored.
            #
            # It used to be set — both in the in-memory cache and in the
            # database — before insert_forecast_snapshots_bulk ran. The
            # external audit described this as a crash window, but the
            # in-memory half makes it worse than that: an ordinary insert
            # failure (a transient SQLite error, a full disk, a
            # connection closed by the P0-03 unload race) was enough. The
            # run was then treated as already-processed for the rest of
            # the process lifetime, and permanently after restart because
            # the fingerprint had also been persisted. A complete
            # provider run vanished with no error surfaced anywhere.
            #
            # Ordering alone closes it: if the insert raises, neither the
            # cache nor the database is updated, so the next cycle
            # re-attempts the same run.
            self._last_run_fingerprint[source] = parsed.run_fingerprint
            if parsed.run_fingerprint is not None:
                await self.hass.async_add_executor_job(
                    self._db.set_provider_run_fingerprint, source, parsed.run_fingerprint
                )
            results[source] = parsed

        # v0.1.24 fix (P1-01): if EVERY source failed and at least one of
        # those failures was an authentication problem, surface it as
        # ConfigEntryAuthFailed so Home Assistant actually starts its
        # reauth flow. Raised only after the loop and only when nothing
        # usable came back, which preserves the existing per-source fault
        # tolerance for the common case where just one paid-tier key is
        # bad and the free sources still work.
        if auth_failure is not None and not results:
            raise ConfigEntryAuthFailed(
                f"Open-Meteo authentication failed: {auth_failure}"
            ) from None
        return results


class SrfCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        consumer_key: str,
        consumer_secret: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_srf",
            update_interval=SRF_POLL_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = SrfClient(
            async_get_clientsession(hass),
            consumer_key,
            consumer_secret,
            diagnostics=diagnostics,
            latitude=latitude,
            longitude=longitude,
        )
        self.health = SourceHealth()

    async def _async_update_data(self) -> list[Any]:
        from .clients.srf import SrfPermanentError
        from .fingerprint import fingerprint_points

        start = time.monotonic()
        if self._diagnostics is not None:
            self._diagnostics.record(source="srf", event_type="poll_start", detail="polling")
        used_fallback = False
        try:
            # v0.1.6: an outer backstop timeout, in addition to the
            # per-request timeouts added in the client itself — belt and
            # suspenders against a hang happening somewhere other than
            # the three explicit HTTP calls (e.g. during the token cache
            # check, or a retry loop), given the whole point is to never
            # again see a coordinator silently stop updating with no
            # error recorded.
            async with asyncio.timeout(60):
                try:
                    # v0.1.18: the confirmed-working v2/forecastpoint
                    # endpoint is now the primary fetch — genuine hourly
                    # data, not just daily. Falls back to the old
                    # daily-only endpoint below if this fails for a
                    # TRANSIENT reason; better to have some data than
                    # none, and SRF's API has surprised this project
                    # enough times that a graceful fallback is worth
                    # keeping rather than removing the old code path
                    # entirely.
                    points = await self._client.async_fetch_forecastpoint(
                        latitude=self._latitude, longitude=self._longitude
                    )
                except SrfPermanentError:
                    # v0.1.23 fix (L-11): a permanent 4xx (account/plan
                    # restriction, bad request, etc.) is NOT
                    # fallback-eligible — the daily endpoint uses the
                    # same auth and the same account, so it has no
                    # reason to succeed where the primary endpoint was
                    # permanently rejected. Falling back here used to
                    # mean every single poll wasted a second request on
                    # an endpoint that structurally cannot produce a
                    # usable "temperature" measurement anyway (see
                    # clients/srf.py's fallback docstring), while hiding
                    # the real, permanent cause behind what looked like
                    # ordinary degraded-but-functioning operation.
                    # Re-raised as-is; the outer except below classifies
                    # and records it same as any other failure.
                    raise
                except Exception as primary_err:  # noqa: BLE001
                    _LOGGER.warning(
                        "SRF v2/forecastpoint fetch failed, falling back to "
                        "the daily-only endpoint: %s",
                        primary_err,
                    )
                    # v0.1.20 fix: this failure was only ever logged to
                    # HA's own log, never recorded as a diagnostics event
                    # — so a downloaded diagnostics file could show 100%
                    # of polls silently landing on the fallback endpoint
                    # with no way to see *why* without separately pulling
                    # HA's core log too. Found investigating exactly that
                    # scenario: expert_weight_srf stuck Unknown because
                    # every single poll (6/6 observed) was landing on the
                    # daily fallback, which structurally can't produce a
                    # "temperature" measurement Model A reconciles against
                    # (see clients/srf.py's _DAY_FIELD_MAP — the fallback
                    # only ever produces temperature_daily_max/min, never
                    # plain "temperature").
                    if self._diagnostics is not None:
                        self._diagnostics.record(
                            source="srf", event_type="forecastpoint_fallback",
                            detail=f"primary forecastpoint fetch failed: {primary_err}",
                        )
                    used_fallback = True
                    points = await self._client.async_fetch_forecast(
                        latitude=self._latitude, longitude=self._longitude
                    )
        except Exception as err:  # noqa: BLE001
            duration_ms = (time.monotonic() - start) * 1000
            kind = self.health.record_error(err, duration_ms=duration_ms)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="srf", event_type="poll_failure",
                    detail=f"{kind} error: {err}",
                )
            if kind == "auth":
                # v0.1.24 fix (P1-01): this branch used to only LOG, then
                # fall through to the generic UpdateFailed below — which
                # Home Assistant's config-entry framework does not
                # recognise as an authentication problem, so no reauth
                # flow was ever offered and the integration degraded
                # silently and indefinitely on a revoked or rotated key.
                #
                # Note on the audit's stated evidence: it claimed the
                # UpdateFailed wrapper hid the HTTP status from
                # classify_exception. That is not the case —
                # health.record_error(err) above runs on the ORIGINAL
                # exception, so classification and diagnostics were
                # always correct. The only thing missing was the raise
                # Home Assistant actually reacts to.
                #
                # `from None` rather than `from err` (P1-03): the
                # original exception's str() can contain a credential,
                # and __cause__ would resurface it in any logged
                # traceback regardless of what this message says.
                _LOGGER.error(
                    "SRF authentication failed — credentials likely need "
                    "to be re-entered (reauth flow): %s",
                    err,
                )
                raise ConfigEntryAuthFailed(
                    f"SRF authentication failed ({kind} error)"
                ) from None
            elif isinstance(err, SrfPermanentError):
                _LOGGER.error(
                    "SRF request permanently rejected (HTTP %d) — not an "
                    "auth failure, but not transient either. See the error "
                    "detail for the likely account/API-plan cause: %s",
                    err.status,
                    err,
                )
            raise UpdateFailed(f"SRF fetch failed ({kind} error): {err}") from None
        duration_ms = (time.monotonic() - start) * 1000
        self.health.record_success(duration_ms=duration_ms)

        # v0.1.23 fix (L-04's practical concern): dedupe against the
        # persisted content fingerprint of the last successfully stored
        # SRF run, same mechanism as Open-Meteo (L-06) and Meteoblue
        # (L-05) — see fingerprint.py's module docstring. An unchanged
        # SRF response (e.g. two polls landing on the same underlying
        # SRF model run within the 45-minute poll interval) no longer
        # creates a duplicate set of forecast_snapshots training rows.
        run_fingerprint = fingerprint_points(points)
        previous_fingerprint = await self.hass.async_add_executor_job(
            self._db.get_provider_run_fingerprint, "srf"
        )
        is_duplicate_run = points and run_fingerprint == previous_fingerprint
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="srf", event_type="poll_success",
                detail=(
                    f"{len(points)} points"
                    + (" (fallback endpoint)" if used_fallback else "")
                    + (" (duplicate run, not stored)" if is_duplicate_run else "")
                ),
                extra={
                    "point_count": len(points),
                    "used_fallback": used_fallback,
                    "duplicate_run": is_duplicate_run,
                },
            )

        if is_duplicate_run:
            return points

        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            ("srf", now_iso, p.valid_at.isoformat(), p.variable, p.value, "scheduled")
            for p in points
        ]
        # v0.1.24 (P1-23): shared physical-bounds validation.
        rows, rejected = provider_validation.validate_forecast_rows(rows)
        if rejected and self._diagnostics is not None:
            self._diagnostics.record(
                source="srf", event_type="validation_rejected",
                detail=f"{rejected} value(s) outside physical bounds",
            )
        await self.hass.async_add_executor_job(self._db.insert_forecast_snapshots_bulk, rows)
        if points:
            await self.hass.async_add_executor_job(
                self._db.set_provider_run_fingerprint, "srf", run_fingerprint
            )
        return points


class MeteoblueCoordinator(DataUpdateCoordinator):
    """Seasonal schedule (Mar-Oct vs Nov-Feb, both 3 calls/day) plus the
    one-bonus-call-per-storm-scenario allowance from the cross-model
    trigger. Polls every few minutes just to *check* whether it's a
    scheduled slot or a bonus call is due — most checks do nothing, which
    is the intended, credit-neutral behavior (see DEVELOPER.md).
    """

    CHECK_INTERVAL = timedelta(minutes=5)

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        api_key: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_meteoblue",
            update_interval=self.CHECK_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        # v0.1.24 (P1-03): see OpenMeteoCoordinator.
        self._api_key = api_key
        self._client = MeteoblueClient(async_get_clientsession(hass), api_key)
        # v0.1.23 fix (L-08): these three are still constructed with
        # empty/default in-memory state here — that part is unchanged and
        # intentional (constructing them doesn't require I/O). What
        # changed is that _async_load_persisted_state_if_needed() below
        # now overlays any DB-persisted state onto them once per
        # coordinator lifetime, so a restart no longer silently resets a
        # same-day bonus-call count or forgets an already-serviced
        # scheduled slot.
        self._bonus_tracker = BonusCallTracker()
        self._last_scheduled_call_hour: Optional[datetime] = None
        # v0.1.24 fix (P1-09): tracks the last ATTEMPT, success or
        # failure, separately from the success-only marker above.
        # _last_scheduled_call_hour was only ever set on success, so a
        # failing scheduled call re-entered the call path on every
        # 5-minute poll for the rest of that hour — up to ~12 attempts,
        # each spending a real API credit against the annual ceiling.
        self._last_scheduled_attempt_at: Optional[datetime] = None
        # v0.1.24 fix (P1-06): meteoblue had per-day and per-event caps
        # but nothing bounding the annual total. Reuses the
        # AnnualCallBudget class already built for Meteonomiqs rather
        # than growing a second implementation of the same idea.
        # NOTE the positional argument. AnnualCallBudget's parameter is
        # `annual_budget`, not `max_calls_per_year` — v0.1.25 shipped with
        # the wrong keyword and took setup down with a TypeError at
        # construction time, because no test ever called this constructor.
        # See tests/test_v0_1_26_construction.py.
        self._annual_budget = AnnualCallBudget(METEOBLUE_ANNUAL_CALL_BUDGET)
        self._state_loaded_from_db = False
        self.health = SourceHealth()

    async def _async_load_persisted_state_if_needed(self) -> None:
        """v0.1.23 fix (L-08): loads bonus-tracker and last-scheduled-hour
        state from durable storage exactly once per coordinator lifetime
        (i.e. once per HA restart/reload). Previously both of these lived
        only as plain instance attributes — reset to their empty defaults
        on every restart — meaning a restart could forget same-day bonus
        usage already spent (letting the daily allowance be exceeded
        across a reload) and forget the already-serviced scheduled slot
        (risking a duplicate provider call for the same slot right after
        the reload)."""
        if self._state_loaded_from_db:
            return
        bonus_state = await self.hass.async_add_executor_job(
            self._db.get_bonus_call_tracker_state, "meteoblue"
        )
        if bonus_state is not None:
            self._bonus_tracker = BonusCallTracker.from_state(bonus_state)
        # v0.1.24 (P1-06): restore the new annual budget the same way.
        annual_state = await self.hass.async_add_executor_job(
            self._db.get_annual_call_budget_state, "meteoblue"
        )
        if annual_state is not None:
            self._annual_budget.load_state(annual_state)
        last_hour_iso = await self.hass.async_add_executor_job(
            self._db.get_last_scheduled_call_hour, "meteoblue"
        )
        if last_hour_iso is not None:
            self._last_scheduled_call_hour = datetime.fromisoformat(last_hour_iso)
        self._state_loaded_from_db = True

    def _secret_values(self) -> list[str]:
        """v0.1.24 (P1-03) — see OpenMeteoCoordinator._secret_values."""
        return [v for v in (self._api_key,) if v]

    async def _async_persist_annual_budget_state(self) -> None:
        """v0.1.24 (P1-06): persist the annual counter the same way the
        bonus tracker already is, so it survives restarts (the L-07 fix
        applied to meteoblue's new budget)."""
        state = self._annual_budget.to_state()
        await self.hass.async_add_executor_job(
            self._db.set_annual_call_budget_state,
            "meteoblue",
            state["year"],
            state["calls_used"],
        )

    async def _async_persist_bonus_tracker_state(self) -> None:
        await self.hass.async_add_executor_job(
            self._db.set_bonus_call_tracker_state, "meteoblue", self._bonus_tracker.to_state()
        )

    async def async_request_bonus_call(self) -> bool:
        """Called by the cross-model trigger (see ModelBCoordinator). Returns
        True if a bonus call was actually made, False if the daily
        allowance was already used.
        """
        await self._async_load_persisted_state_if_needed()
        today = datetime.now(timezone.utc).date()
        # v0.1.15 fix: reserves the slot atomically before the fetch, not
        # after — the original race window was specifically the await
        # below (the HTTP call), where a second concurrent trigger could
        # pass the same can_use_bonus_call check before either recorded
        # usage. This does mean a failed fetch still counts against the
        # daily allowance rather than being refunded — a deliberate,
        # simpler trade-off given how rare and already-protected (by the
        # calling coordinator's own overlap protection) this path is.
        if not self._bonus_tracker.try_use_bonus_call(today=today):
            return False
        # v0.1.23 fix (L-08): persist immediately after reserving the
        # slot, not just in memory — a restart between this point and the
        # next scheduled poll must not forget that this call already
        # counted against today's allowance.
        await self._async_persist_bonus_tracker_state()
        await self._async_fetch_and_store(trigger_reason="storm_trigger")
        return True

    async def _async_fetch_and_store(self, *, trigger_reason: str) -> None:
        start = time.monotonic()
        # v0.1.14: same defense-in-depth backstop added to every other
        # coordinator — see OpenMeteoCoordinator's comment for the reason.
        async with asyncio.timeout(60):
            parsed = await self._client.async_fetch_forecast(
                latitude=self._latitude, longitude=self._longitude
            )
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)

        # v0.1.23 fix (L-05): meteoblue previously had NO dedup mechanism
        # at all — every scheduled or bonus poll was inserted
        # unconditionally, even if the upstream model run hadn't actually
        # changed. Same persisted-fingerprint mechanism as Open-Meteo
        # (L-06) and SRF (L-04's practical fix), see fingerprint.py.
        previous_fingerprint = await self.hass.async_add_executor_job(
            self._db.get_provider_run_fingerprint, "meteoblue"
        )
        is_duplicate_run = bool(parsed.points) and parsed.run_fingerprint == previous_fingerprint
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteoblue", event_type="poll_success",
                detail=(
                    f"{len(parsed.points)} points ({trigger_reason})"
                    + (" (duplicate run, not stored)" if is_duplicate_run else "")
                ),
            )
        if is_duplicate_run:
            return
        rows = [
            (
                "meteoblue",
                parsed.issued_at.isoformat(),
                p.valid_at.isoformat(),
                p.variable,
                p.value,
                trigger_reason,
            )
            for p in parsed.points
        ]
        # v0.1.24 fix (P1-24): surface an array-length mismatch instead
        # of letting zip() truncate silently. Open-Meteo has done this
        # since v0.1.19; meteoblue had no equivalent.
        if parsed.array_length_mismatches:
            _LOGGER.warning(
                "meteoblue: hourly array length mismatch for %s — the longer "
                "array's tail was truncated (see clients/meteoblue.py)",
                ", ".join(parsed.array_length_mismatches),
            )
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteoblue", event_type="parse_warning",
                    detail=(
                        "hourly array length mismatch for: "
                        + ", ".join(parsed.array_length_mismatches)
                    ),
                )

        # v0.1.24 (P1-23): shared physical-bounds validation.
        rows, rejected = provider_validation.validate_forecast_rows(rows)
        if rejected and self._diagnostics is not None:
            self._diagnostics.record(
                source="meteoblue", event_type="validation_rejected",
                detail=f"{rejected} value(s) outside physical bounds",
            )
        await self.hass.async_add_executor_job(self._db.insert_forecast_snapshots_bulk, rows)
        if parsed.points and parsed.run_fingerprint is not None:
            await self.hass.async_add_executor_job(
                self._db.set_provider_run_fingerprint, "meteoblue", parsed.run_fingerprint
            )

    async def _async_update_data(self) -> None:
        # v0.1.6 fix: this used hardcoded UTC ("local_dt" was a misnomer —
        # it wasn't local at all). In summer (CEST = UTC+2), that meant
        # meteoblue was actually polling at 14:00/18:00/22:00 local time
        # instead of the intended 12:00/16:00/20:00 — a real 2-hour
        # scheduling offset, caught from a production log showing
        # meteoblue hadn't polled yet at a time it should have. Now uses
        # HA's own configured-timezone "now" helper, the standard pattern
        # for this rather than assuming UTC equals local time.
        await self._async_load_persisted_state_if_needed()
        local_dt = dt_util.now()
        if not should_fire_scheduled_call(
            local_dt=local_dt, last_scheduled_call_hour=self._last_scheduled_call_hour
        ):
            return None
        # v0.1.24 fix (P1-09): bound retry frequency within a slot that
        # is still unserviced because the last attempt failed.
        if self._last_scheduled_attempt_at is not None:
            since_attempt = local_dt - self._last_scheduled_attempt_at
            if since_attempt < METEOBLUE_SCHEDULED_RETRY_COOLDOWN:
                return None

        # v0.1.24 fix (P1-06): reserve against the annual budget before
        # spending a credit. Deliberately a quiet skip rather than an
        # UpdateFailed — an exhausted annual budget is a designed
        # operating state, not a fault, and this matches the style of the
        # "not a scheduled slot yet" early return directly above.
        if not self._annual_budget.try_call(today=local_dt.date()):
            _LOGGER.warning(
                "meteoblue annual call budget exhausted (%s calls/year); "
                "skipping this scheduled slot",
                METEOBLUE_ANNUAL_CALL_BUDGET,
            )
            return None
        await self._async_persist_annual_budget_state()

        self._last_scheduled_attempt_at = local_dt
        try:
            await self._async_fetch_and_store(trigger_reason="scheduled")
            self._last_scheduled_call_hour = local_dt
            # v0.1.23 fix (L-08): persist the serviced slot immediately —
            # previously only held in memory, so a restart right after a
            # successful scheduled call could forget it happened and fire
            # a duplicate call for the same slot.
            await self.hass.async_add_executor_job(
                self._db.set_last_scheduled_call_hour, "meteoblue", local_dt.isoformat()
            )
        except Exception as err:  # noqa: BLE001
            kind = self.health.record_error(err)
            # v0.1.24 (P1-03): the meteoblue client also embeds its key in
            # the request URL, so raw exception text can carry it.
            safe_err = redact_secret_values(str(err), secrets=self._secret_values())
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteoblue", event_type="poll_failure", detail=safe_err
                )
            # v0.1.24 fix (P1-01): meteoblue had no auth branch at all.
            if kind == "auth":
                raise ConfigEntryAuthFailed(
                    "meteoblue authentication failed"
                ) from None
            raise UpdateFailed(f"meteoblue fetch failed: {safe_err}") from None
        return None


class CombiPrecipCoordinator(DataUpdateCoordinator):
    """Continuous 5-min polling — this is a Model B feature source, not a
    Model A blend expert, so results are stored in radar_observations, not
    forecast_snapshots (see storage/db.py and DEVELOPER.md).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        *,
        diagnostics: Any = None,
    ) -> None:
        from .const import COMBIPRECIP_POLL_INTERVAL

        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_combiprecip",
            update_interval=COMBIPRECIP_POLL_INTERVAL,
        )
        self._db = db
        self._diagnostics = diagnostics
        self._client = CombiPrecipClient(
            async_get_clientsession(hass),
            latitude,
            longitude,
            bearing_degrees=UPWIND_BEARING_DEGREES,
            distances_km=UPWIND_DISTANCES_KM,
            labels=UPWIND_POINT_LABELS,
        )
        self.health = SourceHealth()

    async def _async_update_data(self) -> list[Any]:
        start = time.monotonic()
        try:
            # v0.1.7 fix: this used to do `with tempfile.TemporaryDirectory()`
            # directly here, with the actual file write happening inside
            # the awaited client call — HA's own loop-blocking detector
            # caught both the file write and the temp-dir cleanup
            # happening synchronously on the event loop. Now: async
            # download only, then the entire blocking sequence (temp dir,
            # write, h5py parse, cleanup) runs via one executor job.
            #
            # v0.1.14: outer backstop timeout, longer than most other
            # coordinators' (120s vs 60s) since this is the one client
            # downloading an actual binary file plus running an
            # executor-wrapped HDF5 parse, not just a small JSON response.
            async with asyncio.timeout(120):
                data = await self._client.async_fetch_latest_bytes()
                values = await self.hass.async_add_executor_job(
                    self._client.write_temp_and_extract, data
                )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="combiprecip", event_type="poll_failure", detail=str(err)
                )
            raise UpdateFailed(f"CombiPrecip fetch failed: {err}") from err
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="combiprecip", event_type="poll_success",
                detail=f"{len(values)} points extracted",
            )

        # Only the "local" point goes into radar_observations (const.py
        # schema — one row per scan for the configured location); the
        # upwind points are Model B-only features and are passed straight
        # through to ModelBCoordinator rather than persisted separately,
        # since their value is in the live signal, not historical trend.
        local = next((v for v in values if v.label == "local"), None)
        if local is not None:
            await self.hass.async_add_executor_job(
                self._db.insert_radar_observation,
                local.valid_at.isoformat(),
                local.precip_rate_mmh,
                None,
            )
        return values


class StationCoordinator(DataUpdateCoordinator):
    """Reads the configured local sensor entities and logs them — this is
    the ground truth everything else gets corrected against.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        temp_entity: str,
        humidity_entity: str,
        pressure_entity: str,
        *,
        pressure_is_sea_level: bool = True,
        elevation_m: Optional[float] = None,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_station",
            update_interval=STATION_POLL_INTERVAL,
        )
        self._db = db
        self._temp_entity = temp_entity
        self._humidity_entity = humidity_entity
        self._pressure_entity = pressure_entity
        self._diagnostics = diagnostics
        # v0.1.24 (P1-22): see _async_update_data.
        self._pressure_is_sea_level = pressure_is_sea_level
        self._elevation_m = elevation_m

    def _read_float_state(
        self, entity_id: str, measurement_kind: str
    ) -> Optional[float]:
        """Read one station entity, in the units the models expect.

        **v0.1.24 fix (P1-20)**: float() happily parses "nan", "inf",
        "-inf" and "Infinity" without raising, so those sailed straight
        through the old `except ValueError` into station_observations and
        from there into Model A's EMA and Model B's tendency math. A
        single non-finite sample permanently poisons an EMA bucket —
        there is no mechanism for an EMA to forget a value it has already
        absorbed. Treated the same as "unknown"/"unavailable".

        **v0.1.24 fix (P1-21)**: the reading is now converted from the
        entity's own declared unit. Previously the raw number was used
        as-is, so a Fahrenheit or inHg sensor produced numerically
        plausible but badly wrong values which Model A would faithfully
        learn as provider bias. An unrecognised unit yields None rather
        than a guess — see unit_conversion.py.
        """
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        if not math.isfinite(value):
            return None

        unit = None
        try:
            unit = state.attributes.get("unit_of_measurement")
        except AttributeError:  # pragma: no cover - defensive
            unit = None

        if measurement_kind == "temperature":
            return unit_conversion.convert_temperature_to_celsius(value, unit)
        if measurement_kind == "pressure":
            return unit_conversion.convert_pressure_to_hpa(value, unit)
        if measurement_kind == "humidity":
            return unit_conversion.convert_humidity_to_percent(value, unit)
        return value

    async def _async_update_data(self) -> dict[str, Optional[float]]:
        temperature = self._read_float_state(self._temp_entity, "temperature")
        humidity = self._read_float_state(self._humidity_entity, "humidity")
        pressure = self._read_float_state(self._pressure_entity, "pressure")

        # v0.1.24 fix (P1-22): reduce a station-level pressure reading to
        # mean sea level, so it is comparable with every provider's
        # forecast pressure (all of which are MSL — Open-Meteo's
        # pressure_msl, meteoblue's sealevelpressure). Without this,
        # Model A absorbs a constant elevation-dependent offset as
        # "bias".
        #
        # Gated on an explicit user answer rather than a heuristic
        # because it genuinely cannot be inferred: Netatmo, the reference
        # station here, publishes BOTH a sea-level-normalised "Pressure"
        # and a raw "AbsolutePressure", and Home Assistant gives both the
        # same device class. See CONF_STATION_PRESSURE_IS_SEA_LEVEL.
        if pressure is not None and not self._pressure_is_sea_level:
            pressure = unit_conversion.reduce_station_pressure_to_sea_level(
                pressure, self._elevation_m, temperature
            )

        # v0.1.24 fix (IND-02): do not write a row when every value is
        # missing. compute_tendency_features used to take samples[-1]
        # wholesale, so one all-None row at the end of the window blanked
        # all nine tendency features and dropped the storm score to zero
        # despite an hour of good data sitting behind it. model_b.py now
        # resolves its endpoints per measurement, which is the real fix;
        # not writing empty rows in the first place keeps the table
        # honest and reduces volume against the retention window.
        if temperature is None and humidity is None and pressure is None:
            return {"temperature": None, "humidity": None, "pressure": None}

        now_iso = datetime.now(timezone.utc).isoformat()
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has.
        async with asyncio.timeout(30):
            await self.hass.async_add_executor_job(
                self._db.insert_station_observation, now_iso, temperature, humidity, pressure
            )
        return {"temperature": temperature, "humidity": humidity, "pressure": pressure}


class MeteonomiqsCoordinator(DataUpdateCoordinator):
    """Daily keep-alive (unconditional, prevents the ~30-day inactivity
    revocation) plus event-triggered bonus calls from the cross-model
    trigger. See DEVELOPER.md ("Why Meteonomiqs needs a daily heartbeat").

    During Mar-Oct (the same storm-season window as meteoblue's schedule),
    the daily keep-alive call uses /forecast/hourly (pressure,
    precipitation) at local noon instead of /nowcast — this is NOT
    an additional call, either satisfies the same keep-alive requirement,
    so the annual budget is unaffected; it's just a more useful payload on
    the day it's needed. Outside that window, or if noon has already
    passed without a call happening yet that day, the plain nowcast
    keep-alive is used as the fallback — the priority is never missing a
    day, not always hitting noon exactly.
    """

    # v0.1.28 fix (SWF-P2-006): 1 hour, not 6.
    #
    # const.py states this call happens "at local noon", and the hour was
    # chosen for a meteorological reason — the seasonal /forecast/hourly
    # upgrade is most useful then. But noon was only ever a GATE
    # (`local_now.hour >= 12`), not a schedule, and the coordinator woke
    # every 6 hours counted from Home Assistant start-up. The real call
    # time was therefore "the first check after noon", anywhere from
    # 12:00 to nearly 18:00, silently drifting on every restart. An
    # installation started at 08:14 made its daily call at 14:14.
    #
    # An hourly check makes the call land within an hour of noon, always.
    # It costs nothing: the daily gate (_last_successful_call_date) and
    # the noon gate both still apply, so 23 of the 24 checks return
    # immediately without touching the network, and — since v0.1.27's
    # SWF-P1-001 fix — without reserving quota either.
    CHECK_INTERVAL = timedelta(hours=1)

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        latitude: float,
        longitude: float,
        api_key: str,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_meteonomiqs",
            update_interval=self.CHECK_INTERVAL,
        )
        self._db = db
        self._latitude = latitude
        self._longitude = longitude
        self._diagnostics = diagnostics
        self._client = MeteonomiqsClient(async_get_clientsession(hass), api_key)
        self._budget = AnnualCallBudget(METEONOMIQS_ANNUAL_CALL_BUDGET)
        # v0.1.17 fix: previously had no per-day cap on bonus calls at
        # all — see async_request_bonus_call and const.py's
        # METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT for the full story.
        self._bonus_tracker = BonusCallTracker(
            max_calls_per_day=METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT
        )
        self._last_successful_call_date: Optional[date] = None
        self.last_nowcast: Optional[Any] = None
        self.last_hourly_forecast: Optional[list[Any]] = None
        # v0.1.23 fix (L-07, and the same L-08 bug class applied here
        # too): _budget and _bonus_tracker above are both still
        # constructed with empty in-memory state — that part is
        # unchanged. _async_load_persisted_state_if_needed() below
        # overlays DB-persisted state onto them once per coordinator
        # lifetime, so a restart no longer resets the annual call counter
        # (L-07) or the same-day bonus-call allowance (L-08's bug class,
        # previously only fixed for meteoblue's own tracker).
        self._state_loaded_from_db = False
        self.health = SourceHealth()

    async def _async_load_persisted_state_if_needed(self) -> None:
        if self._state_loaded_from_db:
            return
        budget_state = await self.hass.async_add_executor_job(
            self._db.get_annual_call_budget_state, "meteonomiqs"
        )
        if budget_state is not None:
            self._budget.load_state(budget_state)
        bonus_state = await self.hass.async_add_executor_job(
            self._db.get_bonus_call_tracker_state, "meteonomiqs"
        )
        if bonus_state is not None:
            self._bonus_tracker = BonusCallTracker.from_state(
                bonus_state, max_calls_per_day=METEONOMIQS_MAX_BONUS_CALLS_PER_EVENT
            )
        # v0.1.24 fix (P1-08): restore the daily-call marker too. Every
        # other piece of this coordinator's restart state was already
        # persisted by the L-07/L-08 fixes; this one was missed, so a
        # same-day restart forgot that today had been serviced and spent
        # an extra call.
        last_call_iso = await self.hass.async_add_executor_job(
            self._db.get_meteonomiqs_last_successful_call_date
        )
        if last_call_iso:
            try:
                self._last_successful_call_date = date.fromisoformat(last_call_iso)
            except ValueError:
                # Same philosophy as P2-02's _safe_parse_meta: a corrupt
                # value must not stop this coordinator from starting.
                _LOGGER.warning(
                    "Ignoring unparseable persisted Meteonomiqs call date %r",
                    last_call_iso,
                )
        self._state_loaded_from_db = True

    async def _async_persist_budget_and_bonus_state(self) -> None:
        """Called after every successful call that mutates either
        tracker, so the persisted state never lags behind what's actually
        been spent — the whole point of L-07/L-08 is that these numbers
        must survive a restart happening at ANY point, not just at a
        convenient checkpoint."""
        state = self._budget.to_state()
        await self.hass.async_add_executor_job(
            self._db.set_annual_call_budget_state,
            "meteonomiqs",
            state["year"],
            state["calls_used"],
        )
        await self.hass.async_add_executor_job(
            self._db.set_bonus_call_tracker_state, "meteonomiqs", self._bonus_tracker.to_state()
        )

    async def _async_persist_last_successful_call_date(self, day: date) -> None:
        """v0.1.24 fix (P1-08): persist the daily-call marker.

        This was memory-only, so it reset to None on every restart and
        the daily gate then failed to recognise a day that had already
        been serviced — firing an unnecessary extra call against a
        1000-calls/year budget after any same-day restart. During setup
        or troubleshooting that can easily be several restarts in one
        afternoon.
        """
        await self.hass.async_add_executor_job(
            self._db.set_meteonomiqs_last_successful_call_date, day.isoformat()
        )

    async def async_request_bonus_call(self) -> bool:
        """Cross-model trigger bonus call — always nowcast (the fast,
        radar-based signal), regardless of season, since this is about an
        immediate storm check, not the daily outlook the noon call gives.
        """
        await self._async_load_persisted_state_if_needed()
        today = datetime.now(timezone.utc).date()
        # v0.1.17 fix: this used to only check the overall annual budget
        # (self._budget.can_call), with no per-day cap at all — confirmed
        # in production allowing it to fire every 5 minutes (whatever the
        # underlying reason the trigger kept re-evaluating true), unlike
        # meteoblue's equivalent path which was always protected by
        # BonusCallTracker. This check is deliberately placed FIRST and
        # short-circuits before the annual-budget check — a repeatedly
        # firing trigger should be stopped by the daily cap long before
        # it's even a question of remaining annual budget.
        if not self._bonus_tracker.can_use_bonus_call(today=today):
            return False
        # v0.1.24 fix (P1-07): this used to be a plain can_call()
        # pre-filter, with the matching record_call() happening inside
        # _async_fetch_nowcast AFTER an awaited HTTP call. Two paths share
        # this same self._budget object — this bonus path and the
        # independent daily keepalive path — so both could pass the check
        # before either committed, and the real annual quota could be
        # exceeded.
        #
        # The v0.1.15 comment that used to sit here argued try_call()
        # would double-count, because the fetch recorded the call
        # itself. That was true then; the fix is to move the accounting
        # rather than keep the race. Reservation now happens exactly once,
        # synchronously, at the caller, and record_call() has been removed
        # from both fetch methods.
        if not self._budget.try_call(today=today):
            _LOGGER.warning(
                "Meteonomiqs annual budget exhausted; skipping bonus call"
            )
            return False
        self._bonus_tracker.record_bonus_call_used(today=today)
        await self._async_persist_budget_and_bonus_state()
        await self._async_fetch_nowcast(today=today)
        return True

    async def _async_fetch_nowcast(self, *, today: date) -> None:
        start = time.monotonic()
        try:
            # v0.1.14: same defense-in-depth backstop as every other
            # coordinator now has.
            async with asyncio.timeout(60):
                self.last_nowcast = await self._client.async_fetch_nowcast(
                    latitude=self._latitude, longitude=self._longitude
                )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteonomiqs", event_type="poll_failure",
                    detail=f"nowcast: {err}",
                )
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteonomiqs", event_type="poll_success", detail="nowcast",
            )
        # v0.1.24 (P1-07): budget reservation now happens exactly once at
        # the caller, synchronously, before this awaited fetch — not here
        # afterwards. See async_request_bonus_call and _async_update_data.
        self._last_successful_call_date = today
        await self._async_persist_last_successful_call_date(today)
        await self._async_persist_budget_and_bonus_state()

    async def _async_fetch_hourly_forecast(self, *, today: date) -> None:
        start = time.monotonic()
        try:
            async with asyncio.timeout(60):
                self.last_hourly_forecast = await self._client.async_fetch_hourly_forecast(
                    latitude=self._latitude, longitude=self._longitude
                )
        except Exception as err:  # noqa: BLE001
            self.health.record_error(err, duration_ms=(time.monotonic() - start) * 1000)
            if self._diagnostics is not None:
                self._diagnostics.record(
                    source="meteonomiqs", event_type="poll_failure",
                    detail=f"hourly_forecast: {err}",
                )
            raise
        self.health.record_success(duration_ms=(time.monotonic() - start) * 1000)
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="meteonomiqs", event_type="poll_success", detail="hourly_forecast",
            )
        # v0.1.24 (P1-07): see _async_fetch_nowcast.
        self._last_successful_call_date = today
        await self._async_persist_last_successful_call_date(today)
        await self._async_persist_budget_and_bonus_state()

        # v0.1.23 fix (own-review finding — "Meteonomiqs hourly forecast
        # fetched but never used"): previously self.last_hourly_forecast
        # above was the only place this data ever went — nothing read it
        # again, despite this call spending real annual-budget quota. Now
        # persisted into forecast_snapshots under variable names prefixed
        # with METEONOMIQS_HOURLY_VARIABLE_PREFIX (see const.py's comment
        # there for why the prefix matters: Meteonomiqs stays deliberately
        # excluded from ALL_FORECAST_SOURCES, so these rows can never be
        # picked up by Model A's blend, even by accident — this only makes
        # the data durable and available for future use, it does not
        # change any current scoring behavior).
        if self.last_hourly_forecast:
            now_iso = datetime.now(timezone.utc).isoformat()
            # HourlyForecastPoint is wide-format (one row per hour with
            # three named fields), unlike the narrow variable/value shape
            # every other client's forecast point uses — unpacked into
            # three narrow forecast_snapshots rows per hour here.
            rows = []
            for point in self.last_hourly_forecast:
                for suffix, value in (
                    ("pressure", point.mean_sea_level_pressure),
                    ("precip_sum", point.precipitation_sum_mm),
                    ("precip_probability", point.precipitation_probability),
                ):
                    rows.append(
                        (
                            "meteonomiqs",
                            now_iso,
                            point.valid_at.isoformat(),
                            f"{METEONOMIQS_HOURLY_VARIABLE_PREFIX}{suffix}",
                            value,
                            "scheduled",
                        )
                    )
            await self.hass.async_add_executor_job(
                self._db.insert_forecast_snapshots_bulk, rows
            )

    async def _async_update_data(self) -> None:
        await self._async_load_persisted_state_if_needed()
        # v0.1.15 fix: "local_now" used to be datetime.now(timezone.utc) —
        # the same class of bug already fixed for meteoblue in v0.1.6, but
        # never checked here too, caught by an outside code review. In
        # Switzerland (CEST = UTC+2) this shifted the noon cutoff by 2
        # hours. Now uses HA's own configured-timezone helper, matching
        # the meteoblue fix.
        local_now = dt_util.now()
        today = local_now.date()

        # v0.1.15 fix: this used to be gated behind needs_keepalive_call()
        # (only True once ~30 days had passed since the last successful
        # call) wrapping the entire method below — meaning the "daily"
        # seasonal forecast call this project's own design docs describe
        # never actually fired more than once every 30 days, contradicting
        # the documented intent. Confirmed by an outside code review
        # against this exact code. The daily-once-per-day check below is
        # now the actual gate; the 30-day threshold is only a loud warning
        # if the daily logic somehow hasn't produced a successful call in
        # that long — a real problem worth surfacing, not something that
        # should have been gating every attempt in the first place.
        if needs_keepalive_call(
            last_successful_call_date=self._last_successful_call_date,
            today=today,
            max_days_between_calls=METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
        ):
            _LOGGER.warning(
                "Meteonomiqs hasn't had a successful call in %s+ days — "
                "the daily keepalive logic may not be working, and the "
                "API key risks revocation from inactivity.",
                METEONOMIQS_KEEPALIVE_MAX_DAYS_BETWEEN_CALLS,
            )

        if self._last_successful_call_date == today:
            return None

        in_forecast_season = local_now.month in METEONOMIQS_FORECAST_SEASON_MONTHS
        past_noon = local_now.hour >= METEONOMIQS_FORECAST_CALL_HOUR_LOCAL

        # v0.1.24 fix (P1-07): the scheduled/keepalive path reserves its
        # own call, since record_call() no longer happens inside the fetch
        # methods. Deliberately record_call() and NOT try_call():
        # AnnualCallBudget's own class docstring states the keepalive must
        # never be skipped just because bonus calls consumed the budget
        # elsewhere — losing API access to inactivity-revocation is worse
        # than a slightly tighter annual count.
        #
        # The external ICS audit challenged that policy as an unbounded
        # bypass of a hard quota, which is a fair question, so the
        # arithmetic is stated here rather than left implicit: the
        # keepalive fires at most once per day (gated by
        # _last_successful_call_date above) and the bonus path at most
        # once per day (BonusCallTracker). Worst case is therefore
        # 365 + 365 = 730 calls/year against a 1000/year budget. The
        # unconditional keepalive cannot exhaust the quota; it can only
        # make the count slightly tighter, which is the trade the
        # docstring describes.
        #
        # v0.1.27 fix (SWF-P1-001): the reservation now happens INSIDE
        # each branch that actually performs a request.
        #
        # It used to run before this if/elif, which meant the seasonal
        # pre-noon case — in forecast season, checked before
        # METEONOMIQS_FORECAST_CALL_HOUR_LOCAL — incremented the annual
        # counter and then deliberately made no call at all. With a
        # 6-hourly check that is up to two phantom credits per seasonal
        # day before the real noon call, roughly tripling the recorded
        # cost of a once-daily service. Persisted quota state then
        # diverges from actual provider traffic, and the budget exhausts
        # early, taking legitimate storm-triggered bonus calls with it.
        #
        # Still recorded synchronously, before the awaited fetch, so the
        # P1-07 TOCTOU fix is preserved intact.
        try:
            if in_forecast_season and past_noon:
                self._budget.record_call(today=today)
                await self._async_fetch_hourly_forecast(today=today)
            elif not in_forecast_season:
                # Nov-Feb: nowcast is a pure keepalive with no time-of-day
                # data-quality reason to wait, unlike the seasonal forecast
                # call above — fire as soon as a new day starts.
                self._budget.record_call(today=today)
                await self._async_fetch_nowcast(today=today)
            # else: in forecast season but before local noon today — this
            # coordinator checks every 6h and may run before noon; simply
            # wait for a later check today (guaranteed within the same day,
            # since a 6h interval always has at least one check past noon).
            # NOTHING is reserved on this path: no request is made.
        except Exception as err:  # noqa: BLE001
            # A failed keep-alive is worth logging loudly — losing API
            # access entirely from inactivity is worse than a routine
            # data-fetch error elsewhere in this system. Deliberately not
            # re-raised: the next scheduled check today (or tomorrow) will
            # retry via the same daily-gate logic above.
            #
            # v0.1.24 fix (P1-01): with ONE exception. This block used to
            # swallow every failure kind, including a revoked key — so
            # the single condition this coordinator exists to prevent
            # produced no user-visible signal whatsoever. Authentication
            # failures are now re-raised as ConfigEntryAuthFailed so Home
            # Assistant starts its reauth flow; every other kind still
            # degrades silently, exactly as designed.
            #
            # classify_exception() is called directly rather than
            # health.record_error(), which already ran earlier in this
            # same call chain inside the fetch methods — calling it again
            # here would double-count the failure in diagnostics.
            _LOGGER.error("Meteonomiqs keep-alive call failed: %s", err)
            if classify_exception(err) == "auth":
                raise ConfigEntryAuthFailed(
                    "Meteonomiqs authentication failed"
                ) from None
        return None


class ModelABlendCoordinator(DataUpdateCoordinator):
    """Computes Model A's blended values — both "now" and a genuine
    multi-hour forecast — in one batched executor job per refresh cycle.

    **v0.1.5 fix**: this replaces logic that used to live directly in
    weather.py's entity properties, which queried the database
    synchronously on the event loop — every other part of this project
    routes DB access through an executor job except that one. Moving the
    computation here, run once per refresh rather than once per property
    read, fixes that and is also what makes a real hourly forecast
    practical: computing 48 hours × 5 measurements as 240 individual
    blocking property-reads would have been much worse than the same
    work batched into one executor job.

    **v0.1.13 fix**: moving the work into one executor job wasn't enough
    on its own — the job itself was still doing up to ~8,400 individual
    sequential database round trips every single cycle (168 hours × 5
    measurements × up to 5 sources, each needing its own
    get_forecast_values_for_valid_at *and* get_bucket_stats call). Found
    while investigating a reported multi-hour freeze affecting every
    coordinator simultaneously — whether or not this was the full
    explanation, an executor job potentially taking a very long time
    every 10 minutes is a real problem on its own, tying up a thread far
    longer than it needs to. Now: two bulk queries
    (get_forecast_snapshots_in_window, get_all_bucket_stats) fetch
    everything needed for the whole 168-hour computation up front, and
    _blend_at becomes a pure in-memory lookup with no database access at
    all — the same math, just no longer paying for a round trip per
    individual lookup.

    Also the home of wind_speed exposure, which was already flowing
    through Model A's blend (every client already reports it) but was
    never actually surfaced on the weather entity — data that existed
    with nothing reading it.
    """

    MEASUREMENTS = ("temperature", "humidity", "pressure", "precip", "wind_speed")
    # 7 days rather than 2 — needed for meaningful daily/twice-daily
    # coverage (added alongside precipitation-in-mm for those views), and
    # matches roughly CH2/meteoblue's own horizons. Sources with shorter
    # horizons (CH1's ~33-45h) simply taper off within this window rather
    # than every hour having full coverage from every source.
    FORECAST_HOURS_AHEAD = 168

    def __init__(self, hass: HomeAssistant, db: SwissWeatherDB) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_blend",
            update_interval=timedelta(minutes=10),
        )
        self._db = db

    def _is_daytime_at(self, when: datetime) -> Optional[bool]:
        """Whether the sun is above the horizon at `when` (v0.1.28).

        Uses Home Assistant's own astral helpers so the answer matches
        core's sun entity and the installation's real location. Returns
        None if that cannot be determined, which makes derive_condition
        fall back to its pre-v0.1.28 behaviour rather than guessing.
        """
        try:
            from homeassistant.helpers.sun import get_astral_event_date

            from homeassistant.util import dt as dt_util

            local = dt_util.as_local(when)
            sunrise = get_astral_event_date(self.hass, "sunrise", local.date())
            sunset = get_astral_event_date(self.hass, "sunset", local.date())
            if sunrise is None or sunset is None:
                return None
            return sunrise <= when < sunset
        except Exception:  # noqa: BLE001 - a forecast icon must never break a refresh
            return None

    async def _async_update_data(self) -> dict[str, Any]:
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has. Generous (120s) since this job now does
        # two bulk queries plus in-memory processing of a potentially
        # large result set (v0.1.13's fix), still bounded but with
        # headroom.
        async with asyncio.timeout(120):
            return await self.hass.async_add_executor_job(self._compute_blend)

    def _blend_at(
        self,
        measurement: str,
        target_hour: datetime,
        *,
        latest_forecast: dict[tuple[str, str, str], tuple[float, datetime]],
        bucket_lookup: dict[tuple, Any],
    ) -> Optional[float]:
        """**v0.1.13**: pure in-memory lookup, no database access at all —
        both dicts are built once per cycle in _compute_blend from two
        bulk queries, not fetched here. Same blending math as before,
        just no longer paying for a round trip per (hour, measurement,
        source) combination.
        """
        from .models import model_a

        hour_of_day = target_hour.hour
        season = model_a.derive_season(target_hour)
        target_iso = target_hour.replace(minute=0, second=0, microsecond=0).isoformat()

        contributions: list[model_a.SourceContribution] = []
        for source in ALL_FORECAST_SOURCES:
            entry = latest_forecast.get((source, measurement, target_iso))
            if entry is None:
                continue
            raw_value, issued_at = entry
            lead_time_bucket = model_a.derive_lead_time_bucket(issued_at, target_hour)
            bucket = bucket_lookup.get(
                (hour_of_day, season, lead_time_bucket, source, measurement)
            )
            if bucket is None:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=raw_value,
                        ema_bias=0.0, ema_weight=1.0, sample_count=0,
                    )
                )
            else:
                contributions.append(
                    model_a.SourceContribution(
                        source=source, raw_value=raw_value,
                        ema_bias=bucket.ema_bias, ema_weight=bucket.ema_weight,
                        sample_count=bucket.sample_count,
                    )
                )
        return model_a.blend(contributions)

    def _compute_blend(self) -> dict[str, Any]:
        from .models import model_a
        from .storage.db import BucketStats

        now = model_a.utcnow().replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(hours=self.FORECAST_HOURS_AHEAD)

        # Two bulk queries for the whole cycle, replacing what used to be
        # up to ~8,400 individual round trips — see the class docstring.
        raw_rows = self._db.get_forecast_snapshots_in_window(
            start_valid_at=now.isoformat(), end_valid_at=end.isoformat()
        )
        latest_forecast: dict[tuple[str, str, str], tuple[float, datetime]] = {}
        for row in raw_rows:
            if row["value"] is None:
                continue
            key = (row["source"], row["variable"], row["valid_at"])
            if key in latest_forecast:
                continue  # already have the freshest (rows are issued_at DESC)
            latest_forecast[key] = (row["value"], datetime.fromisoformat(row["issued_at"]))

        bucket_rows = self._db.get_all_bucket_stats()
        bucket_lookup: dict[tuple, BucketStats] = {}
        for row in bucket_rows:
            key = (
                row["hour_of_day"], row["season"], row["lead_time_bucket"],
                row["source"], row["measurement"],
            )
            bucket_lookup[key] = BucketStats(
                ema_bias=row["ema_bias"], ema_abs_error=row["ema_abs_error"],
                ema_weight=row["ema_weight"], sample_count=row["sample_count"],
                last_updated=row["last_updated"],
            )

        current = {
            m: self._blend_at(m, now, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            for m in self.MEASUREMENTS
        }

        # v0.1.14 fix: ExpertWeightSensor used to call self._db.get_bucket_stats()
        # directly inside its native_value property — a plain (non-
        # CoordinatorEntity) property that HA polls directly on the event
        # loop, completely bypassing the executor-job pattern used
        # everywhere else in this project. Computed here instead, for
        # free — bucket_lookup is already fetched above for the blend
        # itself, so extracting the "current hour/season/short lead time"
        # weight per source costs nothing extra.
        from .const import LEAD_TIME_SHORT
        from .models import model_a as _model_a_for_weights

        season_now = _model_a_for_weights.derive_season(now)
        expert_weights: dict[str, Optional[float]] = {}
        for source in ALL_FORECAST_SOURCES:
            bucket = bucket_lookup.get(
                (now.hour, season_now, LEAD_TIME_SHORT, source, "temperature")
            )
            expert_weights[source] = bucket.ema_weight if bucket else None

        hourly_forecast: list[dict[str, Any]] = []
        for i in range(self.FORECAST_HOURS_AHEAD):
            target = now + timedelta(hours=i)
            temperature = self._blend_at("temperature", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            humidity = self._blend_at("humidity", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            pressure = self._blend_at("pressure", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            precip = self._blend_at("precip", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            wind_speed = self._blend_at("wind_speed", target, latest_forecast=latest_forecast, bucket_lookup=bucket_lookup)
            # Skip hours with literally nothing from any source — no
            # point showing an all-None row, and sources with shorter
            # horizons (CH1's ~33-45h) will naturally taper off within
            # this 48h window rather than every hour having full coverage.
            if all(v is None for v in (temperature, humidity, pressure, precip, wind_speed)):
                continue
            hourly_forecast.append(
                {
                    "datetime": target.isoformat(),
                    "native_temperature": temperature,
                    "humidity": humidity,
                    "native_pressure": pressure,
                    "native_precipitation": precip,
                    "native_wind_speed": wind_speed,
                    # v0.1.24 (P2-10): shared mapping. Raw precip and the
                    # hourly 0.1 mm threshold, matching this site's own
                    # pre-existing behaviour.
                    #
                    # v0.1.28 (SWF-P2-005): day/night is evaluated PER
                    # FORECAST HOUR, not from "now" — this is the site
                    # where the sun icon at 02:00 was actually visible.
                    # A fixed hour cutoff would be wrong for Switzerland,
                    # where sunset moves about three hours between June
                    # and December, so real solar elevation is used.
                    "condition": model_a.derive_condition(
                        precip,
                        temperature,
                        humidity,
                        is_daytime=self._is_daytime_at(target),
                    ),
                }
            )

        return {
            "current": current,
            "expert_weights": expert_weights,
            "hourly_forecast": hourly_forecast,
            # Built from the same hourly data above — no extra DB access,
            # just reshaped, per the request to have precipitation (mm)
            # available at daily and twice-daily granularity too, not just
            # hourly.
            # v0.1.15 fix: these used to always group by UTC calendar day
            # regardless of the configured local timezone — confirmed by
            # an outside code review. dt_util.now().tzinfo is the same
            # proven pattern already used for meteoblue/Meteonomiqs's own
            # local-time fixes (v0.1.6/v0.1.15), not a new API.
            "daily_forecast": model_a.aggregate_daily_forecast(
                hourly_forecast, local_tz=dt_util.now().tzinfo
            ),
            "twice_daily_forecast": model_a.aggregate_twice_daily_forecast(
                hourly_forecast, local_tz=dt_util.now().tzinfo
            ),
        }


class ModelALearningCoordinator(DataUpdateCoordinator):
    """Model A's actual learning step — periodically compares past
    forecasts against what the station actually measured, and folds the
    result into bucket_stats via the EMA.

    **v0.1.7: closes a real gap found during review.**
    `models.model_a.update_bucket_ema` and `storage.db.upsert_bucket_stats`
    existed and were unit-tested in isolation since early in this
    project, but nothing in production code ever actually called them.
    Without this coordinator, `bucket_stats` would stay empty forever —
    not just during a cold-start window — meaning Model A's blend was
    only ever an unweighted average of raw forecasts, never applying the
    learned bias correction that's the actual point of the project.

    Runs every 20 minutes (bias correction is a slow-moving statistic;
    this doesn't need to be frequent) and does the entire batch — finding
    due forecast rows, fetching candidate station readings, matching,
    and updating every bucket — inside one executor job, the same
    pattern as ModelABlendCoordinator.
    """

    RECONCILIATION_INTERVAL = timedelta(minutes=20)
    # Only measurements the local station can actually confirm — precip
    # and wind_speed have no ground truth yet (station has no rain/wind
    # sensors), so forecasts for those are stored but never reconciled.
    RECONCILIATION_MEASUREMENTS = ("temperature", "humidity", "pressure")
    # How far back to look on the very first run ever (no watermark yet).
    # 14 days comfortably covers every source's forecast horizon
    # (meteoblue's ~7-10 days is the longest) without trying to reconcile
    # an unbounded amount of history in one go.
    INITIAL_LOOKBACK = timedelta(days=14)
    # v0.1.15 fix: how long to keep retrying a row that couldn't find a
    # matching station reading, before treating the gap as permanent and
    # letting the watermark advance past it. Without this, the watermark
    # used to advance to "now" unconditionally every cycle regardless of
    # skipped rows — a station outage lasting even a few minutes longer
    # than the matching tolerance would permanently drop that hour's
    # learning sample forever, with no distinction between "genuinely
    # unrecoverable" and "just hasn't been retried yet". Confirmed by an
    # outside code review against this exact loop. 48 hours gives several
    # retry cycles (every 20 minutes) before concluding a gap is real,
    # without letting the retry window grow unbounded if a gap turns out
    # to be permanent.
    RETRY_GIVE_UP_AGE = timedelta(hours=48)

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        *,
        reconcile_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_learning",
            update_interval=self.RECONCILIATION_INTERVAL,
        )
        self._db = db
        self.last_reconciled_count: int = 0
        # v0.1.28 (SWF-P1-007): cached for ForecastAccuracySensor, so no
        # entity has to touch the database from the event loop.
        self.temperature_mae: Optional[dict[str, Any]] = None
        # v0.1.24 fix (P2-03 / P2-04): ONE lock, shared with the other
        # coordinator that writes the same tables.
        #
        # Every SwissWeatherDB method takes self._lock individually, but
        # reconciliation's logical read-modify-write spans several
        # separate locked calls, and RetentionCoordinator can delete rows
        # in between them. Per-statement locking does not make a
        # multi-step read snapshot-consistent. A shared object rather
        # than two independent locks is the whole point — two
        # uncoordinated locks would serialize nothing against each other.
        #
        # Falls back to a private lock when none is injected, so each
        # coordinator remains independently constructible in tests.
        self._reconcile_lock = reconcile_lock or asyncio.Lock()

    async def _async_update_data(self) -> Optional[datetime]:
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has.
        async with self._reconcile_lock:
            async with asyncio.timeout(120):
                return await self.hass.async_add_executor_job(self._reconcile)

    def _compute_temperature_mae(self) -> Optional[dict[str, Any]]:
        """Sample-count-weighted mean absolute error across temperature buckets.

        Synchronous by design — only ever called from inside the executor
        job that _reconcile() already runs in.

        Weighted by sample_count so a bucket with three observations does
        not carry the same authority as one with three hundred. Returns
        None only when nothing has genuinely been learned yet, which on a
        fresh install is the honest answer.

        v0.1.28 (SWF-P1-007): note this iterates sqlite3.Row objects.
        v0.1.24's version called `.items()` on the same list as though it
        were a dict, raised AttributeError on every call, and had that
        swallowed by a blanket `except Exception: return None` — so the
        sensor silently showed nothing for four releases while looking
        implemented. No blanket except here: a failure in reconciliation
        should surface, not hide.
        """
        rows = self._db.get_all_bucket_stats()

        total_weighted = 0.0
        total_samples = 0
        buckets = 0
        for row in rows:
            if row["measurement"] != "temperature":
                continue
            sample_count = row["sample_count"] or 0
            if sample_count <= 0:
                continue
            total_weighted += (row["ema_abs_error"] or 0.0) * sample_count
            total_samples += sample_count
            buckets += 1

        if total_samples == 0:
            return None
        return {
            "value": round(total_weighted / total_samples, 3),
            "bucket_count": buckets,
            "sample_count": total_samples,
        }

    def _reconcile(self) -> datetime:
        """Synchronous — only ever called via the executor job above.

        v0.1.23 fix (L-01/L-02): rewritten around per-row
        reconciliation_status instead of a single global watermark. See
        SwissWeatherDB.get_pending_forecast_snapshots()/
        mark_forecast_snapshots_status() for the storage-layer half of
        this fix and the full rationale. The behavioral guarantee this
        gives: a row is folded into bucket_stats at most once, ever
        (fixes L-01's re-learning), and a row is never permanently
        unreachable just because other rows near it were already
        processed (fixes L-02's silently-dropped late arrivals).
        """
        from .models import model_a
        from .storage.db import BucketKey

        now = model_a.utcnow()
        until_iso = now.isoformat()

        pending_rows = self._db.get_pending_forecast_snapshots(
            until_ts=until_iso,
            measurements=self.RECONCILIATION_MEASUREMENTS,
        )
        if not pending_rows:
            self.last_reconciled_count = 0
            return now

        # One station-observation query for the whole batch (padded by
        # the matching tolerance on each side), not one query per forecast
        # row — matches the batching approach already used elsewhere in
        # this project (e.g. ModelABlendCoordinator). The lower bound uses
        # INITIAL_LOOKBACK as a safety margin: pending_rows could in
        # principle span further back than "now - tolerance" if something
        # was pending for a long time, but station_observations itself is
        # purged on the same RetentionCoordinator schedule, so anything
        # genuinely that old has no station data left to match against
        # anyway — this bound just keeps the query itself bounded.
        earliest_pending_valid_at = min(
            datetime.fromisoformat(r["valid_at"]) for r in pending_rows
        )
        tolerance = timedelta(minutes=model_a.RECONCILIATION_TOLERANCE_MINUTES)
        lookback_floor = now - self.INITIAL_LOOKBACK - tolerance
        station_query_start = max(earliest_pending_valid_at - tolerance, lookback_floor)
        station_rows = self._db.get_station_observations_between(
            station_query_start.isoformat(), (now + tolerance).isoformat()
        )
        candidates_by_measurement: dict[str, list[tuple[datetime, Any]]] = {
            "temperature": [],
            "humidity": [],
            "pressure": [],
        }
        for row in station_rows:
            ts = datetime.fromisoformat(row["ts"])
            candidates_by_measurement["temperature"].append((ts, row["temperature"]))
            candidates_by_measurement["humidity"].append((ts, row["humidity"]))
            candidates_by_measurement["pressure"].append((ts, row["pressure"]))

        reconciled_count = 0
        reconciled_ids: list[int] = []
        skipped_ids: list[int] = []
        # v0.1.24 (P0-01): accumulated during the loop, applied atomically
        # at the end. bucket_updates is keyed so that repeated hits on one
        # bucket within a batch collapse to a single final write;
        # pending_bucket_state carries the in-flight EMA state those
        # repeats must build on.
        bucket_updates: dict[BucketKey, tuple] = {}
        pending_bucket_state: dict[BucketKey, tuple[float, float, int]] = {}
        for fs_row in pending_rows:
            if fs_row["value"] is None:
                # The stored forecast value itself is null — this can
                # never change no matter how many times it's retried, so
                # there's nothing to gain by leaving it 'pending' forever.
                skipped_ids.append(fs_row["id"])
                continue
            measurement = fs_row["variable"]
            valid_at = datetime.fromisoformat(fs_row["valid_at"])
            issued_at = datetime.fromisoformat(fs_row["issued_at"])

            actual_value = model_a.find_nearest_observation(
                target=valid_at, candidates=candidates_by_measurement[measurement]
            )
            if actual_value is None:
                if (now - valid_at) >= self.RETRY_GIVE_UP_AGE:
                    # Old enough that this gap is treated as permanent
                    # (e.g. a genuine, lasting station outage) — mark it
                    # 'skipped' so it stops being selected every cycle.
                    skipped_ids.append(fs_row["id"])
                # else: still young enough to retry — leave it 'pending'
                # and it will be picked up again next cycle, same as
                # every other still-pending row (no separate watermark
                # bookkeeping needed: per-row status IS the retry state).
                continue

            key = BucketKey(
                hour_of_day=valid_at.hour,
                season=model_a.derive_season(valid_at),
                lead_time_bucket=model_a.derive_lead_time_bucket(issued_at, valid_at),
                source=fs_row["source"],
                measurement=measurement,
            )
            # v0.1.24 fix (P0-01): consult this batch's own in-flight
            # results before falling back to the database.
            #
            # A naive "defer every write, commit once" implementation
            # breaks same-batch, same-bucket sequencing: two rows landing
            # in the same bucket_stats key within one reconciliation
            # batch must build on each other's result, not both read the
            # same stale pre-batch state and then have one silently
            # overwrite the other. Since a bucket is
            # (hour, season, lead_time, source, measurement) and a batch
            # routinely contains many hours of one source's forecast,
            # this is a common case rather than a corner one.
            pending_state = pending_bucket_state.get(key)
            if pending_state is not None:
                previous_bias, previous_abs_error, previous_sample_count = pending_state
            else:
                existing = self._db.get_bucket_stats(key)
                if existing is None:
                    previous_bias, previous_abs_error, previous_sample_count = 0.0, 0.0, 0
                else:
                    previous_bias = existing.ema_bias
                    previous_abs_error = existing.ema_abs_error
                    previous_sample_count = existing.sample_count

            result = model_a.update_bucket_ema(
                previous_bias=previous_bias,
                previous_abs_error=previous_abs_error,
                previous_sample_count=previous_sample_count,
                forecast_value=fs_row["value"],
                actual_value=actual_value,
                lead_time_bucket=key.lead_time_bucket,
            )
            pending_bucket_state[key] = (
                result.ema_bias,
                result.ema_abs_error,
                result.sample_count,
            )
            bucket_updates[key] = (
                key,
                result.ema_bias,
                result.ema_abs_error,
                result.ema_weight,
                result.sample_count,
                now.isoformat(),
            )
            reconciled_count += 1
            reconciled_ids.append(fs_row["id"])

        # v0.1.24 fix (P0-01), CRITICAL: every EMA write and every status
        # transition for this cycle is applied in ONE transaction.
        #
        # Previously upsert_bucket_stats() committed per row inside the
        # loop above, while the two mark_forecast_snapshots_status()
        # calls ran once at the end. A crash between those two points
        # left bucket_stats already updated for rows still marked
        # 'pending' — so the next cycle re-selected them and folded them
        # into the EMA a second time. That is the same double-counting
        # the v0.1.23 reconciliation_status redesign was built to
        # eliminate, arriving through a crash boundary instead of through
        # watermark arithmetic. An EMA cannot un-absorb a duplicated
        # sample, so there is no recovery after the fact.
        #
        # This is still the point of no return: once a row's status
        # leaves 'pending', get_pending_forecast_snapshots() can never
        # select it again. The difference is that now it leaves 'pending'
        # if and only if its learning was also committed.
        self._db.apply_reconciliation_batch(
            list(bucket_updates.values()), reconciled_ids, skipped_ids
        )

        # v0.1.28 fix (SWF-P1-007): the headline accuracy figure is
        # computed HERE, inside the executor job that already holds the
        # database, and cached for the sensor to read.
        #
        # v0.1.24's P3-02 fix had ForecastAccuracySensor.native_value
        # query SQLite directly from a property. Home Assistant polls
        # that property on the event loop roughly every 30 seconds, so
        # it was blocking database I/O on the loop — the same defect
        # class as the manifest read v0.1.25 introduced and v0.1.26
        # removed. Doing it once per reconciliation cycle, off-loop, is
        # both correct and far less work.
        self.temperature_mae = self._compute_temperature_mae()

        self.last_reconciled_count = reconciled_count
        _LOGGER.debug(
            "Model A learning: reconciled %d of %d pending forecast snapshots "
            "(%d newly skipped, %d still pending for retry)",
            reconciled_count,
            len(pending_rows),
            len(skipped_ids),
            len(pending_rows) - reconciled_count - len(skipped_ids),
        )
        return now


class ModelBCoordinator(DataUpdateCoordinator):
    """Scores Model B every 5-10 minutes off the local station stream plus
    the live CombiPrecip radar points, and fires the cross-model trigger
    (INCA originally, now: force a fresh meteoblue/Meteonomiqs read) on an
    upward probability crossing. See models/model_b.py and DEVELOPER.md.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        station_coordinator: StationCoordinator,
        combiprecip_coordinator: CombiPrecipCoordinator,
        meteoblue_coordinator: MeteoblueCoordinator,
        meteonomiqs_coordinator: MeteonomiqsCoordinator,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_model_b",
            update_interval=MODEL_B_SCORING_INTERVAL,
        )
        self._db = db
        self._station_coordinator = station_coordinator
        self._combiprecip_coordinator = combiprecip_coordinator
        self._meteoblue_coordinator = meteoblue_coordinator
        self._meteonomiqs_coordinator = meteonomiqs_coordinator
        # v0.1.26: this coordinator had no diagnostics recorder at all.
        # P2-09's future-dated-sample fix referenced self._diagnostics
        # anyway, which would have raised AttributeError on the first
        # scoring cycle that encountered a future-dated row. Added as an
        # optional keyword so the attribute always exists, defaulting to
        # None exactly like every other coordinator here.
        self._diagnostics = diagnostics
        self._previous_probability = 0.0
        self.current_probability = 0.0
        # v0.1.23 fix (L-09): _previous_probability above is still
        # initialized to 0.0 here — that part is unchanged. What's new is
        # _async_load_persisted_state_if_needed() below, called once at
        # the top of the first post-(re)start scoring cycle, which
        # overlays the last-persisted probability if one exists. Without
        # this, a restart happening while a storm probability was already
        # elevated above the crossing threshold would reset
        # _previous_probability to 0.0, and the very next fresh score
        # (also elevated) would look like a brand-new upward crossing —
        # firing an unwarranted bonus call for a "crossing" that never
        # actually happened, just a restart.
        self._state_loaded_from_db = False

    async def _async_load_persisted_state_if_needed(self) -> None:
        if self._state_loaded_from_db:
            return
        persisted = await self.hass.async_add_executor_job(
            self._db.get_model_b_previous_probability
        )
        if persisted is not None:
            self._previous_probability = persisted
        self._state_loaded_from_db = True

    async def _async_update_data(self) -> float:
        # v0.1.14: same defense-in-depth backstop as every other
        # coordinator now has. Generous (90s) since this method can also
        # trigger meteoblue/Meteonomiqs bonus calls (each already
        # independently timed-out, but bounding the whole cycle here too
        # is cheap insurance).
        async with asyncio.timeout(90):
            await self._async_load_persisted_state_if_needed()
            return await self._async_update_data_inner()

    async def _async_update_data_inner(self) -> float:
        rows = await self.hass.async_add_executor_job(
            self._db.get_station_observations_since,
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )
        now = datetime.now(timezone.utc)
        # v0.1.24 fix (P2-09): get_station_observations_since bounds only
        # the LOWER time edge, so nothing rejected a sample stamped in the
        # future — from clock skew, or a restored/replayed state. A
        # future-dated row becomes the window endpoint and silently
        # distorts every tendency delta.
        samples = []
        future_dated = 0
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["ts"])
            except (TypeError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > now:
                future_dated += 1
                continue
            samples.append(
                model_b.StationSample(
                    ts_epoch_seconds=ts.timestamp(),
                    temperature=r["temperature"],
                    humidity=r["humidity"],
                    pressure=r["pressure"],
                )
            )
        if future_dated and self._diagnostics is not None:
            self._diagnostics.record(
                source="model_b", event_type="data_quality",
                detail=f"{future_dated} future-dated station sample(s) ignored",
            )

        radar_values = self._combiprecip_coordinator.data or []
        # v0.1.24 fix (P1-13 / P1-16): valid_at and quality are now
        # threaded through. valid_at was captured from the HDF5 file's own
        # scan-time metadata into RadarPixelValue and then DROPPED right
        # here, which is what left the freshness of a radar reading
        # unknowable downstream.
        radar_points = tuple(
            model_b.RadarPointReading(
                label=v.label,
                precip_accum_mm_1h=v.precip_accum_mm_1h,
                valid_at=v.valid_at,
                quality=v.quality,
            )
            for v in radar_values
        )

        features = model_b.compute_tendency_features(
            samples=samples,
            now_epoch_seconds=time.time(),
            radar_points=radar_points,
        )
        base_probability = model_b.score_v0_graduated(features, now=now)
        probability = base_probability

        # v0.1.24 fix (P2-05): initialised unconditionally. These were
        # previously defined only inside the `if decision.should_trigger:`
        # block below, so referencing them in the richer prediction
        # payload — which this release does — would raise NameError on
        # every non-triggering cycle, i.e. almost all of them.
        got_meteonomiqs = False
        risk_values: list[int] = []

        decision = model_b.evaluate_cross_model_trigger(
            previous_probability=self._previous_probability,
            current_probability=probability,
            threshold=STORM_PREDICTION_UPPER_CROSSING_THRESHOLD,
        )
        if decision.should_trigger:
            _LOGGER.info(
                "Model B cross-model trigger fired (probability %.2f) — "
                "requesting bonus meteoblue + Meteonomiqs calls",
                probability,
            )
            # v0.1.15 fix: these bonus calls used to be unguarded — a
            # transient failure in either (a timeout, a rate limit —
            # plausible exactly during a real storm scenario when these
            # APIs may be under more load) would raise all the way out of
            # this method, meaning the freshly computed probability above
            # was never saved to current_probability at all. Confirmed by
            # an independent review as a real bug in the specific feature
            # (storm-onset detection for blinds automation) this project
            # was built for — exactly the moment reliability matters most.
            # Now isolated: a bonus-call failure is logged but never
            # prevents the base scoring result from being persisted and
            # exposed below.
            try:
                await self._meteoblue_coordinator.async_request_bonus_call()
                got_meteonomiqs = await self._meteonomiqs_coordinator.async_request_bonus_call()
                if got_meteonomiqs and self._meteonomiqs_coordinator.last_nowcast:
                    # v0.1.24 fix (P1-12): restrict to intervals that
                    # actually overlap the near-term window this score
                    # claims to describe. Previously every interval the
                    # nowcast returned was folded into max(risk_values)
                    # regardless of how far out it was, so a high-risk
                    # interval hours away could raise a score presented
                    # as "storm within ~30 minutes".
                    #
                    # An OVERLAP test, not "starts after now": an
                    # interval that began slightly before now and is
                    # still running is the single most relevant one, and
                    # a start-time filter would exclude exactly that.
                    target_cutoff = now + METEONOMIQS_NOWCAST_TARGET_WINDOW
                    risk_values = [
                        item.precip_risk_value
                        for item in self._meteonomiqs_coordinator.last_nowcast.items
                        if item.precip_risk_value is not None
                        and item.to_ts > now
                        and item.from_ts < target_cutoff
                    ]
                    if risk_values:
                        probability = model_b.refine_with_meteonomiqs(
                            base_probability=probability,
                            meteonomiqs_risk_value=max(risk_values),
                        )
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning(
                    "Cross-model trigger's bonus calls failed (base "
                    "probability %.2f is still saved normally): %s",
                    base_probability,
                    err,
                )

        # v0.1.15 fix: this used to persist the pre-refinement probability
        # (computed before the trigger/refinement block above), while
        # current_probability below got the post-refinement value — the
        # same storm event could show two different numbers depending on
        # whether you looked at history or the live sensor. Now persists
        # after refinement, and stores both values explicitly so the
        # refinement's effect (when it fires) stays visible in history
        # rather than being silently overwritten.
        # v0.1.24 fix (P2-05): the persisted feature blob captured only 2
        # of the 9 tendency deltas, and reduced each radar point to
        # {label: value} — no timestamp, no quality. That is not enough
        # to reconstruct what score_v0_graduated actually saw for a
        # historical prediction, which is the table's only stated
        # purpose: it is the training set for Model B v1. A training
        # example you cannot reproduce the inputs for is not a training
        # example.
        await self.hass.async_add_executor_job(
            self._db.insert_storm_prediction,
            now.isoformat(),
            probability,
            {
                "delta_pressure_10min": features.delta_pressure_10min,
                "delta_pressure_30min": features.delta_pressure_30min,
                "delta_pressure_60min": features.delta_pressure_60min,
                "delta_humidity_10min": features.delta_humidity_10min,
                "delta_humidity_30min": features.delta_humidity_30min,
                "delta_humidity_60min": features.delta_humidity_60min,
                "delta_temperature_10min": features.delta_temperature_10min,
                "delta_temperature_30min": features.delta_temperature_30min,
                "delta_temperature_60min": features.delta_temperature_60min,
                "radar_points": [
                    {
                        "label": p.label,
                        "precip_accum_mm_1h": p.precip_accum_mm_1h,
                        "valid_at": p.valid_at.isoformat() if p.valid_at else None,
                        "quality": p.quality,
                    }
                    for p in radar_points
                ],
                "station_sample_count": len(samples),
                "got_meteonomiqs": got_meteonomiqs,
                "meteonomiqs_risk_values": risk_values,
                "base_probability": base_probability,
                "refined_probability": probability,
            },
        )

        # v0.1.24 fix (P0-02), CRITICAL: the CROSSING STATE is the
        # unrefined base probability; the DISPLAYED value stays refined.
        #
        # These used to be the same variable. _previous_probability was
        # set to the post-refinement value, but evaluate_cross_model_trigger
        # always compares it against the NEXT cycle's unrefined base
        # score. refine_with_meteonomiqs is a plain average that can pull
        # a value below threshold while the base signal stays genuinely
        # elevated — so the next cycle's fresh base score read as a newly
        # crossing above a stale, refined previous value, and the trigger
        # fired again. And again.
        #
        # This is not an edge case. With V0_TRIGGER_PROBABILITY = 0.65, a
        # 0.5 threshold, and refinement averaging against risk/9, ANY
        # Meteonomiqs risk value of 0-3 (ordinary weather) drops the
        # stored value to 0.33-0.49 — below threshold — guaranteeing a
        # spurious re-trigger on the next cycle, every 5 minutes, for the
        # whole duration of any sustained signal. Quota survived on the
        # daily bonus caps; storm_predictions filled with duplicate
        # pseudo-events for a single storm, which is precisely the data
        # Model B v1 is meant to learn from.
        #
        # current_probability stays refined for display and history, for
        # the reason the original v0.1.15 fix gave: the same storm event
        # should not show two different numbers depending on whether you
        # look at the live sensor or the stored history.
        self._previous_probability = base_probability
        self.current_probability = probability
        # v0.1.23 fix (L-09): persist immediately, not just in memory —
        # this is what lets the load above survive a restart happening
        # at any point, not just neatly between scoring cycles.
        await self.hass.async_add_executor_job(
            self._db.set_model_b_previous_probability, base_probability
        )
        return probability


class RetentionCoordinator(DataUpdateCoordinator):
    """v0.1.23 fix (L-10): the only caller of SwissWeatherDB.purge_older_than().

    purge_older_than() itself was already correctly implemented — the
    external ICS audit's finding was that nothing in production ever
    called it, so the configured purge_days retention setting had no
    operational effect at all and high-volume tables (forecast_snapshots
    especially) could grow without bound.

    Runs on its own slow schedule (RETENTION_CHECK_INTERVAL, default
    24h) independent of every other coordinator's polling cadence —
    retention is a housekeeping concern, not a data-freshness one, so
    there's no reason to tie it to any provider's poll interval.
    purge_days = 0 means "keep forever" (per const.py's CONF_PURGE_DAYS
    docstring); this coordinator simply no-ops in that case rather than
    computing a meaningless cutoff.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        *,
        purge_days: int,
        retention_lock: Optional[asyncio.Lock] = None,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_retention",
            update_interval=RETENTION_CHECK_INTERVAL,
        )
        self._db = db
        self._purge_days = purge_days
        self._diagnostics = diagnostics
        # v0.1.24 fix (P2-03 / P2-04): ONE lock, shared with the other
        # coordinator that writes the same tables.
        #
        # Every SwissWeatherDB method takes self._lock individually, but
        # reconciliation's logical read-modify-write spans several
        # separate locked calls, and RetentionCoordinator can delete rows
        # in between them. Per-statement locking does not make a
        # multi-step read snapshot-consistent. A shared object rather
        # than two independent locks is the whole point — two
        # uncoordinated locks would serialize nothing against each other.
        #
        # Falls back to a private lock when none is injected, so each
        # coordinator remains independently constructible in tests.
        self._retention_lock = retention_lock or asyncio.Lock()

    async def _async_update_data(self) -> Optional[dict[str, int]]:
        if self._purge_days <= 0:
            return None
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self._purge_days)).isoformat()
        # v0.1.24 (P2-04): held for the whole purge so deletions cannot
        # land in the middle of a reconciliation batch's reads.
        async with self._retention_lock:
            deleted = await self.hass.async_add_executor_job(
                self._db.purge_older_than, cutoff
            )
        total_deleted = sum(deleted.values())
        if total_deleted > 0:
            _LOGGER.info(
                "Retention purge (cutoff %d days): deleted %s",
                self._purge_days,
                ", ".join(f"{table}={count}" for table, count in deleted.items() if count > 0),
            )
        if self._diagnostics is not None:
            self._diagnostics.record(
                source="retention", event_type="purge",
                detail=f"{total_deleted} rows deleted across high-volume tables",
                extra=deleted,
            )
        return deleted


class StormEventReconciliationCoordinator(DataUpdateCoordinator):
    """Confirms or rejects past storm predictions against what actually happened.

    **v0.1.24 fix (P2-08).** This is the first production caller of
    SwissWeatherDB.insert_storm_event(), which until now had none at all
    — verified by grep across the whole package. storm_events is the
    ground-truth table the entire Model B v0 -> v1 plan depends on, and
    nothing could ever put a row in it. The v1 upgrade path documented in
    DEVELOPER.md was therefore unreachable by construction, not merely
    "not done yet".

    **How a prediction is confirmed.** For any storm_predictions row
    whose follow-up window has fully elapsed, and whose probability was
    high enough to have actually made a claim, the real station and radar
    observations across that window are fetched and checked against Model
    B's OWN existing v0 thresholds — V0_PRESSURE_DROP_HPA_THRESHOLD and
    RADAR_PRECIP_ACCUM_MM_THRESHOLD.

    Reusing the live scorer's thresholds is deliberate. Inventing a
    second, independent definition of "a storm signature" here would mean
    the training labels described a different phenomenon from the one the
    model is trying to predict, which is a subtle way to produce a v1
    model that is confidently wrong. The honest caveat, carried in
    DEVELOPER.md: these thresholds are themselves an unvalidated v0
    heuristic, so what this table records is "the v0 signature was
    observed", not "a meteorologist would call this a storm". That is
    still enormously more useful than an empty table, and it is worth
    revisiting once real events accumulate.

    A confirmed prediction is promoted to a storm_events row with the
    ACTUALLY OBSERVED peak values, not the predicted ones — storing the
    prediction back as if it were ground truth would make the training
    set circular.

    Every checked prediction is marked reconciled whether or not it was
    confirmed, so nothing is ever re-checked. An unconfirmed prediction
    is a negative training example, not an unfinished job.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        db: SwissWeatherDB,
        *,
        diagnostics: Any = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="swissweather_fusion_storm_reconciliation",
            update_interval=STORM_RECONCILIATION_INTERVAL,
        )
        self._db = db
        self._diagnostics = diagnostics
        self.last_confirmed_count: int = 0
        self.last_checked_count: int = 0

    async def _async_update_data(self) -> dict[str, int]:
        async with asyncio.timeout(120):
            return await self.hass.async_add_executor_job(self._reconcile_storm_events)

    def _reconcile_storm_events(self) -> dict[str, int]:
        """Synchronous — only ever called via the executor job above."""
        now = datetime.now(timezone.utc)
        # Only predictions whose outcome has had time to play out fully.
        cutoff = (now - STORM_FOLLOW_UP_WINDOW).isoformat()
        predictions = self._db.get_unreconciled_storm_predictions(
            cutoff, STORM_RECONCILIATION_MIN_PROBABILITY
        )
        if not predictions:
            self.last_checked_count = 0
            self.last_confirmed_count = 0
            return {"checked": 0, "confirmed": 0}

        checked_ids: list[int] = []
        confirmed = 0

        for prediction in predictions:
            try:
                predicted_at = datetime.fromisoformat(prediction["ts"])
            except (TypeError, ValueError):
                # Unparseable timestamp: mark it checked so it does not
                # jam the queue forever, but never promote it.
                checked_ids.append(prediction["id"])
                continue
            if predicted_at.tzinfo is None:
                predicted_at = predicted_at.replace(tzinfo=timezone.utc)
            window_end = predicted_at + STORM_FOLLOW_UP_WINDOW

            evidence = self._collect_evidence(predicted_at, window_end)
            checked_ids.append(prediction["id"])

            if evidence is None:
                continue

            peak_pressure_drop, peak_precip = evidence
            pressure_confirms = (
                peak_pressure_drop is not None
                and peak_pressure_drop >= V0_PRESSURE_DROP_HPA_THRESHOLD
            )
            radar_confirms = (
                peak_precip is not None
                and peak_precip >= RADAR_PRECIP_ACCUM_MM_THRESHOLD
            )
            if not (pressure_confirms or radar_confirms):
                continue

            self._db.insert_storm_event(
                predicted_at.isoformat(),
                window_end.isoformat(),
                peak_pressure_drop if peak_pressure_drop is not None else 0.0,
                0.0,
                peak_precip if peak_precip is not None else 0.0,
            )
            confirmed += 1

        self._db.mark_storm_predictions_reconciled(checked_ids)
        self.last_checked_count = len(checked_ids)
        self.last_confirmed_count = confirmed

        if self._diagnostics is not None and checked_ids:
            self._diagnostics.record(
                source="model_b", event_type="storm_reconciliation",
                detail=f"checked {len(checked_ids)}, confirmed {confirmed}",
            )
        _LOGGER.debug(
            "Storm reconciliation: checked %d prediction(s), confirmed %d",
            len(checked_ids),
            confirmed,
        )
        return {"checked": len(checked_ids), "confirmed": confirmed}

    def _collect_evidence(
        self, start: datetime, end: datetime
    ) -> Optional[tuple[Optional[float], Optional[float]]]:
        """Peak observed pressure drop (hPa) and peak radar accumulation (mm).

        Returns None when there is no usable observation at all across the
        window — an absence of evidence is not evidence of absence, and
        marking such a prediction "did not verify" would teach a future
        model that a real storm was a false alarm.
        """
        station_rows = self._db.get_station_observations_between(
            start.isoformat(), end.isoformat()
        )
        radar_rows = self._db.get_radar_observations_between(
            start.isoformat(), end.isoformat()
        )
        if not station_rows and not radar_rows:
            return None

        pressures = [r["pressure"] for r in station_rows if r["pressure"] is not None]
        peak_drop: Optional[float] = None
        if len(pressures) >= 2:
            # Largest fall from any earlier reading to any later one —
            # a running maximum, matching how the live scorer looks at a
            # drop across a window rather than only at its endpoints.
            running_max = pressures[0]
            peak_drop = 0.0
            for value in pressures[1:]:
                running_max = max(running_max, value)
                peak_drop = max(peak_drop, running_max - value)

        precips = [
            r["precip_accum_mm_1h"]
            for r in radar_rows
            if r["precip_accum_mm_1h"] is not None
        ]
        peak_precip = max(precips) if precips else None

        return peak_drop, peak_precip
