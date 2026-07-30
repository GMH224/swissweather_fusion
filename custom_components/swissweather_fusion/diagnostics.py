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

This file is intentionally thin: it assembles what's already been
recorded and redacts it, rather than triggering new API calls of its
own — downloading diagnostics should be safe to do at any time without
side effects.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .redaction import redact_sensitive_keys


def _health_summary(health: Any) -> dict[str, Any]:
    """A compact, already-non-sensitive snapshot of one SourceHealth
    object's state — timestamps and counts, nothing that needs redacting.
    """
    if health is None:
        return {}
    return {
        "last_success_time": health.last_success_time.isoformat()
        if health.last_success_time
        else None,
        "last_poll_duration_ms": health.last_poll_duration_ms,
        "last_data_error": health.last_data_error,
        "last_auth_error": health.last_auth_error,
        "consecutive_failures": health.consecutive_failures,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    runtime = hass.data[DOMAIN][entry.entry_id]
    recorder = runtime.get("diagnostics_recorder")

    # Config entry data/options redacted the same way as everything else
    # in this project — credentials, coordinates, and elevation all match
    # SENSITIVE_KEY_SUBSTRINGS in redaction.py.
    redacted_data = redact_sensitive_keys(dict(entry.data))
    redacted_options = redact_sensitive_keys(dict(entry.options or {}))

    source_health: dict[str, Any] = {}
    for name in ("station", "srf", "meteoblue", "combiprecip", "meteonomiqs"):
        coordinator = runtime.get(f"{name}_coordinator")
        health = getattr(coordinator, "health", None) if coordinator is not None else None
        source_health[name] = _health_summary(health)

    open_meteo_coordinator = runtime.get("open_meteo_coordinator")
    if open_meteo_coordinator is not None and hasattr(open_meteo_coordinator, "health"):
        source_health["open_meteo"] = {
            model: _health_summary(h) for model, h in open_meteo_coordinator.health.items()
        }

    diagnostics_events = recorder.get_events() if recorder is not None else []

    return {
        "diagnostic_logging_enabled": recorder.enabled if recorder is not None else False,
        "note": (
            "Coordinates, elevation, and API credentials are redacted below. "
            "diagnostics_events may include raw third-party API response "
            "bodies (only when diagnostic logging was enabled) — these are "
            "passed through the same redaction as everything else, but "
            "review before sharing publicly regardless, since third-party "
            "APIs occasionally embed identifying data in fields this "
            "project doesn't yet know to look for."
        ),
        "config_data": redacted_data,
        "config_options": redacted_options,
        "source_health": source_health,
        "diagnostics_events": diagnostics_events,
    }
