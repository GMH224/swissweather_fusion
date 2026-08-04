"""Home Assistant's built-in Diagnostics platform.

Implementing async_get_config_entry_diagnostics is what makes a
"Download Diagnostics" option appear natively in the integration's UI
(Settings → Devices & Services → SwissWeather Fusion → the three-dot
menu) — no custom download mechanism needed, HA already has one built
for exactly this purpose.

**Everything returned here goes through redaction.py first** — both the
config entry's own data (credentials, coordinates, elevation) and the
DiagnosticsRecorder's buffered events (which can include raw third-party
API response bodies — see diagnostics_recorder.py and redaction.py for
why those need redacting too, not just this project's own config).

**Bug found and fixed from a real downloaded diagnostics file**: the
first version of this module redacted `config_data`/`config_options` (the
integration's own settings) but assumed `last_data_error`/`last_auth_error`
"needed no redacting" since they're just short status strings, not
structured config. That was wrong — a real capture showed a 503 error
from Open-Meteo whose message was the *full request URL*, which embeds
latitude/longitude as query parameters
(`?latitude=...&longitude=...`). Any error message built from `str(err)`
on an HTTP client exception can carry a URL like this. Both fields now go
through the same coordinate-string redaction pass as everything else —
this is exactly the kind of "value embeds location in a place the key
name doesn't suggest" case this project has run into before (SRF's own
`geolocationId` being a bare coordinate string was the original version of
this exact problem).

This file is intentionally thin: it assembles what's already been
recorded and redacts it, rather than triggering new API calls of its
own — downloading diagnostics should be safe to do at any time without
side effects.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_METEOBLUE_API_KEY,
    CONF_METEONOMIQS_API_KEY,
    CONF_OPEN_METEO_API_KEY,
    CONF_SRF_CONSUMER_KEY,
    CONF_SRF_CONSUMER_SECRET,
    DOMAIN,
)
from .redaction import redact_coordinate_strings, redact_secret_values, redact_sensitive_keys


def _redact_text(
    value: str, *, latitude: float, longitude: float, secrets: list[str]
) -> str:
    value = redact_coordinate_strings(value, latitude=latitude, longitude=longitude)
    return redact_secret_values(value, secrets=secrets)


def _redact_event(
    event: dict[str, Any], *, latitude: float, longitude: float, secrets: list[str]
) -> dict[str, Any]:
    """v0.1.20 fix: this module's own docstring/note has always claimed
    diagnostics_events "are passed through the same redaction" as
    everything else — that was never actually true.
    DiagnosticsRecorder.record() does no redaction of its own (it's a
    dumb append, by design — see diagnostics_recorder.py), and the
    caller-side redaction that DOES exist (SrfClient._record_diagnostic,
    which redacts raw_payload before recording) only covers that one
    call site. Every OTHER `self._diagnostics.record(...)` call across
    every coordinator — most importantly `poll_failure` events, whose
    `detail` is built from `str(exception)` and can therefore contain a
    full request URL, coordinates and all, or (for Open-Meteo
    specifically, which embeds its API key as a URL query parameter) a
    real credential — went out completely unredacted. Found while
    investigating a real SRF `forecastpoint` 400 error; confirmed the
    same gap applies to every source, not just SRF.

    Redacts `detail` and any string values inside `extra` (recursively
    one level, which is as deep as any current event's `extra` goes) for
    both coordinates and configured secrets. Applied centrally here,
    covering every event regardless of which coordinator recorded it or
    whether that call site remembers to redact — the same "redact once,
    at the single funnel point everything already passes through" design
    already used for config_data/config_options and source_health above.
    """
    redacted_extra: dict[str, Any] = {}
    for key, value in event.get("extra", {}).items():
        if isinstance(value, str):
            redacted_extra[key] = _redact_text(
                value, latitude=latitude, longitude=longitude, secrets=secrets
            )
        else:
            # Non-string extra values (raw_response dicts, point_count
            # ints, used_fallback bools) are either already redacted at
            # the point they were built (raw_response, via
            # redact_diagnostic_payload) or aren't sensitive to begin
            # with (counts/flags) — left as-is.
            redacted_extra[key] = value
    return {
        "ts": event.get("ts"),
        "source": event.get("source"),
        "event_type": event.get("event_type"),
        "detail": _redact_text(
            event.get("detail", ""), latitude=latitude, longitude=longitude, secrets=secrets
        ),
        "extra": redacted_extra,
    }


def _health_summary(
    health: Any, *, latitude: float, longitude: float, secrets: list[str]
) -> dict[str, Any]:
    """A snapshot of one SourceHealth object's state. last_data_error and
    last_auth_error are exception message strings and can embed a full
    request URL (query parameters and all) — these go through coordinate
    AND secret redaction (v0.1.20: added secrets here too — Open-Meteo's
    client embeds its API key directly in the request URL, so an error
    message from that source could carry it, same as the
    diagnostics_events leak this was found alongside), unlike the plain
    timestamps/counts which don't need either.
    """
    if health is None:
        return {}
    return {
        "last_success_time": health.last_success_time.isoformat()
        if health.last_success_time
        else None,
        "last_poll_duration_ms": health.last_poll_duration_ms,
        "last_data_error": (
            _redact_text(health.last_data_error, latitude=latitude, longitude=longitude, secrets=secrets)
            if health.last_data_error is not None
            else None
        ),
        "last_auth_error": (
            _redact_text(health.last_auth_error, latitude=latitude, longitude=longitude, secrets=secrets)
            if health.last_auth_error is not None
            else None
        ),
        "consecutive_failures": health.consecutive_failures,
    }


def _coordinator_scheduling_summary(
    coordinator: Any, *, last_success_time: Any = None
) -> dict[str, Any]:
    """v0.1.11: added after a 2-hour diagnostic-logging window showed
    zero new events and zero updated timestamps for the six source
    coordinators — much stronger evidence of an actual scheduling problem
    than a simple "downloaded right after reload" explanation could
    account for. Reports Home Assistant's own built-in
    `last_update_success` for EVERY coordinator (a signal independent of
    this project's own SourceHealth bookkeeping, in case that bookkeeping
    itself has a bug), plus a computed `overdue` flag comparing how long
    it's actually been since the last success against the coordinator's
    own configured interval — so a future capture is self-explanatory
    rather than needing the elapsed time worked out by hand every time.
    """
    if coordinator is None:
        return {"present": False}
    interval = getattr(coordinator, "update_interval", None)
    interval_seconds = interval.total_seconds() if interval else None

    seconds_since_last_success = None
    overdue = None
    if last_success_time is not None and interval_seconds:
        from datetime import datetime, timezone

        seconds_since_last_success = (
            datetime.now(timezone.utc) - last_success_time
        ).total_seconds()
        # A generous 3x multiplier before calling it "overdue" — avoids
        # flagging ordinary jitter (a slightly slow poll, a scheduling
        # check that skips one cycle) as if it were the same problem this
        # was built to catch.
        overdue = seconds_since_last_success > interval_seconds * 3

    return {
        "present": True,
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "update_interval_seconds": interval_seconds,
        "seconds_since_last_success": seconds_since_last_success,
        "overdue": overdue,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data[DOMAIN][entry.entry_id]
    recorder = runtime.get("diagnostics_recorder")

    # The REAL coordinates, needed for the coordinate-redaction pass below
    # — extracted before entry.data gets redacted, since redaction needs
    # the actual values to search for and replace.
    latitude = entry.data.get(CONF_LATITUDE, 0.0)
    longitude = entry.data.get(CONF_LONGITUDE, 0.0)
    # v0.1.20: real secret values, same "extract before redacting
    # entry.data" pattern as coordinates above — needed to scrub these
    # exact values out of event detail/extra text (see _redact_event).
    #
    # v0.1.22 fix: this assignment briefly existed only in a leftover,
    # never-actually-called duplicate of this function (an artifact of
    # an earlier imprecise edit — Python silently keeps the LAST
    # definition of a module-level function, so the real one being
    # called had no `secrets` defined at all). Confirmed broken via a
    # real production crash: NameError: name 'secrets' is not defined,
    # in this exact function, the moment diagnostics were downloaded.
    # `ast.parse`-based syntax checks can't catch this (duplicate
    # function names are syntactically legal), and the fast unit suite
    # only exercised the smaller helper functions, never this top-level
    # one directly — see tests/test_diagnostics.py's new
    # test_async_get_config_entry_diagnostics_smoke for why that gap is
    # now closed.
    secrets = [
        entry.data.get(CONF_SRF_CONSUMER_KEY),
        entry.data.get(CONF_SRF_CONSUMER_SECRET),
        entry.data.get(CONF_METEOBLUE_API_KEY),
        entry.data.get(CONF_METEONOMIQS_API_KEY),
        entry.data.get(CONF_OPEN_METEO_API_KEY),
    ]

    # Config entry data/options redacted the same way as everything else
    # in this project — credentials, coordinates, and elevation all match
    # SENSITIVE_KEY_SUBSTRINGS in redaction.py.
    redacted_data = redact_sensitive_keys(dict(entry.data))
    redacted_options = redact_sensitive_keys(dict(entry.options or {}))

    source_health: dict[str, Any] = {}
    for name in ("station", "srf", "meteoblue", "combiprecip", "meteonomiqs"):
        coordinator = runtime.get(f"{name}_coordinator")
        health = getattr(coordinator, "health", None) if coordinator is not None else None
        source_health[name] = _health_summary(
            health, latitude=latitude, longitude=longitude, secrets=secrets
        )
        source_health[name]["scheduling"] = _coordinator_scheduling_summary(
            coordinator,
            last_success_time=getattr(health, "last_success_time", None),
        )

    open_meteo_coordinator = runtime.get("open_meteo_coordinator")
    if open_meteo_coordinator is not None and hasattr(open_meteo_coordinator, "health"):
        source_health["open_meteo"] = {
            model: _health_summary(h, latitude=latitude, longitude=longitude, secrets=secrets)
            for model, h in open_meteo_coordinator.health.items()
        }
        # Uses whichever model has the most recent success, for the
        # coordinator-level overdue check — CH1/CH2/D2 share one
        # coordinator, so any one of them refreshing counts as the
        # coordinator itself still being scheduled.
        most_recent = None
        for h in open_meteo_coordinator.health.values():
            if h.last_success_time and (most_recent is None or h.last_success_time > most_recent):
                most_recent = h.last_success_time
        source_health["open_meteo"]["scheduling"] = _coordinator_scheduling_summary(
            open_meteo_coordinator, last_success_time=most_recent
        )

    # v0.1.11: these three coordinators have no SourceHealth object (they
    # don't fetch from an external API the same way), which is exactly
    # why they were invisible in earlier diagnostics captures despite
    # being the ones whose apparent freezing originally raised this
    # question. Reported separately, using whatever each actually
    # exposes.
    internal_coordinators: dict[str, Any] = {}
    model_b_coordinator = runtime.get("model_b_coordinator")
    internal_coordinators["model_b"] = {
        **_coordinator_scheduling_summary(model_b_coordinator),
        "current_probability": getattr(model_b_coordinator, "current_probability", None),
    }
    blend_coordinator = runtime.get("blend_coordinator")
    internal_coordinators["blend"] = {
        **_coordinator_scheduling_summary(blend_coordinator),
        "has_computed_data": bool(getattr(blend_coordinator, "data", None)),
    }
    learning_coordinator = runtime.get("learning_coordinator")
    learning_last_run = getattr(learning_coordinator, "data", None)
    internal_coordinators["learning"] = {
        **_coordinator_scheduling_summary(
            learning_coordinator, last_success_time=learning_last_run
        ),
        "last_run_time": learning_last_run.isoformat() if learning_last_run else None,
        "last_reconciled_count": getattr(learning_coordinator, "last_reconciled_count", None),
    }

    # v0.1.20 fix: this used to be `recorder.get_events() if recorder is
    # not None else []` — passed straight through with NO redaction at
    # all, despite the "note" text below always having claimed
    # diagnostics_events go through "the same redaction". See
    # _redact_event's docstring for the full story and how this was
    # found (a real SRF 400 error investigation, not a proactive audit).
    diagnostics_events = (
        [
            _redact_event(e, latitude=latitude, longitude=longitude, secrets=secrets)
            for e in recorder.get_events()
        ]
        if recorder is not None
        else []
    )

    return {
        "diagnostic_logging_enabled": recorder.enabled if recorder is not None else False,
        "note": (
            "Coordinates, elevation, and API credentials are redacted below, "
            "including inside error message strings that may embed a full "
            "request URL or (for Open-Meteo specifically, whose client "
            "embeds its API key as a URL query parameter) a real "
            "credential. diagnostics_events may include raw third-party "
            "API response bodies (only when diagnostic logging was "
            "enabled) — these are passed through the same redaction, but "
            "review before sharing publicly regardless, since third-party "
            "APIs occasionally embed identifying data in fields this "
            "project doesn't yet know to look for."
        ),
        "config_data": redacted_data,
        "config_options": redacted_options,
        "source_health": source_health,
        "internal_coordinators": internal_coordinators,
        "diagnostics_events": diagnostics_events,
    }
