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

from typing import Any, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LATITUDE, CONF_LONGITUDE, DOMAIN
from .redaction import redact_coordinate_strings, redact_sensitive_keys


def _redact_error_string(
    value: Optional[str], *, latitude: float, longitude: float
) -> Optional[str]:
    if value is None:
        return None
    return redact_coordinate_strings(value, latitude=latitude, longitude=longitude)


def _health_summary(health: Any, *, latitude: float, longitude: float) -> dict[str, Any]:
    """A snapshot of one SourceHealth object's state. last_data_error and
    last_auth_error are exception message strings and can embed a full
    request URL (query parameters and all) — these go through coordinate
    redaction, unlike the plain timestamps/counts which don't need it.
    """
    if health is None:
        return {}
    return {
        "last_success_time": health.last_success_time.isoformat()
        if health.last_success_time
        else None,
        "last_poll_duration_ms": health.last_poll_duration_ms,
        "last_data_error": _redact_error_string(
            health.last_data_error, latitude=latitude, longitude=longitude
        ),
        "last_auth_error": _redact_error_string(
            health.last_auth_error, latitude=latitude, longitude=longitude
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

    # Config entry data/options redacted the same way as everything else
    # in this project — credentials, coordinates, and elevation all match
    # SENSITIVE_KEY_SUBSTRINGS in redaction.py.
    redacted_data = redact_sensitive_keys(dict(entry.data))
    redacted_options = redact_sensitive_keys(dict(entry.options or {}))

    source_health: dict[str, Any] = {}
    for name in ("station", "srf", "meteoblue", "combiprecip", "meteonomiqs"):
        coordinator = runtime.get(f"{name}_coordinator")
        health = getattr(coordinator, "health", None) if coordinator is not None else None
        source_health[name] = _health_summary(health, latitude=latitude, longitude=longitude)
        source_health[name]["scheduling"] = _coordinator_scheduling_summary(
            coordinator,
            last_success_time=getattr(health, "last_success_time", None),
        )

    open_meteo_coordinator = runtime.get("open_meteo_coordinator")
    if open_meteo_coordinator is not None and hasattr(open_meteo_coordinator, "health"):
        source_health["open_meteo"] = {
            model: _health_summary(h, latitude=latitude, longitude=longitude)
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

    diagnostics_events = recorder.get_events() if recorder is not None else []

    return {
        "diagnostic_logging_enabled": recorder.enabled if recorder is not None else False,
        "note": (
            "Coordinates, elevation, and API credentials are redacted below, "
            "including inside error message strings that may embed a full "
            "request URL. diagnostics_events may include raw third-party API "
            "response bodies (only when diagnostic logging was enabled) — "
            "these are passed through the same redaction, but review before "
            "sharing publicly regardless, since third-party APIs "
            "occasionally embed identifying data in fields this project "
            "doesn't yet know to look for."
        ),
        "config_data": redacted_data,
        "config_options": redacted_options,
        "source_health": source_health,
        "internal_coordinators": internal_coordinators,
        "diagnostics_events": diagnostics_events,
    }
